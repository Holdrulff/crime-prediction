"""SSP-SP -> auditable records and neighborhood/day counts. Never deduplicate."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import posixpath
import re
import shutil
import tempfile
import unicodedata
from zipfile import ZipFile

from lxml import etree
import pyarrow as pa
import pyarrow.parquet as pq

from eda_preflight import NS, date_from_excel, rows, shared_strings

ROOT = Path(__file__).resolve().parent
BATCH = 25_000
FIELDS = {
    'delegacia': ('NOME_DELEGACIA',),
    'municipio_registro': ('NOME_MUNICIPIO', 'CIDADE'),
    'numero_bo': ('NUM_BO',), 'ano_bo': ('ANO_BO',),
    'data_registro_raw': ('DATA_REGISTRO', 'DATA_COMUNICACAO_BO'),
    'data_ocorrencia_raw': ('DATA_OCORRENCIA_BO',),
    'hora_ocorrencia_raw': ('HORA_OCORRENCIA_BO',),
    'bairro_original': ('BAIRRO',),
    'municipio_ocorrencia': ('NOME_MUNICIPIO_CIRCUNCRICAO', 'NOME_MUNICIPIO_CIRCUNSCRICAO'),
    'natureza': ('NATUREZA_APURADA',), 'rubrica': ('RUBRICA',),
    'conduta': ('DESCR_CONDUTA',), 'subtipo_local': ('DESCR_SUBTIPOLOCAL',),
    'latitude_raw': ('LATITUDE',), 'longitude_raw': ('LONGITUDE',),
    'mes_estatistica': ('MES_ESTATISTICA',), 'ano_estatistica': ('ANO_ESTATISTICA',),
}
REQUIRED = set(FIELDS)
STRING_FIELDS = ['record_id', 'arquivo_origem', 'aba_origem', 'delegacia',
                 'municipio_registro', 'numero_bo', 'ano_bo', 'data_registro_raw',
                 'data_ocorrencia_raw', 'hora_ocorrencia_raw', 'bairro_original',
                 'bairro_normalizado', 'bairro_chave', 'municipio_ocorrencia',
                 'natureza', 'rubrica', 'conduta', 'subtipo_local', 'latitude_raw',
                 'longitude_raw', 'coordenadas_status', 'bo_chave', 'row_fingerprint']
SCHEMA = pa.schema([(k, pa.string()) for k in STRING_FIELDS] + [
    ('linha_origem', pa.int32()), ('ano_arquivo', pa.int16()),
    ('data_ocorrencia', pa.date32()), ('data_registro', pa.date32()),
    ('data_ocorrencia_status', pa.string()), ('data_registro_status', pa.string()),
    ('ocorrencia_posterior_registro', pa.bool_()), ('atraso_registro_dias', pa.int32()),
    ('bairro_ausente', pa.bool_()), ('latitude', pa.float64()), ('longitude', pa.float64()),
])
PANEL_SCHEMA = pa.schema([
    ('bairro_chave', pa.string()), ('data', pa.date32()), ('quantidade_registros', pa.int32()),
    ('dia_semana', pa.int8()), ('mes', pa.int8()), ('ano', pa.int16()),
    ('cobertura_confirmada', pa.bool_()), ('periodo_parcial', pa.bool_()),
])


def normalize(value):
    text = unicodedata.normalize('NFKD', str(value or ''))
    return ' '.join(''.join(c for c in text if not unicodedata.combining(c)).upper().split())


def header(value):
    return re.sub(r'[^A-Z0-9]+', '_', normalize(value)).strip('_')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def write_csv(path, names, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=names)
        writer.writeheader()
        writer.writerows(records)


def columns_for(row):
    names = defaultdict(list)
    for column, name in row.items():
        names[header(name)].append(column)
    result = {}
    for field, aliases in FIELDS.items():
        matches = [col for alias in aliases for col in names.get(alias, [])]
        if len(matches) != 1:
            raise ValueError(f'Campo obrigatório ausente ou ambíguo: {field}: {matches}')
        result[field] = matches[0]
    return result


def load_aliases(path, missing):
    result = {}
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['origem', 'destino', 'justificativa']:
            raise ValueError('Cabeçalho inválido no arquivo de equivalências de bairros')
        for row in reader:
            a, b = normalize(row['origem']), normalize(row['destino'])
            if not a or not b or a in missing or b in missing or a == b or not row['justificativa'].strip() or a in result:
                raise ValueError(f'Equivalência inválida: {row}')
            result[a] = b
    if set(result) & set(result.values()):
        raise ValueError('Equivalências encadeadas ou cíclicas não são aceitas; use destinos finais')
    return result


def parse_date(value, epoch1904=False):
    if normalize(value) in {'', 'NULL'}:
        return None, 'ausente'
    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Data não finita')
        base = datetime(1904, 1, 1) if epoch1904 else datetime(1899, 12, 30)
        return (base + timedelta(days=number)).date(), 'valida'
    except (ValueError, TypeError, OverflowError):
        try:
            return datetime.fromisoformat(value).date(), 'valida'
        except (ValueError, TypeError):
            return None, 'invalida'


def coordinate_pair(lat_raw, lon_raw, bbox):
    if normalize(lat_raw) in {'', 'NULL'} or normalize(lon_raw) in {'', 'NULL'}:
        return None, None, 'ausente'
    try:
        lat, lon = float(lat_raw), float(lon_raw)
        if not math.isfinite(lat) or not math.isfinite(lon):
            raise ValueError('Coordenada não finita')
    except (TypeError, ValueError):
        return None, None, 'invalida'
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return lat, lon, 'fora_limites_numericos'
    if lat == 0 or lon == 0:
        return lat, lon, 'zero'
    a, b, c, d = bbox
    return lat, lon, 'plausivel' if a < lat < b and c < lon < d else 'suspeita_bbox'


def workbook_sheets(archive):
    workbook = etree.fromstring(archive.read('xl/workbook.xml'))
    relations = etree.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
    targets = {r.get('Id'): r.get('Target') for r in relations}
    result = []
    for sheet in workbook.find(NS + 'sheets'):
        rid = sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
        target = targets[rid]
        part = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        match = re.fullmatch(r'xl/worksheets/sheet(\d+)\.xml', part)
        if not match:
            raise ValueError(f'Planilha não suportada: {part}')
        result.append((sheet.get('name'), int(match[1])))
    properties = workbook.find(NS + 'workbookPr')
    epoch1904 = properties is not None and properties.get('date1904') in {'1', 'true'}
    return result, epoch1904


def integer(value):
    try:
        number = float(value)
        return int(number) if math.isfinite(number) and number.is_integer() else None
    except (ValueError, TypeError):
        return None


def transform(raw, columns, raw_headers, source_hash, filename, sheet, row_number, year, rules, aliases, epoch1904):
    selected = {field: raw.get(col) for field, col in columns.items()}
    if normalize(selected['municipio_ocorrencia']) not in rules['city_values'] or not normalize(selected['natureza']).startswith('ROUBO'):
        return None
    occurrence, occurrence_status = parse_date(selected['data_ocorrencia_raw'], epoch1904)
    registered, registered_status = parse_date(selected['data_registro_raw'], epoch1904)
    bairro = normalize(selected['bairro_original'])
    missing = bairro in rules['missing_neighborhood_values']
    lat, lon, coord_status = coordinate_pair(selected['latitude_raw'], selected['longitude_raw'], rules['plausible_bbox'])
    bo_year = integer(selected['ano_bo'])
    bo_number, station = normalize(selected['numero_bo']), normalize(selected['delegacia'])
    bo_key = digest([station, bo_year, bo_number]) if bo_year and station not in {'', 'NULL'} and bo_number not in {'', 'NULL'} else None
    record = {k: selected.get(k) for k in STRING_FIELDS}
    record.update(
        record_id=digest([source_hash, sheet, row_number]), arquivo_origem=filename,
        aba_origem=sheet, linha_origem=row_number, ano_arquivo=year,
        bairro_normalizado=None if missing else bairro,
        bairro_chave=None if missing else aliases.get(bairro, bairro), bairro_ausente=missing,
        data_ocorrencia=occurrence, data_registro=registered,
        data_ocorrencia_status=occurrence_status, data_registro_status=registered_status,
        ocorrencia_posterior_registro=bool(occurrence and registered and occurrence > registered),
        atraso_registro_dias=(registered - occurrence).days if occurrence and registered else None,
        latitude=lat, longitude=lon, coordenadas_status=coord_status, bo_chave=bo_key,
        row_fingerprint=digest({header(name): raw.get(col) for col, name in raw_headers.items()}),
    )
    return record


def ingest(path, stage, rules, aliases, pipeline_hash, old_output, baseline_dir):
    year_match = re.fullmatch(r'SPDadosCriminais_(\d{4})\.xlsx', path.name)
    if not year_match or year_match[1] not in rules['coverage']:
        raise ValueError(f'Arquivo sem cobertura declarada: {path.name}')
    year = int(year_match[1])
    signature = file_hash(path)
    relative = Path('registros') / (path.stem + '.parquet')
    meta_relative = relative.with_suffix('.json')
    output = stage / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    old_meta = old_output / meta_relative
    if old_meta.exists() and (old_output / relative).exists():
        cached = read_json(old_meta)
        if cached['source_sha256'] == signature and cached['pipeline_sha256'] == pipeline_hash and cached['parquet_sha256'] == file_hash(old_output / relative):
            shutil.copy2(old_output / relative, output)
            write_json(stage / meta_relative, cached)
            print(f'{path.name}: cache validado; {cached["selected_rows"]:,} registros', flush=True)
            return cached
    coverage = rules['coverage'][str(year)]
    expected_months = set(range(date.fromisoformat(coverage['start']).month, date.fromisoformat(coverage['end']).month + 1))
    months, stat_issues = set(), Counter()
    stats = {'file': path.name, 'source_sha256': signature, 'pipeline_sha256': pipeline_hash,
             'processed_at_utc': datetime.now(timezone.utc).isoformat(), 'source_rows': 0,
             'selected_rows': 0, 'sheets': [], 'quality': Counter(), 'occurrence_years': Counter(),
             'legacy_occurrence_years': Counter()}
    buffer = []
    metadata = {b'rules_version': rules['version'].encode(), b'pipeline_sha256': pipeline_hash.encode(), b'source_sha256': signature.encode()}
    with pq.ParquetWriter(output, SCHEMA.with_metadata(metadata), compression='zstd') as writer, ZipFile(path) as archive:
        sheets, epoch1904 = workbook_sheets(archive)
        found = {name for name, _ in sheets if name != 'CAMPOS_DA_TABELA_SPDADOS' and normalize(name).startswith(('JAN-JUN_', 'JUL-DEZ_'))}
        if found != set(coverage['sheets']):
            raise ValueError(f'Abas não correspondem à cobertura declarada: {path.name}: {found}')
        strings = shared_strings(archive)
        for sheet_name, sheet_number in sheets:
            if sheet_name not in found:
                continue
            iterator = rows(archive, sheet_number, strings, with_row_numbers=True)
            _, headers = next(iterator)
            columns = columns_for(headers)
            stats['sheets'].append({'name': sheet_name, 'columns': columns})
            for row_number, raw in iterator:
                stats['source_rows'] += 1
                month = integer(raw.get(columns['mes_estatistica']))
                stat_year = integer(raw.get(columns['ano_estatistica']))
                if stat_year == year and month in expected_months:
                    months.add(month)
                else:
                    stat_issues['outside_declared_statistical_period'] += 1
                record = transform(raw, columns, headers, signature, path.name, sheet_name,
                                   row_number, year, rules, aliases, epoch1904)
                if record is not None:
                    buffer.append(record)
                    stats['selected_rows'] += 1
                    quality = stats['quality']
                    quality['data_ocorrencia_' + record['data_ocorrencia_status']] += 1
                    quality['data_registro_' + record['data_registro_status']] += 1
                    quality['coordenadas_' + record['coordenadas_status']] += 1
                    quality['bairro_ausente'] += record['bairro_ausente']
                    quality['ocorrencia_posterior_registro'] += record['ocorrencia_posterior_registro']
                    quality['bo_sem_chave'] += record['bo_chave'] is None
                    legacy_date = date_from_excel(record['data_ocorrencia_raw'])
                    if legacy_date:
                        stats['legacy_occurrence_years'][str(legacy_date.year)] += 1
                    elif record['data_ocorrencia']:
                        quality['data_ocorrencia_texto_recuperada'] += 1
                    if record['data_ocorrencia']:
                        stats['occurrence_years'][str(record['data_ocorrencia'].year)] += 1
                if len(buffer) >= BATCH:
                    writer.write_table(pa.Table.from_pylist(buffer, schema=SCHEMA))
                    buffer.clear()
                if stats['source_rows'] % 250_000 == 0:
                    print(f'{path.name}: {stats["source_rows"]:,} linhas lidas', flush=True)
            print(f'{path.name}: aba {sheet_name} concluída', flush=True)
        if buffer:
            writer.write_table(pa.Table.from_pylist(buffer, schema=SCHEMA))
    stats['coverage'] = {**coverage, 'observed_statistical_months': sorted(months),
                         'confirmed': months == expected_months and not stat_issues,
                         'issues': dict(stat_issues), 'partial_year': coverage['end'][5:] != '12-31'}
    baseline = baseline_dir / (path.stem + '.json')
    stats['baseline_verified'] = False
    if baseline.exists():
        old = read_json(baseline)
        if stats['source_rows'] != old['rows'] or stats['selected_rows'] != old['robbery_city'] or dict(stats['legacy_occurrence_years']) != old['robbery_occurrence_year']:
            raise ValueError(f'Contagens divergem da análise anterior: {path.name}; '
                             f'linhas={stats["source_rows"]}/{old["rows"]}; '
                             f'selecionadas={stats["selected_rows"]}/{old["robbery_city"]}; '
                             f'anos_originais={dict(stats["legacy_occurrence_years"])}; '
                             f'anos_eda={old["robbery_occurrence_year"]}')
        stats['baseline_verified'] = True
    stats['parquet_sha256'] = file_hash(output)
    write_json(stage / meta_relative, stats)
    # Keep a validated annual checkpoint if a later source fails. The final
    # manifest remains the gate for accepting a complete dataset.
    checkpoint = old_output / relative
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(output, checkpoint.with_suffix('.parquet.tmp'))
    checkpoint.with_suffix('.parquet.tmp').replace(checkpoint)
    write_json(old_output / meta_relative.with_suffix('.json.tmp'), stats)
    (old_output / meta_relative.with_suffix('.json.tmp')).replace(old_output / meta_relative)
    print(f'{path.name}: {stats["selected_rows"]:,} selecionados; cobertura confirmada={stats["coverage"]["confirmed"]}', flush=True)
    return stats


def batches(directory, columns=None):
    for path in sorted(directory.glob('*.parquet')):
        yield from pq.ParquetFile(path).iter_batches(batch_size=BATCH, columns=columns)


def aggregate(stage, sources, rules):
    counts, municipal, year_totals = Counter(), defaultdict(Counter), Counter()
    exact, bos, labels = Counter(), Counter(), Counter()
    reconciled = Counter()
    for batch in batches(stage / 'registros', ['data_ocorrencia', 'bairro_chave', 'bairro_original', 'row_fingerprint', 'bo_chave']):
        for row in batch.to_pylist():
            day, bairro = row['data_ocorrencia'], row['bairro_chave']
            exact[row['row_fingerprint']] += 1
            if row['bo_chave']:
                bos[row['bo_chave']] += 1
            labels[(row['bairro_original'], bairro)] += 1
            if day is None:
                reconciled['sem_data'] += 1
                continue
            year_totals[day.year] += 1
            municipal[day]['total'] += 1
            municipal[day]['sem_bairro' if bairro is None else 'com_bairro'] += 1
            if bairro is None:
                reconciled['com_data_sem_bairro'] += 1
            else:
                counts[(bairro, day)] += 1
                reconciled['com_data_e_bairro'] += 1
    assert sum(reconciled.values()) == sum(s['selected_rows'] for s in sources)
    assert sum(counts.values()) == reconciled['com_data_e_bairro']
    analysis, reports = stage / 'analise', stage / 'relatorios'
    analysis.mkdir(exist_ok=True); reports.mkdir(exist_ok=True)
    write_csv(reports / 'bairros_observados.csv', ['bairro_original', 'bairro_chave', 'quantidade'],
              ({'bairro_original': raw, 'bairro_chave': key, 'quantidade': n}
               for (raw, key), n in sorted(labels.items(), key=lambda x: (x[0][1] or '', x[0][0] or ''))))
    write_csv(reports / 'totais_ano_ocorrencia.csv', ['ano', 'quantidade'],
              ({'ano': y, 'quantidade': n} for y, n in sorted(year_totals.items())))
    write_csv(reports / 'totais_diarios_municipio.csv', ['data', 'total', 'com_bairro', 'sem_bairro'],
              ({'data': d.isoformat(), **v} for d, v in sorted(municipal.items())))
    panel_report = []
    for source in sources:
        cov = source['coverage']
        start, end = date.fromisoformat(cov['start']), date.fromisoformat(cov['end'])
        year = start.year
        neighborhoods = sorted({b for b, d in counts if start <= d <= end})
        path = analysis / f'bairro_dia_{year}.parquet'
        expected_total = sum(n for (b, d), n in counts.items() if start <= d <= end)
        total, panel_rows, zero_rows = 0, 0, 0
        buffer = []
        with pq.ParquetWriter(path, PANEL_SCHEMA, compression='zstd') as writer:
            for bairro in neighborhoods:
                day = start
                while day <= end:
                    quantity = counts.get((bairro, day), 0)
                    if quantity or cov['confirmed']:
                        buffer.append(dict(bairro_chave=bairro, data=day, quantidade_registros=quantity,
                                           dia_semana=day.weekday(), mes=day.month, ano=day.year,
                                           cobertura_confirmada=cov['confirmed'], periodo_parcial=cov['partial_year']))
                        total += quantity; panel_rows += 1; zero_rows += quantity == 0
                    if len(buffer) >= BATCH:
                        writer.write_table(pa.Table.from_pylist(buffer, schema=PANEL_SCHEMA)); buffer.clear()
                    day += timedelta(days=1)
            if buffer:
                writer.write_table(pa.Table.from_pylist(buffer, schema=PANEL_SCHEMA))
        assert total == expected_total
        panel_report.append({'year': year, 'main_period': year in rules['main_years'],
                             'neighborhoods': len(neighborhoods), 'rows': panel_rows, 'zero_rows': zero_rows,
                             'records': total, 'coverage_confirmed': cov['confirmed'], 'partial': cov['partial_year']})
    covered_total = sum(p['records'] for p in panel_report)
    reconciled['com_data_e_bairro_fora_cobertura'] = reconciled['com_data_e_bairro'] - covered_total
    reconciled['nos_paineis'] = covered_total
    duplicate_report = audit_duplicates(stage, exact, bos)
    return {'selected_rows': sum(s['selected_rows'] for s in sources),
            'reconciliation': dict(reconciled), 'panels': panel_report,
            'occurrence_years': dict(sorted(year_totals.items())), 'duplicates': duplicate_report}


def audit_duplicates(stage, exact, bos):
    repeated_exact = {key for key, n in exact.items() if n > 1}
    repeated_bo = {key for key, n in bos.items() if n > 1}
    bo_natures = defaultdict(set)
    for batch in batches(stage / 'registros', ['bo_chave', 'natureza']):
        for row in batch.to_pylist():
            if row['bo_chave'] in repeated_bo:
                bo_natures[row['bo_chave']].add(row['natureza'])
    columns = ['record_id', 'arquivo_origem', 'aba_origem', 'linha_origem', 'numero_bo', 'ano_bo',
               'delegacia', 'natureza', 'rubrica', 'conduta', 'bo_chave', 'row_fingerprint']
    candidate_schema = pa.schema([SCHEMA.field(c) for c in columns] + [
        ('linha_repetida', pa.bool_()), ('bo_repetido', pa.bool_()), ('bo_multiplas_naturezas', pa.bool_())])
    members = 0
    with pq.ParquetWriter(stage / 'relatorios' / 'candidatos_duplicacao.parquet', candidate_schema, compression='zstd') as writer:
        for batch in batches(stage / 'registros', columns):
            candidates = []
            for row in batch.to_pylist():
                same_row, same_bo = row['row_fingerprint'] in repeated_exact, row['bo_chave'] in repeated_bo
                if same_row or same_bo:
                    row.update(linha_repetida=same_row, bo_repetido=same_bo,
                               bo_multiplas_naturezas=len(bo_natures.get(row['bo_chave'], ())) > 1)
                    candidates.append(row)
            if candidates:
                writer.write_table(pa.Table.from_pylist(candidates, schema=candidate_schema))
                members += len(candidates)
    write_csv(stage / 'relatorios' / 'grupos_repetidos.csv', ['tipo', 'chave', 'quantidade', 'multiplas_naturezas'],
              [dict(tipo='linha', chave=k, quantidade=exact[k], multiplas_naturezas=False) for k in sorted(repeated_exact)] +
              [dict(tipo='bo', chave=k, quantidade=bos[k], multiplas_naturezas=len(bo_natures[k]) > 1) for k in sorted(repeated_bo)])
    return {'exact_groups': len(repeated_exact), 'bo_groups': len(repeated_bo),
            'bo_groups_multiple_natures': sum(len(v) > 1 for v in bo_natures.values()),
            'candidate_records': members, 'removed_records': 0}


def dictionary(stage):
    descriptions = {
        'record_id': 'SHA256 de arquivo original, aba e número real da linha; identifica proveniência, não uma ocorrência única.',
        'data_ocorrencia': 'Data do fato; sem data válida não entra no painel diário.',
        'data_registro': 'Data do registro/comunicação. Preservar para controlar disponibilidade histórica antes de prever.',
        'bairro_original': 'Texto da SSP preservado; não é uma unidade territorial oficial.',
        'bairro_normalizado': 'Espaços uniformes, maiúsculas e acentos removidos; NULL quando ausente.',
        'bairro_chave': 'Nome normalizado ou destino de equivalência revisada; unidade textual usada na agregação.',
        'bo_chave': 'SHA256 de delegacia normalizada, ano e número do BO. Chave candidata, não prova de unicidade.',
        'row_fingerprint': 'SHA256 de todos os campos originais da linha com cabeçalhos normalizados. Não exclui registros.',
        'coordenadas_status': 'ausente, invalida, zero, fora_limites_numericos, suspeita_bbox ou plausivel. Bbox não confirma limites municipais.',
        'quantidade_registros': 'Contagem de linhas selecionadas preservadas. Zero é ausência de registros na base coberta, não ausência de crime.',
        'dia_semana': '0=segunda, 1=terça, ..., 6=domingo.',
        'cobertura_confirmada': 'Abas correspondem ao intervalo declarado e meses estatísticos esperados estão presentes, sem registros estatísticos fora do intervalo.',
        'periodo_parcial': 'True para cobertura inferior ao ano completo; True em 2026.',
        'ocorrencia_posterior_registro': 'Sinalização temporal; linha preservada para revisão.',
        'atraso_registro_dias': 'Data de registro menos data de ocorrência; negativo indica inconsistência.',
    }
    def describe(field):
        if field.name in descriptions:
            return descriptions[field.name]
        if field.name.endswith('_raw'):
            return 'Valor original da célula, antes da conversão; datas numéricas são seriais do Excel.'
        if field.name in FIELDS:
            return 'Valor original da SSP; origem: ' + ' ou '.join(FIELDS[field.name]) + '.'
        return {'arquivo_origem': 'Nome do XLSX de origem.', 'aba_origem': 'Nome real da aba.',
                'linha_origem': 'Número real da linha no Excel, começando em 1.',
                'ano_arquivo': 'Ano no nome do arquivo; não substitui o ano da ocorrência.',
                'bairro_ausente': 'Indica ausência de bairro conforme sentinelas nas regras.',
                'data_ocorrencia_status': 'valida, ausente ou invalida.', 'data_registro_status': 'valida, ausente ou invalida.',
                'latitude': 'Latitude convertida; avaliar em conjunto com coordenadas_status.',
                'longitude': 'Longitude convertida; avaliar em conjunto com coordenadas_status.',
                'data': 'Dia da ocorrência representado na agregação.', 'mes': 'Mês de 1 a 12.', 'ano': 'Ano da ocorrência.'}[field.name]
    lines = ['# Dicionário dos dados tratados', '', '## Registros selecionados', '', '| Campo | Tipo | Significado |', '|---|---|---|']
    lines += [f'| {f.name} | {f.type} | {describe(f)} |' for f in SCHEMA]
    lines += ['', '## Painéis por bairro e dia', '', '| Campo | Tipo | Significado |', '|---|---|---|']
    lines += [f'| {f.name} | {f.type} | {describe(f)} |' for f in PANEL_SCHEMA]
    (stage / 'relatorios' / 'dicionario.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def report_markdown(stage, summary, sources):
    lines = ['# Resultado do tratamento SSP', '', f'Registros selecionados e preservados: **{summary["selected_rows"]:,}**.',
             'Nenhuma linha foi removida por duplicação ou ausência de coordenadas.', '',
             '## Arquivos de origem', '', '| Arquivo | Linhas lidas | Selecionadas | Cobertura confirmada | Conferência EDA |', '|---|---:|---:|---|---|']
    lines += [f'| {s["file"]} | {s["source_rows"]} | {s["selected_rows"]} | {s["coverage"]["confirmed"]} | {s["baseline_verified"]} |' for s in sources]
    lines += ['', '## Reconciliação', '', 'As três primeiras categorias abaixo são disjuntas. As demais detalham a categoria com data e bairro.', '']
    for key in ['sem_data', 'com_data_sem_bairro', 'com_data_e_bairro', 'nos_paineis', 'com_data_e_bairro_fora_cobertura']:
        lines.append(f'- {key}: {summary["reconciliation"].get(key, 0)}')
    lines += ['', '## Painéis', '', '| Ano | Bairros textuais | Linhas bairro-dia | Registros | Zeros | Cobertura confirmada |', '|---|---:|---:|---:|---:|---|']
    lines += [f'| {p["year"]} | {p["neighborhoods"]} | {p["rows"]} | {p["records"]} | {p["zero_rows"]} | {p["coverage_confirmed"]} |' for p in summary['panels']]
    lines += ['', '## Qualidade por arquivo', '']
    for s in sources:
        lines.append(f'### {s["file"]}')
        lines += [f'- {k}: {v}' for k, v in sorted(s['quality'].items())]
        lines.append('')
    lines += ['## Candidatos a duplicação', '']
    lines += [f'- {k}: {v}' for k, v in summary['duplicates'].items()]
    lines += ['', '## Limites de interpretação', '',
              '- As chaves de bairros são rótulos da SSP, não limites geográficos oficiais.',
              '- A presença de todos os meses confirma a cobertura declarada dos arquivos, não completude do registro de crimes nem de cada bairro.',
              '- O universo de bairros de cada painel é o conjunto observado naquele intervalo; sua existência em todos os dias é uma hipótese de trabalho.',
              '- Zeros fora da cobertura não são inventados; painel sem cobertura confirmada contém somente pares observados.',
              '- 2026 cobre janeiro a junho; ocorrências fora dessa janela permanecem nos registros e resumos.',
              '- Os totais históricos incluem registros tardios encontrados nos arquivos posteriores. O painel não é um retrato do que era conhecido em cada data.',
              '- Antes de prever, filtrar disponibilidade pela data de registro e revisar registros sem data de registro, datas inconsistentes e candidatos a duplicação.',
              '- Não houve integração com IBGE, treino de modelos, geocodificação ou inferência automática de bairros.']
    (stage / 'relatorios' / 'resumo.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def verify(output):
    manifest = read_json(output / 'manifest.json')
    for relative, expected in manifest['outputs_sha256'].items():
        if file_hash(output / relative) != expected:
            raise ValueError(f'Artefato ausente ou alterado: {relative}')
    print(f'Verificação OK: {len(manifest["outputs_sha256"])} artefatos íntegros', flush=True)
    return manifest


def ingest_task(arguments):
    return ingest(*arguments)


def run(input_dir, output, rules_path, aliases_path, baseline_dir, workers=1):
    input_dir, output = input_dir.resolve(), output.resolve()
    if output.is_relative_to(input_dir) or input_dir.is_relative_to(output):
        raise ValueError('A saída não pode conter nem estar dentro dos originais')
    rules = read_json(rules_path)
    aliases = load_aliases(aliases_path, set(rules['missing_neighborhood_values']))
    paths = sorted(input_dir.glob('SPDadosCriminais_*.xlsx'))
    if not paths:
        raise ValueError('Nenhuma planilha encontrada')
    versions = {'pyarrow': pa.__version__, 'lxml': etree.LXML_VERSION, 'python': __import__('platform').python_version()}
    pipeline_hash = digest([file_hash(Path(__file__)), file_hash(ROOT / 'eda_preflight.py'),
                            file_hash(rules_path), file_hash(aliases_path), versions])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ssp-', dir=output.parent) as tmp:
        stage = Path(tmp)
        tasks = [(path, stage, rules, aliases, pipeline_hash, output, baseline_dir) for path in paths]
        if workers == 1:
            sources = [ingest_task(task) for task in tasks]
        else:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                sources = list(pool.map(ingest_task, tasks))
        summary = aggregate(stage, sources, rules)
        write_json(stage / 'relatorios' / 'qualidade.json', summary)
        dictionary(stage)
        report_markdown(stage, summary, sources)
        hashes = {p.relative_to(stage).as_posix(): file_hash(p) for p in sorted(stage.rglob('*')) if p.is_file()}
        manifest = {'status': 'complete', 'rules_version': rules['version'], 'pipeline_sha256': pipeline_hash,
                    'executed_at_utc': datetime.now(timezone.utc).isoformat(), 'versions': versions,
                    'sources': sources, 'outputs_sha256': hashes}
        # Publish the manifest last; its hashes reject interrupted or mixed builds.
        for p in sorted(stage.rglob('*')):
            if p.is_file():
                destination = output / p.relative_to(stage)
                destination.parent.mkdir(parents=True, exist_ok=True)
                p.replace(destination)
        write_json(output / 'manifest.json.tmp', manifest)
        (output / 'manifest.json.tmp').replace(output / 'manifest.json')
    verify(output)
    print(f'Concluído: {summary["selected_rows"]:,} registros em {output}', flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir', type=Path, default=ROOT / 'data-sources')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'data' / 'ssp')
    parser.add_argument('--rules', type=Path, default=ROOT / 'config' / 'ssp_rules.json')
    parser.add_argument('--aliases', type=Path, default=ROOT / 'config' / 'bairro_aliases.csv')
    parser.add_argument('--baseline-dir', type=Path, default=ROOT / 'preflight')
    parser.add_argument('--verify', action='store_true', help='Conferir assinaturas das saídas sem reprocessar')
    parser.add_argument('--workers', type=int, choices=[1, 2, 3, 4], default=1,
                        help='Arquivos lidos em paralelo; padrão 1 para limitar memória')
    args = parser.parse_args()
    if args.verify:
        verify(args.output_dir)
    else:
        run(args.input_dir, args.output_dir, args.rules, args.aliases, args.baseline_dir, args.workers)
