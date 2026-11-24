"""Análises exploratórias isoladas; preserva o experimento semanal principal."""
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
        raise ValueError('Registro existente; use verify ou retome as fases.')
    cfg = read_json(CONFIG)
    selection = read_json(ROOT / 'results/selecao.json')
    if sha256(ROOT / 'results/selecao.json') != (ROOT / 'results/selecao.sha256').read_text().strip():
        raise ValueError('Assinatura da seleção original inválida')
    for name, expected in selection['provenance']['scripts_sha256'].items():
        if sha256(ROOT / 'scripts' / name) != expected:
            raise ValueError(f'Código original diverge da execução: {name}')
    verify_inputs(SOURCE)
    versions = {name: importlib.metadata.version(name) for name in
                ['numpy', 'scipy', 'pandas', 'pyarrow', 'scikit-learn', 'xgboost', 'joblib', 'matplotlib']}
    if versions != selection['provenance']['versions']:
        raise ValueError('Usar as mesmas versões do experimento original')
    if any(v['window'] != cfg['window_days'] for v in selection['winners'].values()):
        raise ValueError('Janela incompatível com o protocolo')
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
    print(f'Protocolo registrado {record["registered_at_utc"]}; {len(originals)} arquivos preservados', flush=True)


def verify():
    record = read_json(OUT / 'registro.json')
    if sha256(OUT / 'registro.json') != (OUT / 'registro.sha256').read_text().strip():
        raise ValueError('Registro alterado')
    if record['code_sha256'] != code_hashes():
        raise ValueError('Código ou protocolo complementar alterado após registro')
    for name, expected in record['originals_sha256'].items():
        if sha256(ROOT / name) != expected:
            raise ValueError(f'Original alterado: {name}')
    current = {p.relative_to(ROOT).as_posix() for p in original_files()}
    if current != set(record['originals_sha256']):
        raise ValueError('Inventário original alterado')
    if sha256(SOURCE / 'manifest.json') != record['weekly_manifest_sha256']:
        raise ValueError('Manifesto semanal alterado')
    verify_inputs(SOURCE)
    return record


