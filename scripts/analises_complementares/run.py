
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import sys
import time

for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[name] = '4'

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder
from threadpoolctl import threadpool_limits
from scripts.prepare_weekly_data import sha256, write_json
from scripts.training_common import (baseline_predictions, feature_names, metrics,
    prediction_frame, prepare_phase, verify_inputs)
from scripts.train_models import model_builder

CONFIG = ROOT / 'config/analises_complementares.json'
PROTOCOL = ROOT / 'docs/protocolos/analises_complementares.md'
OUT = ROOT / 'results/analises_complementares'
SOURCE = ROOT / 'normalized-data/modelagem'
KEYS = ['bairro_chave', 'inicio_semana']
GROUPS = ['Q1', 'Q2', 'Q3', 'Q4', 'sem_historico_ajuste']


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def original_files():
    paths = [p for p in (ROOT / 'results').rglob('*') if p.is_file()
             and OUT not in p.parents]
    paths += [p for p in (ROOT / 'scripts').glob('*.py')]
    paths += [ROOT / 'config/experimento_semanal.json']
    return sorted(paths)


def code_hashes():
    return {p.relative_to(ROOT).as_posix(): sha256(p) for p in
            [Path(__file__).resolve(), CONFIG, PROTOCOL]}


def register():
    if OUT.exists() and any(OUT.iterdir()):
        raise ValueError('Registration already exists; use verify or resume the phases.')
    cfg = read_json(CONFIG)
    selection = read_json(ROOT / 'results/selecao.json')
    if sha256(ROOT / 'results/selecao.json') != (ROOT / 'results/selecao.sha256').read_text().strip():
        raise ValueError('Invalid original selection hash')
    for name, expected in selection['provenance']['scripts_sha256'].items():
        if sha256(ROOT / 'scripts' / name) != expected:
            raise ValueError(f'Original code differs from the recorded run: {name}')
    verify_inputs(SOURCE)
    versions = {name: importlib.metadata.version(name) for name in
                ['numpy', 'scipy', 'pandas', 'pyarrow', 'scikit-learn', 'xgboost', 'joblib', 'matplotlib']}
    if versions != selection['provenance']['versions']:
        raise ValueError('Use the same versions as the original experiment')
    if any(v['window'] != cfg['window_days'] for v in selection['winners'].values()):
        raise ValueError('Window is incompatible with the protocol')
    originals = {p.relative_to(ROOT).as_posix(): sha256(p) for p in original_files()}
    record = dict(registered_at_utc=datetime.now(timezone.utc).isoformat(),
                  status=cfg['status_cientifico'], config=cfg,
                  parameters={f: r['params'] for f, r in selection['winners'].items()},
                  selected_models={f: r['model'] for f, r in selection['winners'].items()},
                  versions=versions, python=platform.python_version(), code_sha256=code_hashes(),
                  weekly_manifest_sha256=sha256(SOURCE / 'manifest.json'),
                  originals_sha256=originals)
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / 'registro.json', record)
    (OUT / 'registro.sha256').write_text(sha256(OUT / 'registro.json') + '\n', encoding='utf-8')
    print(f'Protocol registered {record["registered_at_utc"]}; {len(originals)} files preserved', flush=True)


def verify():
    record = read_json(OUT / 'registro.json')
    if sha256(OUT / 'registro.json') != (OUT / 'registro.sha256').read_text().strip():
        raise ValueError('Registration has changed')
    expected_code = dict(record['code_sha256'])
    amendment_path = OUT / 'adendo_representacao.json'
    if amendment_path.exists():
        amendment = read_json(amendment_path)
        original_code = expected_code['scripts/analises_complementares/run.py']
        if (sha256(amendment_path) != (OUT / 'adendo_representacao.sha256').read_text().strip()
            or amendment['registro_inicial_sha256'] != sha256(OUT / 'registro.json')
            or amendment['codigo_inicial_sha256'] != original_code
            or sha256(OUT / 'protocolo_snapshot/run_inicial.py') != original_code):
            raise ValueError('Invalid amendment or initial snapshot')
        expected_code['scripts/analises_complementares/run.py'] = amendment['codigo_corrigido_sha256']
    if expected_code != code_hashes():
        raise ValueError('Complementary code or protocol changed after registration')
    for name, expected in record['originals_sha256'].items():
        if sha256(ROOT / name) != expected:
            raise ValueError(f'Original has changed: {name}')
    current = {p.relative_to(ROOT).as_posix() for p in original_files()}
    if current != set(record['originals_sha256']):
        raise ValueError('Original inventory has changed')
    if sha256(SOURCE / 'manifest.json') != record['weekly_manifest_sha256']:
        raise ValueError('Weekly manifest has changed')
    verify_inputs(SOURCE)
    return record


