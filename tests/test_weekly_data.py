import contextlib
from datetime import date, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest

import pyarrow as pa
import pyarrow.parquet as pq

from scripts.prepare_weekly_data import available_targets, build, origins, sha256, weekly_statistics


class WeeklyDataTests(unittest.TestCase):
    def test_statistics_order_zero_and_trend(self):
        values = [0] * 56 + [1] * 7 + [2] * 7 + [3] * 7 + [4] * 7
        s = weekly_statistics(values, 28)
        self.assertEqual(s['contagem_semana_anterior_01'], 28)
        self.assertEqual(s['contagem_semana_anterior_04'], 7)
        self.assertEqual(s['media_semanal'], 17.5)
        self.assertEqual(s['tendencia_semanal'], 7)
        self.assertEqual(s['dias_sem_registros'], 0)
        self.assertEqual(weekly_statistics([0] * 84, 84)['desvio_padrao_semanal'], 0)

    def test_calendar_boundaries(self):
        splits = {'a': {'start': '2022-01-01', 'end': '2022-12-31'}, 'b': {'start': '2023-01-01', 'end': '2023-01-12'}}
        days = origins(splits, date(2022, 1, 1), date(2023, 1, 12))
        self.assertEqual(min(days), date(2022, 3, 28))
        self.assertNotIn(date(2022, 12, 26), days)
        self.assertIn(date(2023, 1, 2), days)
        self.assertNotIn(date(2023, 1, 9), days)
        self.assertTrue(all(d.weekday() == 0 for d in days))

    def test_build_late_records_unknown_areas_cutoff_and_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source'
            (source / 'registros').mkdir(parents=True)
            (source / 'metadados').mkdir()
            # Registro em 28/03 (igual à previsão) fica indisponível nessa segunda.
            events = [('a', date(2022, 1, 1), date(2022, 1, 1)),
                      ('a', date(2022, 3, 27), date(2022, 3, 28)),
                      ('a', date(2022, 3, 28), date(2022, 4, 5)),
                      ('novo', date(2022, 3, 29), date(2022, 3, 29))]
            schema = pa.schema([('record_id', pa.string()), ('bairro_chave', pa.string()), ('data_ocorrencia', pa.date32()), ('data_registro', pa.date32()), ('elegivel_painel', pa.bool_())])
            path = source / 'registros/source.parquet'
            pq.write_table(pa.Table.from_pylist([dict(record_id=str(i), bairro_chave=b, data_ocorrencia=e, data_registro=r, elegivel_painel=True) for i, (b, e, r) in enumerate(events)], schema=schema), path)
            rules = {'coverage': {'2022': {'start': '2022-01-01', 'end': '2022-04-17'}}, 'splits': {'treino': {'start': '2022-01-01', 'end': '2022-04-17'}}}
            (source / 'metadados/regras.json').write_text(json.dumps(rules), encoding='utf-8')
            (source / 'metadados/manifest.json').write_text(json.dumps({'outputs_sha256': {'registros/source.parquet': sha256(path)}, 'summary': {'registros_elegiveis_paineis': 4}}), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                build(source, root / 'one')
                build(source, root / 'two')
            self.assertEqual((root / 'one/manifest.json').read_bytes(), (root / 'two/manifest.json').read_bytes())
            sample = pq.read_table(root / 'one/treino/janela_28.parquet')
            rows = sample.to_pylist()
            first = [r for r in rows if r['inicio_semana'] == date(2022, 3, 28)]
            self.assertEqual([r['bairro_chave'] for r in first], ['a'])
            self.assertEqual(first[0]['contagem_semana_anterior_01'], 0)
            self.assertEqual(first[0]['alvo_retrospectivo'], 1)
            second = [r for r in rows if r['bairro_chave'] == 'a' and r['inicio_semana'] == date(2022, 4, 4)][0]
            self.assertEqual(second['contagem_semana_anterior_01'], 0)
            self.assertEqual(second['contagem_semana_anterior_02'], 1)
            unknown = pq.read_table(root / 'one/bairros_desconhecidos.parquet').to_pylist()
            self.assertEqual(unknown, [dict(bairro_chave='novo', inicio_semana=date(2022, 3, 28), quantidade_registros=1)])
            ledger = pq.read_table(root / 'one/contribuicoes_alvo.parquet')
            cutoff = available_targets(sample, ledger, date(2022, 4, 4))
            self.assertEqual(len(cutoff), 1)
            self.assertNotIn('alvo_retrospectivo', cutoff.column_names)
            self.assertEqual(cutoff['alvo_disponivel'].to_pylist(), [0])
            self.assertEqual(available_targets(sample, ledger, date(2022, 4, 5))['alvo_disponivel'].to_pylist(), [0])
            self.assertEqual(available_targets(sample, ledger, date(2022, 4, 6))['alvo_disponivel'].to_pylist(), [1])
            report = json.loads((root / 'one/relatorio.json').read_text(encoding='utf-8'))
            self.assertEqual(report['conjuntos']['treino']['registros_bairros_desconhecidos'], 1)


if __name__ == '__main__':
    unittest.main()
