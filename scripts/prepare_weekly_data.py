"""Exemplos semanais SSP; usa somente PyArrow e a biblioteca padrão."""
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
DICTIONARY = """# Dicionário dos exemplos semanais

## Unidade e elegibilidade

Uma linha por bairro conhecido no início da segunda-feira e semana prevista completa (segunda a domingo). As três janelas usam o mesmo calendário, após 84 dias de cobertura. O cadastro cresce com a primeira data de registro elegível estritamente anterior à previsão; não usa rótulos descobertos no futuro e não exige que o bairro permaneça ativo. As exclusões dos microdados normalizados continuam aplicadas.

## Exemplos

| Campo | Tipo | Significado |
|---|---|---|
| bairro_chave | string | Rótulo textual SSP normalizado; categoria conhecida antes da previsão, sem polígono ou vizinhança oficial. |
| inicio_semana | date32 | Segunda-feira da previsão, no início do dia. Identificador temporal, não preditor direto. |
| fim_semana | date32 | Domingo seguinte, incluído no alvo. |
| janela_dias | int16 | 28, 56 ou 84; configuração do experimento. |
| mes | int8 | Mês da segunda-feira, conhecido antecipadamente. |
| contagem_semana_anterior_01 ... | int32 | Contagens disponíveis de ocorrências em blocos de sete dias; 01 é o bloco mais recente, [início−7, início). Há 4, 8 ou 12 blocos. |
| media_semanal | float64 | Média das contagens nos blocos da janela. |
| desvio_padrao_semanal | float64 | Desvio padrão populacional dos blocos, divisor n (ddof=0). |
| tendencia_semanal | float64 | Inclinação por mínimos quadrados das contagens semanais, em ordem cronológica; unidade registros por semana de avanço. |
| dias_sem_registros | int16 | Dias com zero registros disponíveis na janela. Não significa ausência de crimes. |
| alvo_retrospectivo | int32 | Contagem da semana no snapshot completo disponível. Exclusivamente para avaliação, não para ajuste histórico. |

Todos os atributos históricos exigem data_ocorrencia dentro da janela e data_registro anterior ao início da semana. Zeros anteriores à primeira observação de um bairro não comprovam existência territorial. Não há codificadores ajustados, seleção aprendida, vizinhos geográficos, IBGE ou modelos treinados.

## Reconstrução dos alvos

contribuicoes_alvo.parquet: bairro_chave (string), inicio_semana (date32), data_registro (date32) e quantidade_registros (int32). Soma de contribuições por bairro e semana recupera o alvo retrospectivo. O comando targets soma somente data_registro < data de corte, remove alvo_retrospectivo e semanas ainda abertas, e acrescenta alvo_disponivel (int32) e data_corte_treinamento (date32). Entradas históricas permanecem as calculadas na origem de cada exemplo. O corte é exclusivo, inclusive para semanas e registros na fronteira. Alvos disponíveis são provisórios: ausência de registro na data de corte não comprova ausência de ocorrência. Não há política de maturação imposta nesta preparação.

## Auditoria

bairros_desconhecidos.parquet: bairro_chave (string), inicio_semana (date32) e quantidade_registros (int32) dos alvos de categorias desconhecidas na origem, sem inseri-las artificialmente nas entradas. relatorio.json contém totais por conjunto, semanas e exclusões do calendário. conferencia_semanal.csv detalha os mesmos totais por semana. Cada contagem é reportada uma vez, sem triplicar os registros pelas três janelas.

O manifesto semanal cobre os arquivos desta pasta, exceto ele próprio. O manifesto original continua cobrindo o snapshot anterior preservado. A data de registro é proxy da publicação; não existem snapshots históricos completos das versões dos boletins. Alvos recentes podem estar incompletos por registros tardios fora do snapshot.
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
    """Mesmo calendário para todas as janelas; nunca cruza uma divisão."""
    result = {}
    for split, bounds in splits.items():
        lo, hi = (date.fromisoformat(bounds[k]) for k in ("start", "end"))
        lo, hi = max(lo, start + timedelta(days=84)), min(hi, end)
        d = lo + timedelta(days=(-lo.weekday()) % 7)
        while d + timedelta(days=6) <= hi:
            if d in result:
                raise ValueError("Semanas compartilhadas entre conjuntos")
            result[d] = split
            d += timedelta(days=7)
    return dict(sorted(result.items()))


def load_records(source):
    manifest = json.loads((source / "metadados/manifest.json").read_text(encoding="utf-8"))
    # Conferir também as regras e auditorias evita tratar um snapshot misturado.
    for relative, expected in manifest["outputs_sha256"].items():
        if sha256(source / relative) != expected:
            raise ValueError(f"Assinatura divergente: {relative}")
    records, seen = [], set()
    for path in sorted((source / "registros").glob("*.parquet")):
        data = pq.read_table(path, columns=["record_id", "bairro_chave", "data_ocorrencia", "data_registro", "elegivel_painel"]).to_pydict()
        for rid, b, event, registered, eligible in zip(*data.values()):
            if rid in seen:
                raise ValueError(f"Proveniência repetida: {rid}")
            seen.add(rid)
            if eligible:
                if not b or not event or not registered or event > registered:
                    raise ValueError(f"Registro elegível inconsistente: {rid}")
                records.append((b, event, registered))
    expected = manifest["summary"]["registros_elegiveis_paineis"]
    if len(records) != expected:
        raise ValueError("Total elegível diverge do manifesto")
    return records


def weekly_statistics(daily, window):
    """daily em ordem do mais antigo ao mais recente, com 84 posições."""
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
        raise ValueError("Destino deve estar vazio; use outro diretório para reconstruir e comparar")
    records = load_records(source)
    rules = json.loads((source / "metadados/regras.json").read_text(encoding="utf-8"))
    coverage = sorted((date.fromisoformat(v["start"]), date.fromisoformat(v["end"])) for v in rules["coverage"].values())
    for previous, following in zip(coverage, coverage[1:]):
        if previous[1] + DAY != following[0]:
            raise ValueError("Cobertura precisa ser contínua")
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
                raise ValueError("Falha na reconciliação semanal")
            audit.append(dict(conjunto=split, inicio_semana=origin.isoformat(), bairros_conhecidos=len(known), exemplos_por_janela=len(known), registros_avaliados=evaluated, registros_bairros_desconhecidos=unknown, total_elegivel_semana=total, alvos_zero=zero))
            s = summaries[split]
            for key, value in [("semanas", 1), ("exemplos_por_janela", len(known)), ("alvos_zero_por_janela", zero), ("registros_avaliados", evaluated), ("registros_bairros_desconhecidos", unknown)]:
                s[key] += value
    finally:
        for writer in writers.values():
            writer.close()
    # O relatório detalhado é reconstruído com a primeira data de registro, sem olhar o cadastro futuro.
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
        raise ValueError("Reconciliação global inválida")
    report = dict(version=VERSION, registros_elegiveis=len(records), conjuntos=summaries, registros_fora_calendario=dict(excluded), semanas=audit, disponibilidade="data_registro < inicio_semana; proxy da publicação", alvo="registros por natureza; não crimes únicos", cobertura=[start.isoformat(), end.isoformat()])
    write_json(output / "relatorio.json", report)
    import csv
    with (output / "conferencia_semanal.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(audit[0]))
        writer.writeheader()
        writer.writerows(audit)
    lines = ["# Conferência da base semanal", "", "| Conjunto | Semanas | Exemplos por janela | Registros avaliados | Registros de bairros desconhecidos |", "|---|---:|---:|---:|---:|"]
    lines += [f"| {k} | {s['semanas']} | {s['exemplos_por_janela']} | {s['registros_avaliados']} | {s['registros_bairros_desconhecidos']} |" for k, s in summaries.items()]
    lines += ["", f"Reconciliação: {len(records)} elegíveis = {total_evaluated} avaliados + {total_unknown} de bairros desconhecidos + {sum(excluded.values())} fora do calendário.", "", "As três janelas têm os mesmos pares bairro–semana. Alvos retrospectivos servem à avaliação; reconstruir alvos de treinamento com o subcomando targets.", "", "Não há treinamento de modelos. Cadastro de bairros e entradas respeitam data_registro anterior à previsão. Data de registro não é data comprovada de publicação. Os alvos recentes podem estar incompletos por atraso e pelo término do snapshot."]
    (output / "relatorio.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output / "dicionario.md").write_text(DICTIONARY, encoding="utf-8")
    hashes = {p.relative_to(output).as_posix(): sha256(p) for p in sorted(output.rglob("*")) if p.is_file()}
    write_json(output / "manifest.json", dict(version=VERSION, pyarrow_version=pa.__version__, script_sha256=sha256(__file__), source_manifest_sha256=sha256(source / "metadados/manifest.json"), outputs_sha256=hashes))
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


def available_targets(examples, ledger, cutoff):
    """Exclui semanas ainda abertas e remove o alvo retrospectivo da saída."""
    contributions = defaultdict(list)
    for b, w, r, n in zip(*(ledger.column(c).to_pylist() for c in ("bairro_chave", "inicio_semana", "data_registro", "quantidade_registros"))):
        if r < cutoff:
            contributions[b, w].append(n)
    examples = examples.filter(pa.array([end < cutoff for end in examples.column("fim_semana").to_pylist()]))
    if "alvo_disponivel" in examples.column_names:
        raise ValueError("Use o arquivo original de exemplos")
    examples = examples.drop(["alvo_retrospectivo"])
    y = [sum(contributions[b, w]) for b, w in zip(examples.column("bairro_chave").to_pylist(), examples.column("inicio_semana").to_pylist())]
    examples = examples.append_column("alvo_disponivel", pa.array(y, type=pa.int32()))
    examples = examples.append_column("data_corte_treinamento", pa.array([cutoff] * len(examples), type=pa.date32()))
    return examples.replace_schema_metadata({b"weekly_rules_version": VERSION.encode(), b"training_cutoff_exclusive": cutoff.isoformat().encode(), b"target_warning": b"Provisional observed counts, not matured final occurrence totals."})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
            parser.error("O arquivo de destino já existe")
        table = available_targets(pq.read_table(args.examples), pq.read_table(args.ledger), args.as_of)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, args.output, compression="zstd")
        print(f"{len(table)} exemplos com alvos disponíveis antes de {args.as_of}")


if __name__ == "__main__":
    main()