def columns_for(groups, window=84):
    mapping = {'historico': [f'contagem_semana_anterior_{i:02}' for i in range(1, window // 7 + 1)],
               'resumos': ['media_semanal', 'desvio_padrao_semanal', 'tendencia_semanal', 'dias_sem_registros'],
               'calendario': ['mes'], 'bairro': ['bairro_chave']}
    if not groups or set(groups) - mapping.keys() or len(groups) != len(set(groups)):
        raise ValueError('Invalid groups')
    chosen = {c for g in groups for c in mapping[g]}
    return [c for c in feature_names(window) if c in chosen]


def encoder_for(columns):
    categorical = [c for c in ['bairro_chave', 'mes'] if c in columns]
    numeric = [c for c in columns if c not in categorical]
    parts = []
    if categorical:
        parts.append(('categories', OneHotEncoder(handle_unknown='ignore', sparse_output=True, dtype=np.float32), categorical))
    if numeric:
        parts.append(('history', 'passthrough', numeric))
    return ColumnTransformer(parts, sparse_threshold=1.0)


def encode_inputs(encoder, train, evaluation):

    return (csr_matrix(encoder.fit_transform(train), dtype=np.float32),
            csr_matrix(encoder.transform(evaluation), dtype=np.float32))


def aligned(frame, reference):
    if frame[KEYS].duplicated().any() or reference[KEYS].duplicated().any():
        raise ValueError('Duplicate pairs')
    a, b = frame.sort_values(KEYS).reset_index(drop=True), reference.sort_values(KEYS).reset_index(drop=True)
    if not a[KEYS].equals(b[KEYS]) or not np.array_equal(a['alvo'], b['alvo']):
        raise ValueError('Comparison requires identical pairs and targets')
    return a, b


def block_indices(n, repetitions, block, seed):
    if n < block:
        raise ValueError('Too few weeks for bootstrap')
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - block + 1, size=(repetitions, math.ceil(n / block)))
    return (starts[:, :, None] + np.arange(block)).reshape(repetitions, -1)[:, :n]


def contrast(frame, reference, weeks, samples):
    a, b = aligned(frame, reference)
    difference = np.abs(a['previsao'].to_numpy() - a['alvo'].to_numpy()) - np.abs(b['previsao'].to_numpy() - b['alvo'].to_numpy())
    weekly = pd.DataFrame({'week': a['inicio_semana'], 'delta': difference}).groupby('week')['delta'].agg(['sum', 'count'])
    effective = len(weekly)
    result = dict(delta_mae=float(difference.mean()) if len(difference) else None,
                  semanas_com_pares=effective, n=len(a), ic95_inferior=None, ic95_superior=None,
                  repeticoes_validas=0, repeticoes_descartadas=0)
    if effective < 4:
        result['aviso'] = 'Fewer than four weeks with pairs; interval not calculated'
        return result
    weekly = weekly.reindex(weeks, fill_value=0)
    sums = weekly['sum'].to_numpy()[samples].sum(axis=1)
    counts = weekly['count'].to_numpy()[samples].sum(axis=1)
    good = counts > 0
    estimates = sums[good] / counts[good]
    low, high = np.quantile(estimates, [.025, .975])
    result.update(ic95_inferior=float(low), ic95_superior=float(high),
                  repeticoes_validas=int(good.sum()), repeticoes_descartadas=int((~good).sum()),
                  aviso='Approximate exploratory interval without multiplicity correction')
    return result


def seal(destination):
    write_json(destination / 'concluido.json', dict(
        registered_sha256=sha256(OUT / 'registro.json'), completed_at_utc=datetime.now(timezone.utc).isoformat(),
        outputs_sha256={p.relative_to(destination).as_posix(): sha256(p) for p in sorted(destination.rglob('*'))
                        if p.is_file() and p.name != 'concluido.json'}))


def check_seal(destination):
    record = read_json(destination / 'concluido.json')
    if record['registered_sha256'] != sha256(OUT / 'registro.json'):
        raise ValueError('Output belongs to another protocol')
    for name, expected in record['outputs_sha256'].items():
        if sha256(destination / name) != expected:
            raise ValueError(f'Complementary output has changed: {name}')


def attributes(record):
    dest = OUT / 'atributos'
    dest.mkdir(exist_ok=True)
    if (dest / 'concluido.json').exists():
        check_seal(dest)
        return
    cfg = record['config']
    main_cfg = read_json(ROOT / 'config/experimento_semanal.json')
    train, evaluation, xtrain, xeval, ytrain, y = prepare_phase(SOURCE, 'validacao', 84, main_cfg)
    results, frames = [], []
    weeks = sorted(evaluation['inicio_semana'].unique())
    samples = block_indices(len(weeks), cfg['bootstrap']['repetitions'], cfg['bootstrap']['block_weeks'], cfg['seed'])
    for family in ['rf', 'xgb']:
        for variant, groups in cfg['variants'].items():
            name = f'{family}_{variant}'
            saved, metadata = dest / f'{name}.parquet', dest / f'{name}.json'
            columns = columns_for(groups)
            if saved.exists() and metadata.exists():
                row = read_json(metadata)
                if row['registro_sha256'] != sha256(OUT / 'registro.json') or row['prediction_sha256'] != sha256(saved):
                    raise ValueError('Incompatible complementary cache')
                if variant == 'somente_historico' and not row.get('entrada_esparsa_csr'):
                    raise ValueError('History-only cache predates the CSR representation check')
                frame = pd.read_parquet(saved)
                print(f'{name}: cache verified', flush=True)
            else:
                if saved.exists() or metadata.exists():
                    raise ValueError(f'Incomplete file for {name}; check before resuming')
                encoder = encoder_for(columns)
                a, b = encode_inputs(encoder, xtrain[columns], xeval[columns])
                estimator = model_builder(family)(record['parameters'][family], seed=cfg['seed'], threads=cfg['threads'])
                print(f'{name}: starting fit; n={len(train)}; features={len(columns)}; encoded={a.shape[1]}', flush=True)
                started = time.perf_counter()
                with threadpool_limits(limits=cfg['threads']):
                    estimator.fit(a, ytrain)
                    prediction = np.maximum(0, estimator.predict(b).astype(np.float64))
                seconds = time.perf_counter() - started
                frame, _ = prediction_frame(train, evaluation, y, prediction, name, 84, 'validacao')
                frame.to_parquet(saved, index=False, compression='zstd')
                row = dict(modelo=family, versao=variant, entradas=columns, atributos_codificados=a.shape[1],
                           treino_n=len(train), segundos=seconds, **metrics(y, prediction),
                           entrada_esparsa_csr=True,
                           adendo_sha256=sha256(OUT / 'adendo_representacao.json') if (OUT / 'adendo_representacao.json').exists() else None,
                           registro_sha256=sha256(OUT / 'registro.json'), prediction_sha256=sha256(saved))
                write_json(metadata, row)
                del estimator, a, b, encoder
                print(f'{name}: MAE={row["mae"]:.6f}, RMSE={row["rmse"]:.6f}, {seconds:.1f}s', flush=True)
            expected_frame, _ = prediction_frame(train, evaluation, y, np.zeros(len(y)), name, 84, 'validacao')
            aligned(frame, expected_frame)
            if variant == 'completa':
                original = pd.read_parquet(ROOT / 'results/validacao' / f'{record["selected_models"][family]}.parquet')
                a, b = aligned(frame, original)
                np.testing.assert_allclose(a['previsao'], b['previsao'], **cfg['reproduction'])
                row['reproducao_diferenca_maxima'] = float(np.abs(a['previsao'] - b['previsao']).max())
            results.append(row)
            frames.append(frame)
    pd.concat(frames, ignore_index=True).to_parquet(dest / 'previsoes.parquet', index=False, compression='zstd')
    summary = pd.DataFrame([{k: v for k, v in r.items() if k not in ['entradas', 'prediction_sha256', 'registro_sha256']} for r in results])
    summary.to_csv(dest / 'metricas.csv', index=False)
    comparisons = []
    for family in ['rf', 'xgb']:
        complete = next(f for f in frames if f['modelo'].iloc[0] == f'{family}_completa')
        for variant in cfg['variants']:
            if variant != 'completa':
                reduced = next(f for f in frames if f['modelo'].iloc[0] == f'{family}_{variant}')
                comparisons.append(dict(modelo=family, versao=variant, **contrast(reduced, complete, weeks, samples)))
    pd.DataFrame(comparisons).to_csv(dest / 'contrastes.csv', index=False)
    baselines = [dict(modelo=n, **metrics(y, pred)) for n, pred in baseline_predictions(evaluation).items()]
    pd.DataFrame(baselines).to_csv(dest / 'referencias.csv', index=False)
    write_json(dest / 'conferencia.json', dict(treino=len(train), pares=len(evaluation), semanas=len(weeks),
        pares_alvos_identicos=True, reproducao={r['modelo']: r['reproducao_diferenca_maxima'] for r in results if r['versao']=='completa'},
        calendario=calendar_info(weeks)))
    plot_attributes(summary, pd.DataFrame(comparisons), dest)
    seal(dest)


def error_summary(frame):
    value = metrics(frame['alvo'], frame['previsao'])
    errors = frame['previsao'].to_numpy() - frame['alvo'].to_numpy()
    return value | dict(bairros=frame['bairro_chave'].nunique(), semanas=frame['inicio_semana'].nunique(),
        proporcao_zero=float((frame['alvo']==0).mean()), erro_medio=float(errors.mean()),
        soma_erro_absoluto=float(np.abs(errors).sum()), soma_erro_quadratico=float(np.square(errors).sum()))


def reconcile(rows, full):
    for name in ['n', 'alvo_total', 'previsao_total', 'soma_erro_absoluto', 'soma_erro_quadratico']:
        np.testing.assert_allclose(sum(r[name] for r in rows), full[name], rtol=1e-10, atol=1e-8)


def calendar_info(weeks):
    values = pd.to_datetime(weeks)
    gaps = [(a.date().isoformat(), b.date().isoformat()) for a, b in zip(values[:-1], values[1:]) if (b-a).days != 7]
    return dict(inicio=values.min().date().isoformat(), fim=values.max().date().isoformat(),
                intervalos_nao_consecutivos=gaps, blocos='Eligible week order; all neighborhoods within each week are retained')


def volume(record):
    dest = OUT / 'volume'
    dest.mkdir(exist_ok=True)
    if (dest / 'concluido.json').exists():
        check_seal(dest)
        return
    cfg = record['config']
    frame = pd.read_parquet(ROOT / 'results/teste/previsoes.parquet')
    expected = {'rf', 'xgb', 'media_4_semanas', 'ultima_semana', 'zero'}
    if set(frame['modelo']) != expected:
        raise ValueError('Unexpected original methods')
    main_cfg = read_json(ROOT / 'config/experimento_semanal.json')
    train, evaluation, _, _, _, y = prepare_phase(SOURCE, 'teste', 84, main_cfg)
    rebuilt, bounds = prediction_frame(train, evaluation, y, np.zeros(len(y)), 'auditoria', 84, 'teste')
    reference = frame[frame['modelo']==cfg['reference']]
    reference, rebuilt = aligned(reference, rebuilt)
    for column in ['faixa_volume_treino', 'categoria_desconhecida']:
        if not reference[column].equals(rebuilt[column]):
            raise ValueError('Quartiles or categories differ from the original fit')
    weeks = sorted(reference['inicio_semana'].unique())
    samples = block_indices(len(weeks), cfg['bootstrap']['repetitions'], cfg['bootstrap']['block_weeks'], cfg['seed'])
    rows, overall, diagnostic, comparisons, by_week, by_area = [], [], [], [], [], []
    for model, predictions in frame.groupby('modelo', sort=True):
        aligned(predictions, reference)
        a, b = aligned(predictions, reference)
        if not a['faixa_volume_treino'].equals(b['faixa_volume_treino']):
            raise ValueError('Different groups across methods')
        full = error_summary(predictions)
        overall.append(dict(modelo=model, **full))
        part = []
        for group, data in predictions.groupby('faixa_volume_treino', sort=True):
            row = dict(modelo=model, grupo=group, **error_summary(data))
            row['participacao_erro_absoluto'] = row['soma_erro_absoluto'] / full['soma_erro_absoluto'] if full['soma_erro_absoluto'] else None
            row['participacao_erro_quadratico'] = row['soma_erro_quadratico'] / full['soma_erro_quadratico'] if full['soma_erro_quadratico'] else None
            rows.append(row); part.append(row)
            if model in ['rf', 'xgb']:
                comparisons.append(dict(modelo=model, grupo=group, **contrast(data, reference[reference['faixa_volume_treino']==group], weeks, samples)))
            split = []
            for label, mask in [('zero', data['alvo']==0), ('positivo', data['alvo']>0)]:
                sub = data[mask]
                if len(sub):
                    summary = error_summary(sub)
                    diagnostic.append(dict(modelo=model, grupo=group, alvo_observado=label, **summary))
                    split.append(summary)
            reconcile(split, row)
        reconcile(part, full)
        for week, data in predictions.groupby('inicio_semana'):
            by_week.append(dict(modelo=model, inicio_semana=str(week), **error_summary(data)))
        for area, data in predictions.groupby('bairro_chave'):
            by_area.append(dict(modelo=model, bairro_chave=area, grupo=data['faixa_volume_treino'].iloc[0], **error_summary(data)))
    table = pd.DataFrame(rows)
    for filename, records in [('metricas_por_volume', rows), ('metricas_gerais', overall),
        ('diagnostico_zero_positivo', diagnostic), ('contrastes', comparisons), ('erros_por_semana', by_week), ('erros_por_bairro', by_area)]:
        pd.DataFrame(records).to_csv(dest / f'{filename}.csv', index=False)
    base = reference.copy()
    composition = []
    for group in GROUPS:
        part = base[base['faixa_volume_treino']==group]
        composition.append(dict(grupo=group, bairros=part['bairro_chave'].nunique(), pares=len(part),
            semanas=part['inicio_semana'].nunique(), registros=float(part['alvo'].sum()),
            proporcao_zero=float((part['alvo']==0).mean()) if len(part) else None))
    pd.DataFrame(composition).to_csv(dest / 'composicao.csv', index=False)
    write_json(dest / 'conferencia.json', dict(quartis_treino=bounds, grupos_reconstruidos=True,
        pares_comuns=True, erros_reconciliados=True, zeros_positivos_reconciliados=True,
        pares=len(reference), semanas=len(weeks), calendario=calendar_info(weeks)))
    plot_volume(table, pd.DataFrame(comparisons), dest)
    seal(dest)
    print('Volume: pairs, quartiles and errors reconciled; tables and plots generated', flush=True)


def plot_setup():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})
    return plt


