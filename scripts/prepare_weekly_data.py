
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = (28, 56, 84)
DAY = timedelta(days=1)
VERSION = "1.0.0"
DICTIONARY = """# Weekly example data dictionary

## Unit and eligibility

One row per neighborhood known at the beginning of Monday and complete forecast week (Monday through Sunday). All three windows share the same calendar after 84 days of coverage. Membership grows at the first eligible registration date strictly before the forecast; future labels are not used and continued neighborhood activity is not required. Normalized microdata exclusions remain in effect.

## Examples

| Field | Type | Meaning |
|---|---|---|
| bairro_chave | string | Normalized SSP neighborhood label, known before the forecast, without an official polygon or adjacency. |
| inicio_semana | date32 | Forecast Monday, at the beginning of the day. Temporal identifier, not a direct predictor. |
| fim_semana | date32 | Following Sunday, included in the target. |
| janela_dias | int16 | 28, 56 or 84; experiment configuration. |
| mes | int8 | Month of the forecast Monday, known in advance. |
| contagem_semana_anterior_01 ... | int32 | Available occurrence counts in seven-day blocks; 01 is the most recent block, [start minus seven days, start). There are 4, 8 or 12 blocks. |
| media_semanal | float64 | Mean block count within the window. |
| desvio_padrao_semanal | float64 | Population standard deviation of block counts, divisor n (ddof=0). |
| tendencia_semanal | float64 | Least-squares slope of weekly counts in chronological order; records per week of advancement. |
| dias_sem_registros | int16 | Days with zero available records in the window. Does not imply absence of crime. |
| alvo_retrospectivo | int32 | Weekly count in the complete available snapshot. For evaluation only, not historical fitting. |

All historical features require occurrence dates inside the window and registration dates before week start. Zeros before a neighborhood is first observed do not prove territorial existence. This preparation fits no encoders, learned feature selection, geographic neighbors, IBGE integration or models.

## Target reconstruction

contribuicoes_alvo.parquet: bairro_chave (string), inicio_semana (date32), data_registro (date32) and quantidade_registros (int32). Summing contributions by neighborhood and week recovers the retrospective target. The targets command sums only registration dates before the cutoff, removes alvo_retrospectivo and still-open weeks, and adds alvo_disponivel (int32) and data_corte_treinamento (date32). Historical inputs remain those calculated at each forecast origin. The cutoff is exclusive for both weeks and records on the boundary. Available targets are provisional: no record at the cutoff does not prove no occurrence. No maturity policy is imposed at this preparation stage.

## Audit

bairros_desconhecidos.parquet: bairro_chave (string), inicio_semana (date32) and quantidade_registros (int32) for targets of categories unknown at forecast origin, without adding them artificially to inputs. relatorio.json contains split totals, weeks and calendar exclusions. conferencia_semanal.csv details the same totals by week. Each record count is reported once, without tripling across windows.

The weekly manifest covers all files in this directory except itself. The original manifest still covers the preserved earlier snapshot. Registration date is a proxy for publication; complete historical snapshots of report versions are unavailable. Recent targets may lack delayed reports outside the snapshot.
"""


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def monday(d):
    return d - timedelta(days=d.weekday())


def origins(splits, start, end):

    result = {}
    for split, bounds in splits.items():
        lo, hi = (date.fromisoformat(bounds[k]) for k in ("start", "end"))
        lo, hi = max(lo, start + timedelta(days=84)), min(hi, end)
        d = lo + timedelta(days=(-lo.weekday()) % 7)
        while d + timedelta(days=6) <= hi:
            if d in result:
                raise ValueError("Weeks shared across splits")
            result[d] = split
            d += timedelta(days=7)
    return dict(sorted(result.items()))


