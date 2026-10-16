import contextlib
import csv
from datetime import date, datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from lxml import etree
import pyarrow.parquet as pq

from prepare_data import (FIELDS, NS, columns_for, coordinate_pair, file_hash,
                          load_aliases, normalize, parse_date, run, verify)


def serial(day):
    return str((day - date(1899, 12, 30)).days)


def fixture(directory, missing_month=False):
    """Two inline-string sheets with a row gap and deliberately repeated BOs."""
    directory.mkdir()
    headers = [aliases[0] for aliases in FIELDS.values()]
    # Older spelling/position aliases are covered independently below.
    def record(month=1, **values):
        result = {name: 'x' for name in FIELDS}
        result.update(delegacia='DP TESTE', numero_bo=f'BO{month}', ano_bo='2025',
                      municipio_ocorrencia='S.PAULO', natureza='ROUBO - OUTROS',
                      bairro_original='  Jardim  São Luís ',
                      data_ocorrencia_raw=serial(date(2025, month, 1)),
                      data_registro_raw=serial(date(2025, month, 2)),
                      latitude_raw='-23.55', longitude_raw='-46.64',
                      mes_estatistica=str(month), ano_estatistica='2025')
        result.update(values)
        return [result[name] for name in FIELDS]
    first = [record(m) for m in range(1, 7)]
    second = [record(m) for m in range(7, 12 if missing_month else 13)]
    first += [record(), record(natureza='ROUBO DE VEICULO'),
              record(numero_bo='SEMCOORD', latitude_raw='0'),
              record(numero_bo='SEMDATA', data_ocorrencia_raw='lixo'),
              record(numero_bo='SEMBAIRRO', bairro_original='NULL'),
              record(numero_bo='FUTURO', data_ocorrencia_raw=serial(date(2026, 1, 1))),
              record(numero_bo='DATA_TEXTO', data_ocorrencia_raw='2025-01-01'),
              record(numero_bo='OUTRACIDADE', municipio_ocorrencia='CAMPINAS'),
              record(numero_bo='FURTO', natureza='FURTO')]
    path = directory / 'SPDadosCriminais_2025.xlsx'
    workbook = etree.Element(NS + 'workbook', nsmap={None: NS[1:-1], 'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'})
    sheets = etree.SubElement(workbook, NS + 'sheets')
    rels = etree.Element('Relationships', xmlns='http://schemas.openxmlformats.org/package/2006/relationships')
    with ZipFile(path, 'w') as archive:
        for i, (name, data) in enumerate([('JAN-JUN_2025', first), ('JUL-DEZ_2025', second)], 1):
            sheet = etree.SubElement(sheets, NS + 'sheet', name=name, sheetId=str(10 + i))
            sheet.set('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id', f'rId{i}')
            etree.SubElement(rels, 'Relationship', Id=f'rId{i}', Target=f'worksheets/sheet{i}.xml')
            worksheet = etree.Element(NS + 'worksheet'); sheetdata = etree.SubElement(worksheet, NS + 'sheetData')
            for n, values in enumerate([headers] + data, 1):
                actual = n if n == 1 else n + 8
                row = etree.SubElement(sheetdata, NS + 'row', r=str(actual))
                for col, value in enumerate(values):
                    cell = etree.SubElement(row, NS + 'c', r=f'{chr(65 + col)}{actual}', t='inlineStr')
                    etree.SubElement(etree.SubElement(cell, NS + 'is'), NS + 't').text = value
            archive.writestr(f'xl/worksheets/sheet{i}.xml', etree.tostring(worksheet))
        archive.writestr('xl/workbook.xml', etree.tostring(workbook))
        archive.writestr('xl/_rels/workbook.xml.rels', etree.tostring(rels))
    return path