def export(fig, dest, name, plt):
    fig.savefig(dest / f'{name}.png', dpi=180)
    fig.savefig(dest / f'{name}.pdf')
    plt.close(fig)


def plot_attributes(table, comparisons, dest):
    plt = plot_setup()
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), layout='constrained', sharey=True)
    for ax, family in zip(axes, ['rf', 'xgb']):
        data = comparisons[comparisons['modelo']==family].iloc[::-1]
        ax.hlines(np.arange(len(data)), data['ic95_inferior'], data['ic95_superior'], color='#007F83')
        ax.scatter(data['delta_mae'], np.arange(len(data)), color='#007F83')
        ax.set_yticks(np.arange(len(data)), data['versao'].str.replace('_', ' '))
        ax.axvline(0, color='gray', linewidth=1)
        ax.set(title='Random Forest' if family=='rf' else 'XGBoost', xlabel='ΔMAE reduced version − full version')
    fig.suptitle('Feature removal in 2024 • exploratory 95% intervals')
    export(fig, dest, 'contribuicao_atributos', plt)


def plot_volume(table, comparisons, dest):
    plt = plot_setup()
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), layout='constrained')
    labels = ['Q1', 'Q2', 'Q3', 'Q4']
    colors = {'media_4_semanas': '#A96124', 'rf': '#007F83', 'xgb': '#7755AA'}
    for model, color in colors.items():
        data = table[table['modelo']==model].set_index('grupo').reindex(labels)
        axes[0].plot(labels, data['mae'], 'o-', label=model, color=color)
        axes[1].plot(labels, data['erro_medio'], 'o-', label=model, color=color)
    axes[0].set(ylabel='MAE per neighborhood-week pair', xlabel='Training volume quartile')
    axes[1].axhline(0, color='gray', linewidth=1)
    axes[1].set(ylabel='Mean signed error', xlabel='Training volume quartile')
    axes[0].legend(fontsize=9)
    fig.suptitle('2025 test • error magnitude and direction')
    export(fig, dest, 'erros_por_volume', plt)
    fig, ax = plt.subplots(figsize=(10, 4.5), layout='constrained')
    labels = [g for g in GROUPS if g in set(comparisons['grupo'])]
    for model, offset in [('rf', -.12), ('xgb', .12)]:
        data = comparisons[comparisons['modelo']==model].set_index('grupo').reindex(labels)
        means = data['delta_mae'].to_numpy()
        positions = np.arange(len(labels))+offset
        ax.vlines(positions, data['ic95_inferior'], data['ic95_superior'], color=colors[model])
        ax.scatter(positions, means, label=model, color=colors[model])
    ax.set_xticks(np.arange(len(labels)), [g.replace('sem_historico_ajuste', 'No training\nhistory') for g in labels])
    ax.axhline(0, color='gray', linewidth=1)
    ax.set(ylabel='ΔMAE model − four-week mean', title='Exploratory volume comparisons in 2025 • 95% intervals')
    ax.legend()
    export(fig, dest, 'contrastes_por_volume', plt)