def load_records(source):
    manifest = json.loads((source / "metadados/manifest.json").read_text(encoding="utf-8"))

    for relative, expected in manifest["outputs_sha256"].items():
        if sha256(source / relative) != expected:
            raise ValueError(f"Hash mismatch: {relative}")
    records, seen = [], set()
    for path in sorted((source / "registros").glob("*.parquet")):
        data = pq.read_table(path, columns=["record_id", "bairro_chave", "data_ocorrencia", "data_registro", "elegivel_painel"]).to_pydict()
        for rid, b, event, registered, eligible in zip(*data.values()):
            if rid in seen:
                raise ValueError(f"Duplicate provenance: {rid}")
            seen.add(rid)
            if eligible:
                if not b or not event or not registered or event > registered:
                    raise ValueError(f"Inconsistent eligible record: {rid}")
                records.append((b, event, registered))
    expected = manifest["summary"]["registros_elegiveis_paineis"]
    if len(records) != expected:
        raise ValueError("Eligible total differs from the manifest")
    return records


def weekly_statistics(daily, window):

    values = daily[-window:]
    blocks = [sum(values[i:i + 7]) for i in range(0, window, 7)]
    n = len(blocks)
    mean = sum(blocks) / n
    center = (n - 1) / 2
    slope = sum((i - center) * (y - mean) for i, y in enumerate(blocks)) / sum((i - center) ** 2 for i in range(n))
    out = {f"contagem_semana_anterior_{i + 1:02}": y for i, y in enumerate(reversed(blocks))}
    out.update(media_semanal=mean, desvio_padrao_semanal=math.sqrt(sum((y - mean) ** 2 for y in blocks) / n), tendencia_semanal=slope, dias_sem_registros=sum(y == 0 for y in values))
    return out