class TreatmentTests(unittest.TestCase):
    def test_dates_headers_coordinates_and_names(self):
        self.assertEqual(normalize('  São   Luís '), 'SAO LUIS')
        self.assertEqual(parse_date('46023')[0], date(2026, 1, 1))
        self.assertEqual(parse_date('0', True)[0], date(1904, 1, 1))
        self.assertEqual(parse_date('2025-01-02T12:30:00')[0], date(2025, 1, 2))
        for value, status in [('NULL', 'ausente'), ('nan', 'invalida'), ('abc', 'invalida')]:
            self.assertEqual(parse_date(value)[1], status)
        headers = {str(i): values[0] for i, values in enumerate(FIELDS.values())}
        headers['1'] = 'CIDADE'; headers['4'] = 'DATA_COMUNICACAO_BO'
        self.assertEqual(columns_for(headers)['municipio_registro'], '1')
        headers['extra'] = 'NOME_MUNICIPIO'
        with self.assertRaises(ValueError): columns_for(headers)
        del headers['extra']; del headers['0']
        with self.assertRaises(ValueError): columns_for(headers)
        bbox = [-24.1, -23.2, -47.1, -46.2]
        for lat, lon, expected in [('0','0','zero'), ('NULL','0','ausente'), ('nan','-46','invalida'),
                                    ('91','0','fora_limites_numericos'), ('-22','-46','suspeita_bbox'),
                                    ('-23.55','-46.64','plausivel')]:
            self.assertEqual(coordinate_pair(lat, lon, bbox)[2], expected)

    def test_aliases_require_evidence_and_no_chains(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'aliases.csv'
            path.write_text('origem,destino,justificativa\nJD A,JARDIM A,revisão manual\n', encoding='utf-8')
            self.assertEqual(load_aliases(path, {'NULL'}), {'JD A':'JARDIM A'})
            path.write_text('origem,destino,justificativa\nA,B,\n', encoding='utf-8')
            with self.assertRaises(ValueError): load_aliases(path, {'NULL'})
            path.write_text('origem,destino,justificativa\nA,B,ok\nB,C,ok\n', encoding='utf-8')
            with self.assertRaises(ValueError): load_aliases(path, {'NULL'})

    def integration(self, missing_month=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        original = fixture(root / 'raw', missing_month)
        baseline = root / 'baseline'
        baseline.mkdir()
        (baseline / (original.stem + '.json')).write_text(json.dumps({
            'rows': 21 - int(missing_month), 'robbery_city': 19 - int(missing_month),
            'robbery_occurrence_year': {'2025': 16 - int(missing_month), '2026': 1}}))
        before = file_hash(original)
        output = root / 'processed'
        project = Path(__file__).resolve().parents[1]
        with contextlib.redirect_stdout(io.StringIO()):
            summary = run(original.parent, output, project/'config/ssp_rules.json',
                          project/'config/bairro_aliases.csv', baseline)
        self.assertEqual(before, file_hash(original))
        rows = pq.read_table(output/'registros/SPDadosCriminais_2025.parquet').to_pylist()
        self.assertEqual(len(rows), 19 if not missing_month else 18)
        self.assertEqual(len({r['record_id'] for r in rows}), len(rows))
        self.assertEqual(rows[0]['linha_origem'], 10)
        self.assertTrue(any(r['ocorrencia_posterior_registro'] for r in rows))
        self.assertTrue(any(r['coordenadas_status']=='zero' for r in rows))
        self.assertEqual(summary['duplicates']['removed_records'], 0)
        self.assertGreater(summary['duplicates']['exact_groups'], 0)
        self.assertGreater(summary['duplicates']['bo_groups_multiple_natures'], 0)
        self.assertEqual(summary['reconciliation']['sem_data'], 1)
        self.assertEqual(summary['reconciliation']['com_data_sem_bairro'], 1)
        self.assertEqual(summary['reconciliation']['com_data_e_bairro_fora_cobertura'], 1)
        source_stats = json.loads((output/'registros/SPDadosCriminais_2025.json').read_text(encoding='utf-8'))
        self.assertTrue(source_stats['baseline_verified'])
        self.assertEqual(source_stats['quality']['data_ocorrencia_texto_recuperada'], 1)
        panel = pq.read_table(output/'analise/bairro_dia_2025.parquet').to_pylist()
        keys = [(r['bairro_chave'],r['data']) for r in panel]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(r['quantidade_registros'] >= 0 for r in panel))
        self.assertEqual(sum(r['quantidade_registros'] for r in panel), len(rows)-3)
        with contextlib.redirect_stdout(io.StringIO()):
            first_manifest = verify(output)
            repeated = run(original.parent, output, project/'config/ssp_rules.json',
                           project/'config/bairro_aliases.csv', baseline)
            second_manifest = verify(output)
        self.assertEqual(summary, repeated)
        self.assertEqual(first_manifest['outputs_sha256'], second_manifest['outputs_sha256'])
        rebuilt_output = root / 'rebuilt'
        with contextlib.redirect_stdout(io.StringIO()):
            rebuilt = run(original.parent, rebuilt_output, project/'config/ssp_rules.json',
                          project/'config/bairro_aliases.csv', baseline)
        self.assertEqual(summary, rebuilt)
        for relative in first_manifest['outputs_sha256']:
            if relative.endswith('.parquet'):
                self.assertEqual(file_hash(output/relative), file_hash(rebuilt_output/relative))
        (output/'relatorios/resumo.md').write_text('alterado', encoding='utf-8')
        with self.assertRaises(ValueError): verify(output)
        return summary, panel

    def test_reconciliation_zero_fill_duplicates_and_repeatability(self):
        summary, panel = self.integration()
        self.assertEqual(len(panel), 365)
        self.assertTrue(summary['panels'][0]['coverage_confirmed'])
        self.assertTrue(any(r['quantidade_registros'] == 0 for r in panel))

    def test_unconfirmed_coverage_never_zero_fills(self):
        summary, panel = self.integration(missing_month=True)
        self.assertFalse(summary['panels'][0]['coverage_confirmed'])
        self.assertTrue(all(r['quantidade_registros'] > 0 for r in panel))


if __name__ == '__main__':
    unittest.main()