def report(record):
    for name in ['atributos', 'volume']:
        check_seal(OUT / name)
    a = pd.read_csv(OUT / 'atributos/metricas.csv')
    c = pd.read_csv(OUT / 'atributos/contrastes.csv')
    v = pd.read_csv(OUT / 'volume/metricas_por_volume.csv')
    vc = pd.read_csv(OUT / 'volume/contrastes.csv')
    composition = pd.read_csv(OUT / 'volume/composicao.csv')
    lines = ['# Complementary feature and volume error analyses', '',
        f'Protocol registered at {record["registered_at_utc"]}, before the complementary runs.', '',
        '**Exploratory analyses performed after the main results were known.** '
        '2024 already informed parameter selection; 2025 has already been examined. This is not new independent confirmation, '
        'a change to the original selection, or causal evidence. Only SSP-SP data are used.', '',
        '## Feature contribution in 2024', '',
        'Ten fits, keeping parameters and the 84-day window unchanged. The same pairs and available targets as in the original validation are used. '
        'Each full version was retrained and compared with the original predictions. No new parameter search was performed.', '',
        '| Model | Version | MAE | RMSE | Fit and prediction seconds |', '|---|---|---:|---:|---:|']
    lines += [f'| {r.modelo} | {r.versao} | {r.mae:.6f} | {r.rmse:.6f} | {r.segundos:.1f} |' for r in a.itertuples()]
    baseline = pd.read_csv(OUT / 'atributos/referencias.csv')
    ref = baseline[baseline['modelo']=='media_4_semanas'].iloc[0]
    lines += ['', f'Four-week mean on the same population: MAE {ref.mae:.6f}; RMSE {ref.rmse:.6f}.', '',
        '| Model | Removal | ΔMAE versus full version | Exploratory 95% interval |', '|---|---|---:|---|']
    lines += [f'| {r.modelo} | {r.versao} | {r.delta_mae:+.6f} | [{r.ic95_inferior:+.6f}; {r.ic95_superior:+.6f}] |' for r in c.itertuples()]
    lines += ['', 'Positive ΔMAE indicates worse performance after removing inputs; negative values indicate improvement. '
        'Comparisons are conditional on configurations selected for the full version. Groups are correlated; '
        'summaries are derived from history. Removing columns can change tree splits and does not measure causality or universal importance. '
        'A single seed does not measure stability across seeds. No new winning version is selected.', '',
        '![Feature contribution](atributos/contribuicao_atributos.png)', '',
        '## Volume group composition in 2025', '',
        'Quartiles are reconstructed from available training targets and checked against original labels. '
        'Future volume was not used to define groups. Ties can produce unequal groups.', '',
        '| Group | Neighborhoods | Pairs | Observed records | Zero targets |', '|---|---:|---:|---:|---:|']
    lines += [f'| {r.grupo} | {r.bairros} | {r.pares} | {r.registros:.0f} | {r.proporcao_zero:.1%} |' for r in composition.itertuples()]
    lines += ['', '## Errors by volume', '', '| Model | Group | MAE | RMSE | Mean signed error | Absolute error share | Squared error share |',
        '|---|---|---:|---:|---:|---:|---:|']
    lines += [f'| {r.modelo} | {r.grupo} | {r.mae:.6f} | {r.rmse:.6f} | {r.erro_medio:+.6f} | {r.participacao_erro_absoluto:.1%} | {r.participacao_erro_quadratico:.1%} |'
              for r in v.itertuples()]
    lines += ['', 'Signed error is prediction minus observation: positive values indicate average overestimation. '
        'Error shares use each method total as the denominator. They differ from shares of records.', '',
        '| Model | Group | ΔMAE versus mean | Exploratory 95% interval |', '|---|---|---:|---|']
    lines += [f'| {r.modelo} | {r.grupo} | {r.delta_mae:+.6f} | [{r.ic95_inferior:+.6f}; {r.ic95_superior:+.6f}] |' for r in vc.itertuples()]
    lines += ['', '![Errors by volume](volume/erros_por_volume.png)', '',
        '![Volume contrasts](volume/contrastes_por_volume.png)', '',
        '## Zero target diagnostics and traceability', '',
        'The file volume/diagnostico_zero_positivo.csv separates observed zero and positive targets by method and group. '
        'This is retrospective: it does not allow strategy selection with advance knowledge of whether a week will contain a record. '
        'The files erros_por_semana.csv and erros_por_bairro.csv identify error concentrations without selecting favorable cases.', '',
        '## Inference and limitations', '',
        'One thousand paired resamples in blocks of four evaluated weeks, retaining every neighborhood within each week. '
        'The same draws were used within each phase. Percentile 95% intervals are '
        'exploratory approximations without multiplicity correction; they are not prediction intervals or confirmatory evidence. '
        'Spatial dependence and block choice limit inference. Weeks with no pairs in a group have a zero denominator; '
        'draws with zero total denominator are discarded and counted. Calendars and any gaps are recorded in conferencia.json files.', '',
        'No neighborhood-specific selection policy was fitted. Ablations were not trained in 2025 or 2026. '
        'Underreporting, unstable neighborhood labels and registration as a proxy for publication remain study limitations.', '',
        '## Documented technical adjustment', '',
        'The representation amendment, registered before history-only fits, ensures CSR matrices '
        'in every version. This prevents category removal from also changing zero representation '
        'for XGBoost. The first three RF fits already used CSR and were preserved. An unfinished fit without summaries '
        'was interrupted and repeated. The initial code and original registration are preserved; '
        'parameters, feature groups, hypotheses and metrics remained unchanged.', '',
        '## Integrity', '',
        f'{len(record["originals_sha256"])} original experiment files checked by SHA256 before and after. '
        'Original predictions and parameters remained intact. Registration, checks and output manifests accompany this directory.']
    (OUT / 'relatorio.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    verify()
    write_json(OUT / 'verificacao_final.json', dict(checked_at_utc=datetime.now(timezone.utc).isoformat(),
        originais_preservados=True, arquivos_originais=len(record['originals_sha256']),
        registro_sha256=sha256(OUT / 'registro.json'),
        outputs_sha256={p.relative_to(OUT).as_posix(): sha256(p) for p in sorted(OUT.rglob('*'))
                       if p.is_file() and p.name != 'verificacao_final.json'}))
    print('Report completed; originals preserved', flush=True)


def main():
    parser = argparse.ArgumentParser(description='Run exploratory complementary feature and volume error analyses.')
    parser.add_argument('phase', choices=['register', 'verify', 'attributes', 'volume', 'report'])
    args = parser.parse_args()
    if args.phase == 'register':
        register(); return
    record = verify()
    if args.phase == 'attributes':
        attributes(record)
    elif args.phase == 'volume':
        volume(record)
    elif args.phase == 'report':
        report(record)
    verify()
    print('Original integrity verified', flush=True)


if __name__ == '__main__':
    main()