def example_schema(window):
    fields = [("bairro_chave", pa.string()), ("inicio_semana", pa.date32()), ("fim_semana", pa.date32()), ("janela_dias", pa.int16()), ("mes", pa.int8())]
    fields += [(f"contagem_semana_anterior_{i + 1:02}", pa.int32()) for i in range(window // 7)]
    fields += [("media_semanal", pa.float64()), ("desvio_padrao_semanal", pa.float64()), ("tendencia_semanal", pa.float64()), ("dias_sem_registros", pa.int16()), ("alvo_retrospectivo", pa.int32())]
    return pa.schema(fields, metadata={b"weekly_rules_version": VERSION.encode(), b"target_warning": b"Retrospective target for scoring only. Rebuild training labels with targets --as-of."})


def build(source, output):
    if output.exists() and any(output.iterdir()):
        raise ValueError("Destination must be empty; use another directory to rebuild and compare")
    records = load_records(source)
    rules = json.loads((source / "metadados/regras.json").read_text(encoding="utf-8"))
    coverage = sorted((date.fromisoformat(v["start"]), date.fromisoformat(v["end"])) for v in rules["coverage"].values())
    for previous, following in zip(coverage, coverage[1:]):
        if previous[1] + DAY != following[0]:
            raise ValueError("Coverage must be continuous")
    start, end = coverage[0][0], coverage[-1][1]
    dates = origins(rules["splits"], start, end)
    targets, contributions, per_date = Counter(), Counter(), Counter()
    for b, event, registered in records:
        week = monday(event)
        targets[b, week] += 1
        contributions[b, week, registered] += 1
        per_date[event] += 1
    targets_by_week = defaultdict(dict)
    for (b, w), n in targets.items():
        targets_by_week[w][b] = n
    output.mkdir(parents=True, exist_ok=True)
    ledger_schema = pa.schema([("bairro_chave", pa.string()), ("inicio_semana", pa.date32()), ("data_registro", pa.date32()), ("quantidade_registros", pa.int32())])
    ledger = [dict(bairro_chave=b, inicio_semana=w, data_registro=r, quantidade_registros=n) for (b, w, r), n in sorted(contributions.items(), key=lambda x: (x[0][1], x[0][0], x[0][2]))]
    pq.write_table(pa.Table.from_pylist(ledger, schema=ledger_schema), output / "contribuicoes_alvo.parquet", compression="zstd")
    del ledger
    history, known = defaultdict(Counter), set()
    ordered = sorted(records, key=lambda r: (r[2], r[1], r[0]))
    cursor = 0
    audit, summaries, writers = [], {}, {}
    for split in rules["splits"]:
        (output / split).mkdir()
        for window in WINDOWS:
            writers[split, window] = pq.ParquetWriter(output / split / f"janela_{window}.parquet", example_schema(window), compression="zstd")
        summaries[split] = dict(semanas=0, exemplos_por_janela=0, alvos_zero_por_janela=0, registros_avaliados=0, registros_bairros_desconhecidos=0)
    try:
        for origin, split in dates.items():
            while cursor < len(ordered) and ordered[cursor][2] < origin:
                b, event, _ = ordered[cursor]
                known.add(b)
                history[b][event.toordinal()] += 1
                cursor += 1
            rows = {window: [] for window in WINDOWS}
            evaluated, zero = 0, 0
            for b in sorted(known):
                daily = [history[b].get(d, 0) for d in range(origin.toordinal() - 84, origin.toordinal())]
                target = targets[b, origin]
                evaluated += target
                zero += target == 0
                for window in WINDOWS:
                    row = dict(bairro_chave=b, inicio_semana=origin, fim_semana=origin + timedelta(days=6), janela_dias=window, mes=origin.month, alvo_retrospectivo=target)
                    row.update(weekly_statistics(daily, window))
                    rows[window].append(row)
            for window in WINDOWS:
                writers[split, window].write_table(pa.Table.from_pylist(rows[window], schema=example_schema(window)))
            unknown = sum(n for b, n in targets_by_week[origin].items() if b not in known)
            total = sum(per_date[origin + timedelta(days=i)] for i in range(7))
            if evaluated + unknown != total:
                raise ValueError("Weekly reconciliation failed")
            audit.append(dict(conjunto=split, inicio_semana=origin.isoformat(), bairros_conhecidos=len(known), exemplos_por_janela=len(known), registros_avaliados=evaluated, registros_bairros_desconhecidos=unknown, total_elegivel_semana=total, alvos_zero=zero))
            s = summaries[split]
            for key, value in [("semanas", 1), ("exemplos_por_janela", len(known)), ("alvos_zero_por_janela", zero), ("registros_avaliados", evaluated), ("registros_bairros_desconhecidos", unknown)]:
                s[key] += value
    finally:
        for writer in writers.values():
            writer.close()

    first_seen = {}
    for b, _, registered in ordered:
        first_seen.setdefault(b, registered)
    unknown_rows = [dict(bairro_chave=b, inicio_semana=w, quantidade_registros=n) for (b, w), n in targets.items() if w in dates and first_seen[b] >= w]
    unknown_rows.sort(key=lambda r: (r["inicio_semana"], r["bairro_chave"]))
    pq.write_table(pa.Table.from_pylist(unknown_rows, schema=pa.schema([("bairro_chave", pa.string()), ("inicio_semana", pa.date32()), ("quantidade_registros", pa.int32())])), output / "bairros_desconhecidos.parquet", compression="zstd")
    excluded = Counter()
    for _, event, _ in records:
        if monday(event) not in dates:
            reason = "historico_inicial_84_dias" if monday(event) < start + timedelta(days=84) else "semana_parcial_ou_cruza_divisao"
            excluded[reason] += 1
    total_evaluated = sum(s["registros_avaliados"] for s in summaries.values())
    total_unknown = sum(s["registros_bairros_desconhecidos"] for s in summaries.values())
    if total_evaluated + total_unknown + sum(excluded.values()) != len(records):
        raise ValueError("Invalid overall reconciliation")
    report = dict(version=VERSION, registros_elegiveis=len(records), conjuntos=summaries, registros_fora_calendario=dict(excluded), semanas=audit, disponibilidade="Registration date precedes week start; proxy for publication", alvo="Records by offense classification, not unique crimes", cobertura=[start.isoformat(), end.isoformat()])
    write_json(output / "relatorio.json", report)
    import csv
    with (output / "conferencia_semanal.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(audit[0]))
        writer.writeheader()
        writer.writerows(audit)
    lines = ["# Weekly dataset verification", "", "| Split | Weeks | Examples per window | Evaluated records | Records in unknown neighborhoods |", "|---|---:|---:|---:|---:|"]
    lines += [f"| {k} | {s['semanas']} | {s['exemplos_por_janela']} | {s['registros_avaliados']} | {s['registros_bairros_desconhecidos']} |" for k, s in summaries.items()]
    lines += ["", f"Reconciliation: {len(records)} eligible = {total_evaluated} evaluated + {total_unknown} in unknown neighborhoods + {sum(excluded.values())} outside the calendar.", "", "All three windows contain the same neighborhood-week pairs. Retrospective targets are for evaluation; rebuild training targets with the targets subcommand.", "", "No models are trained. Neighborhood membership and inputs use registration dates before the forecast. Registration date is not verified publication date. Recent targets may be incomplete because of reporting delays and the snapshot boundary."]
    (output / "relatorio.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output / "dicionario.md").write_text(DICTIONARY, encoding="utf-8")
    hashes = {p.relative_to(output).as_posix(): sha256(p) for p in sorted(output.rglob("*")) if p.is_file()}
    write_json(output / "manifest.json", dict(version=VERSION, pyarrow_version=pa.__version__, script_sha256=sha256(__file__), source_manifest_sha256=sha256(source / "metadados/manifest.json"), outputs_sha256=hashes))
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


def available_targets(examples, ledger, cutoff):

    contributions = defaultdict(list)
    for b, w, r, n in zip(*(ledger.column(c).to_pylist() for c in ("bairro_chave", "inicio_semana", "data_registro", "quantidade_registros"))):
        if r < cutoff:
            contributions[b, w].append(n)
    examples = examples.filter(pa.array([end < cutoff for end in examples.column("fim_semana").to_pylist()]))
    if "alvo_disponivel" in examples.column_names:
        raise ValueError("Use the original examples file")
    examples = examples.drop(["alvo_retrospectivo"])
    y = [sum(contributions[b, w]) for b, w in zip(examples.column("bairro_chave").to_pylist(), examples.column("inicio_semana").to_pylist())]
    examples = examples.append_column("alvo_disponivel", pa.array(y, type=pa.int32()))
    examples = examples.append_column("data_corte_treinamento", pa.array([cutoff] * len(examples), type=pa.date32()))
    return examples.replace_schema_metadata({b"weekly_rules_version": VERSION.encode(), b"training_cutoff_exclusive": cutoff.isoformat().encode(), b"target_warning": b"Provisional observed counts, not matured final occurrence totals."})


def main():
    parser = argparse.ArgumentParser(description='Build weekly SSP examples and reconstruct targets available at a cutoff.')
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build")
    p.add_argument("--source", type=Path, default=ROOT / "normalized-data")
    p.add_argument("--output", type=Path, default=ROOT / "normalized-data/modelagem")
    p = sub.add_parser("targets")
    p.add_argument("--examples", type=Path, required=True)
    p.add_argument("--ledger", type=Path, default=ROOT / "normalized-data/modelagem/contribuicoes_alvo.parquet")
    p.add_argument("--as-of", type=date.fromisoformat, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        build(args.source.resolve(), args.output.resolve())
    else:
        if args.output.exists():
            parser.error("The destination file already exists")
        table = available_targets(pq.read_table(args.examples), pq.read_table(args.ledger), args.as_of)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, args.output, compression="zstd")
        print(f"{len(table)} examples with targets available before {args.as_of}")


if __name__ == "__main__":
    main()