def columns_for(groups, window=84):
    mapping = {'historico': [f'contagem_semana_anterior_{i:02}' for i in range(1, window // 7 + 1)],
               'resumos': ['media_semanal', 'desvio_padrao_semanal', 'tendencia_semanal', 'dias_sem_registros'],
               'calendario': ['mes'], 'bairro': ['bairro_chave']}
    if not groups or set(groups) - mapping.keys() or len(groups) != len(set(groups)):
        raise ValueError('Grupos inválidos')
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


def aligned(frame, reference):
    if frame[KEYS].duplicated().any() or reference[KEYS].duplicated().any():
        raise ValueError('Pares duplicados')
    a, b = frame.sort_values(KEYS).reset_index(drop=True), reference.sort_values(KEYS).reset_index(drop=True)
    if not a[KEYS].equals(b[KEYS]) or not np.array_equal(a['alvo'], b['alvo']):
        raise ValueError('Comparação exige pares e alvos idênticos')
    return a, b


def block_indices(n, repetitions, block, seed):
    if n < block:
        raise ValueError('Poucas semanas para bootstrap')
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
        result['aviso'] = 'Menos de quatro semanas com pares; intervalo não calculado'
        return result
    weekly = weekly.reindex(weeks, fill_value=0)
    sums = weekly['sum'].to_numpy()[samples].sum(axis=1)
    counts = weekly['count'].to_numpy()[samples].sum(axis=1)
    good = counts > 0
    estimates = sums[good] / counts[good]
    low, high = np.quantile(estimates, [.025, .975])
    result.update(ic95_inferior=float(low), ic95_superior=float(high),
                  repeticoes_validas=int(good.sum()), repeticoes_descartadas=int((~good).sum()),
                  aviso='Intervalo exploratório aproximado sem correção por multiplicidade')
    return result


def seal(destination):
    write_json(destination / 'concluido.json', dict(
        registered_sha256=sha256(OUT / 'registro.json'), completed_at_utc=datetime.now(timezone.utc).isoformat(),
        outputs_sha256={p.relative_to(destination).as_posix(): sha256(p) for p in sorted(destination.rglob('*'))
                        if p.is_file() and p.name != 'concluido.json'}))


def check_seal(destination):
    record = read_json(destination / 'concluido.json')
    if record['registered_sha256'] != sha256(OUT / 'registro.json'):
        raise ValueError('Saída pertence a outro protocolo')
    for name, expected in record['outputs_sha256'].items():
        if sha256(destination / name) != expected:
            raise ValueError(f'Saída complementar alterada: {name}')


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
                    raise ValueError('Cache complementar incompatível')
                frame = pd.read_parquet(saved)
                print(f'{name}: cache verificado', flush=True)
            else:
                if saved.exists() or metadata.exists():
                    raise ValueError(f'Arquivo incompleto em {name}; conferir antes de retomar')
                encoder = encoder_for(columns)
                a = encoder.fit_transform(xtrain[columns])
                b = encoder.transform(xeval[columns])
                estimator = model_builder(family)(record['parameters'][family], seed=cfg['seed'], threads=cfg['threads'])
                print(f'{name}: iniciando ajuste; n={len(train)}; atributos={len(columns)}; codificados={a.shape[1]}', flush=True)
                started = time.perf_counter()
                with threadpool_limits(limits=cfg['threads']):
                    estimator.fit(a, ytrain)
                    prediction = np.maximum(0, estimator.predict(b).astype(np.float64))
                seconds = time.perf_counter() - started
                frame, _ = prediction_frame(train, evaluation, y, prediction, name, 84, 'validacao')
                frame.to_parquet(saved, index=False, compression='zstd')
                row = dict(modelo=family, versao=variant, entradas=columns, atributos_codificados=a.shape[1],
                           treino_n=len(train), segundos=seconds, **metrics(y, prediction),
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
                intervalos_nao_consecutivos=gaps, blocos='Ordem das semanas elegíveis; todos os bairros da semana são mantidos')


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
        raise ValueError('Métodos originais inesperados')
    main_cfg = read_json(ROOT / 'config/experimento_semanal.json')
    train, evaluation, _, _, _, y = prepare_phase(SOURCE, 'teste', 84, main_cfg)
    rebuilt, bounds = prediction_frame(train, evaluation, y, np.zeros(len(y)), 'auditoria', 84, 'teste')
    reference = frame[frame['modelo']==cfg['reference']]
    reference, rebuilt = aligned(reference, rebuilt)
    for column in ['faixa_volume_treino', 'categoria_desconhecida']:
        if not reference[column].equals(rebuilt[column]):
            raise ValueError('Quartis ou categorias divergem do ajuste original')
    weeks = sorted(reference['inicio_semana'].unique())
    samples = block_indices(len(weeks), cfg['bootstrap']['repetitions'], cfg['bootstrap']['block_weeks'], cfg['seed'])
    rows, overall, diagnostic, comparisons, by_week, by_area = [], [], [], [], [], []
    for model, predictions in frame.groupby('modelo', sort=True):
        aligned(predictions, reference)
        a, b = aligned(predictions, reference)
        if not a['faixa_volume_treino'].equals(b['faixa_volume_treino']):
            raise ValueError('Grupos diferentes entre métodos')
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
    print('Volume: pares, quartis e erros reconciliados; tabelas e gráficos gerados', flush=True)


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
        ax.set(title='Random Forest' if family=='rf' else 'XGBoost', xlabel='ΔMAE versão reduzida − completa')
    fig.suptitle('Remoção de atributos em 2024 • intervalos exploratórios de 95%')
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
    axes[0].set(ylabel='MAE por par bairro–semana', xlabel='Quartil de volume no treino')
    axes[1].axhline(0, color='gray', linewidth=1)
    axes[1].set(ylabel='Erro médio com sinal', xlabel='Quartil de volume no treino')
    axes[0].legend(fontsize=9)
    fig.suptitle('Teste de 2025 • magnitude e direção dos erros')
    export(fig, dest, 'erros_por_volume', plt)
    fig, ax = plt.subplots(figsize=(10, 4.5), layout='constrained')
    labels = [g for g in GROUPS if g in set(comparisons['grupo'])]
    for model, offset in [('rf', -.12), ('xgb', .12)]:
        data = comparisons[comparisons['modelo']==model].set_index('grupo').reindex(labels)
        means = data['delta_mae'].to_numpy()
        positions = np.arange(len(labels))+offset
        ax.vlines(positions, data['ic95_inferior'], data['ic95_superior'], color=colors[model])
        ax.scatter(positions, means, label=model, color=colors[model])
    ax.set_xticks(np.arange(len(labels)), [g.replace('sem_historico_ajuste', 'Sem histórico\nde ajuste') for g in labels])
    ax.axhline(0, color='gray', linewidth=1)
    ax.set(ylabel='ΔMAE modelo − média de quatro semanas', title='Comparações exploratórias por volume em 2025 • intervalos de 95%')
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
    lines = ['# Análises complementares de atributos e erros por volume', '',
        f'Protocolo registrado em {record["registered_at_utc"]}, antes das execuções complementares.', '',
        '**Análises exploratórias posteriores ao conhecimento dos resultados principais.** '
        '2024 já orientou a escolha dos parâmetros; 2025 já foi examinado. Não há nova confirmação independente, '
        'mudança da seleção original ou demonstração causal. Os dados são exclusivamente SSP-SP.', '',
        '## Contribuição dos atributos em 2024', '',
        'Dez ajustes, mantendo parâmetros e janela de 84 dias. Mesmos pares e alvos disponíveis na validação original. '
        'Cada versão completa foi retreinada e comparada às previsões originais. Não houve nova busca de parâmetros.', '',
        '| Modelo | Versão | MAE | RMSE | Ajuste e previsão em segundos |', '|---|---|---:|---:|---:|']
    lines += [f'| {r.modelo} | {r.versao} | {r.mae:.6f} | {r.rmse:.6f} | {r.segundos:.1f} |' for r in a.itertuples()]
    baseline = pd.read_csv(OUT / 'atributos/referencias.csv')
    ref = baseline[baseline['modelo']=='media_4_semanas'].iloc[0]
    lines += ['', f'Média de quatro semanas na mesma população: MAE {ref.mae:.6f}; RMSE {ref.rmse:.6f}.', '',
        '| Modelo | Remoção | ΔMAE versus completa | Intervalo exploratório de 95% |', '|---|---|---:|---|']
    lines += [f'| {r.modelo} | {r.versao} | {r.delta_mae:+.6f} | [{r.ic95_inferior:+.6f}; {r.ic95_superior:+.6f}] |' for r in c.itertuples()]
    lines += ['', 'ΔMAE positivo indica piora ao retirar entradas; negativo indica melhora. '
        'São comparações condicionais às configurações escolhidas para a versão completa. Os grupos são correlacionados; '
        'os resumos derivam do histórico. Remover colunas pode mudar as divisões das árvores e não mede causalidade nem importância universal. '
        'Uma única semente não mede estabilidade entre sementes. Não se escolhe uma nova versão vencedora.', '',
        '![Contribuição dos atributos](atributos/contribuicao_atributos.png)', '',
        '## Composição dos grupos de volume em 2025', '',
        'Quartis reconstruídos a partir dos alvos disponíveis no treino e conferidos contra os rótulos originais. '
        'Não se usou o volume futuro para definir grupos. Empates podem tornar os grupos desiguais.', '',
        '| Grupo | Bairros | Pares | Registros observados | Alvos zero |', '|---|---:|---:|---:|---:|']
    lines += [f'| {r.grupo} | {r.bairros} | {r.pares} | {r.registros:.0f} | {r.proporcao_zero:.1%} |' for r in composition.itertuples()]
    lines += ['', '## Erros por volume', '', '| Modelo | Grupo | MAE | RMSE | Erro médio com sinal | Parcela do erro absoluto | Parcela do erro quadrático |',
        '|---|---|---:|---:|---:|---:|---:|']
    lines += [f'| {r.modelo} | {r.grupo} | {r.mae:.6f} | {r.rmse:.6f} | {r.erro_medio:+.6f} | {r.participacao_erro_absoluto:.1%} | {r.participacao_erro_quadratico:.1%} |'
              for r in v.itertuples()]
    lines += ['', 'Erro com sinal é previsão menos observado: positivo indica superestimação média. '
        'As parcelas de erro usam o total de cada método como denominador. Diferem de participação nos registros.', '',
        '| Modelo | Grupo | ΔMAE versus média | Intervalo exploratório de 95% |', '|---|---|---:|---|']
    lines += [f'| {r.modelo} | {r.grupo} | {r.delta_mae:+.6f} | [{r.ic95_inferior:+.6f}; {r.ic95_superior:+.6f}] |' for r in vc.itertuples()]
    lines += ['', '![Erros por volume](volume/erros_por_volume.png)', '',
        '![Contrastes por volume](volume/contrastes_por_volume.png)', '',
        '## Diagnóstico dos zeros e rastreabilidade', '',
        'O arquivo volume/diagnostico_zero_positivo.csv separa alvos observados zero e positivos por método e grupo. '
        'É retrospectivo: não permite escolher uma estratégia sabendo de antemão se a semana terá registro. '
        'Os arquivos erros_por_semana.csv e erros_por_bairro.csv permitem localizar concentrações de erro sem selecionar casos favoráveis.', '',
        '## Inferência e limitações', '',
        'Mil reamostragens pareadas em blocos de quatro semanas avaliadas, com todos os bairros de cada semana. '
        'Os mesmos sorteios foram usados dentro de cada fase. Intervalos percentis de 95% são aproximações '
        'exploratórias, sem correção por multiplicidade; não são intervalos de previsão ou comprovação confirmatória. '
        'A dependência espacial e a escolha do bloco limitam a inferência. Semanas sem pares num grupo têm denominador zero; '
        'sorteios com denominador total zero são descartados e contabilizados. Os calendários e eventuais lacunas estão nos arquivos conferencia.json.', '',
        'Não se ajustou uma política de seleção por bairro. Não se treinaram ablações em 2025 ou 2026. '
        'Subnotificação, instabilidade dos rótulos de bairro e registro como proxy de publicação continuam limitando o estudo.', '',
        '## Integridade', '',
        f'{len(record["originals_sha256"])} arquivos do experimento original conferidos por SHA256 antes e depois. '
        'As previsões e os parâmetros originais permaneceram intactos. Registro, verificações e manifestos de saída acompanham esta pasta.']
    (OUT / 'relatorio.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    verify()
    write_json(OUT / 'verificacao_final.json', dict(checked_at_utc=datetime.now(timezone.utc).isoformat(),
        originais_preservados=True, arquivos_originais=len(record['originals_sha256']),
        registro_sha256=sha256(OUT / 'registro.json'),
        outputs_sha256={p.relative_to(OUT).as_posix(): sha256(p) for p in sorted(OUT.rglob('*'))
                       if p.is_file() and p.name != 'verificacao_final.json'}))
    print('Relatório concluído; originais preservados', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
    print('Integridade original verificada', flush=True)


if __name__ == '__main__':
    main()
