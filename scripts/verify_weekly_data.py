"""Conferência integral dos exemplos e amostra independente dos históricos."""
import argparse
from collections import Counter, defaultdict
from datetime import date, timedelta
import json
import math
from pathlib import Path

import pyarrow.parquet as pq

from prepare_weekly_data import ROOT, WINDOWS, load_records, monday, sha256


def verify(source, output):
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['script_sha256'] == sha256(Path(__file__).with_name('prepare_weekly_data.py'))
    assert manifest['source_manifest_sha256'] == sha256(source / 'metadados/manifest.json')
    for name, expected in manifest['outputs_sha256'].items():
        assert sha256(output / name) == expected, name
    records = load_records(source)
    report = json.loads((output / 'relatorio.json').read_text(encoding='utf-8'))
    rules = json.loads((source / 'metadados/regras.json').read_text(encoding='utf-8'))
    first_seen, targets = {}, Counter()
    for b, event, registered in records:
        first_seen[b] = min(first_seen.get(b, registered), registered)
        targets[b, monday(event)] += 1
    ledger_targets, ledger_keys = Counter(), set()
    for batch in pq.ParquetFile(output / 'contribuicoes_alvo.parquet').iter_batches():
        for row in batch.to_pylist():
            key = row['bairro_chave'], row['inicio_semana'], row['data_registro']
            assert key not in ledger_keys and row['quantidade_registros'] > 0
            ledger_keys.add(key)
            ledger_targets[key[:2]] += row['quantidade_registros']
    assert ledger_targets == targets
    samples = {}
    for split in report['conjuntos']:
        weeks = [date.fromisoformat(r['inicio_semana']) for r in report['semanas'] if r['conjunto'] == split]
        for origin in (weeks[0], weeks[len(weeks) // 2], weeks[-1]):
            daily = defaultdict(Counter)
            for b, event, registered in records:
                if registered < origin and origin - timedelta(days=84) <= event < origin:
                    daily[b][event] += 1
            samples[origin] = daily
    all_weeks = set()
    checked = 0
    for split, summary in report['conjuntos'].items():
        reference_keys = None
        for window in WINDOWS:
            path = output / split / f'janela_{window}.parquet'
            keys, weeks = [], set()
            rows, total, zeros = 0, 0, 0
            previous = None
            for batch in pq.ParquetFile(path).iter_batches(batch_size=8192):
                for row in batch.to_pylist():
                    b, origin, end = row['bairro_chave'], row['inicio_semana'], row['fim_semana']
                    key = (origin, b)
                    assert previous is None or previous < key, ('ordem/duplicata', path, key)
                    previous = key
                    assert origin.weekday() == 0 and end == origin + timedelta(days=6)
                    assert date.fromisoformat(rules['splits'][split]['start']) <= origin <= end <= date.fromisoformat(rules['splits'][split]['end'])
                    assert first_seen[b] < origin
                    assert row['janela_dias'] == window and row['mes'] == origin.month
                    target = row['alvo_retrospectivo']
                    assert target == targets[b, origin] and target >= 0
                    blocks = [row[f'contagem_semana_anterior_{i + 1:02}'] for i in range(window // 7)]
                    assert all(n >= 0 for n in blocks)
                    assert 0 <= row['dias_sem_registros'] <= window
                    assert all(math.isfinite(row[k]) for k in ('media_semanal', 'desvio_padrao_semanal', 'tendencia_semanal'))
                    assert math.isclose(row['media_semanal'], sum(blocks) / len(blocks), abs_tol=1e-12)
                    if origin in samples:
                        counts = samples[origin][b]
                        expected = [sum(counts[origin - timedelta(days=7 * i + j)] for j in range(1, 8)) for i in range(window // 7)]
                        assert blocks == expected, ('historico', origin, b, window)
                        assert row['dias_sem_registros'] == sum(counts[origin - timedelta(days=j)] == 0 for j in range(1, window + 1))
                    keys.append(key)
                    weeks.add(origin)
                    rows += 1
                    total += target
                    zeros += target == 0
            assert rows == summary['exemplos_por_janela']
            assert total == summary['registros_avaliados']
            assert zeros == summary['alvos_zero_por_janela']
            assert len(weeks) == summary['semanas']
            if reference_keys is None:
                assert not all_weeks.intersection(weeks)
                all_weeks.update(weeks)
                reference_keys = keys
            else:
                assert reference_keys == keys, ('janelas divergentes', split)
            checked += rows
    expected_unknown = {(b, w): n for (b, w), n in targets.items() if w in all_weeks and first_seen[b] >= w}
    actual_unknown = {}
    for row in pq.read_table(output / 'bairros_desconhecidos.parquet').to_pylist():
        key = row['bairro_chave'], row['inicio_semana']
        assert key not in actual_unknown
        actual_unknown[key] = row['quantidade_registros']
    assert actual_unknown == expected_unknown
    assert sum(expected_unknown.values()) == sum(s['registros_bairros_desconhecidos'] for s in report['conjuntos'].values())
    assert len(records) == sum(s['registros_avaliados'] + s['registros_bairros_desconhecidos'] for s in report['conjuntos'].values()) + sum(report['registros_fora_calendario'].values())
    print(f'OK: {checked:,} exemplos, {len(all_weeks)} semanas; históricos conferidos em {len(samples)} origens; fontes preservadas.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'normalized-data')
    parser.add_argument('--output', type=Path, default=ROOT / 'normalized-data/modelagem')
    args = parser.parse_args()
    verify(args.source, args.output)
