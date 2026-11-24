
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import json
import math

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder

try:
    from .prepare_weekly_data import ROOT, available_targets, sha256
except ImportError:
    from prepare_weekly_data import ROOT, available_targets, sha256


def load_config():
    return json.loads((ROOT / 'config/experimento_semanal.json').read_text(encoding='utf-8'))


def verify_inputs(source):
    for manifest_path, base in [(source / 'manifest.json', source), (source.parent / 'metadados/manifest.json', source.parent)]:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for name, expected in manifest['outputs_sha256'].items():
            if sha256(base / name) != expected:
                raise ValueError(f'Input file has changed: {base / name}')
    weekly = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    if weekly['source_manifest_sha256'] != sha256(source.parent / 'metadados/manifest.json'):
        raise ValueError('Incompatible manifests')


def mature(table, cutoff, days):
    return table.filter(pa.array([d + timedelta(days=days) <= cutoff for d in table['fim_semana'].to_pylist()]))


def feature_names(window):
    return ['bairro_chave', 'mes'] + [f'contagem_semana_anterior_{i + 1:02}' for i in range(window // 7)] + ['media_semanal', 'desvio_padrao_semanal', 'tendencia_semanal', 'dias_sem_registros']


def features(frame, window):
    result = frame.loc[:, feature_names(window)].copy()
    result['bairro_chave'] = result['bairro_chave'].astype(str)
    result['mes'] = result['mes'].astype(str)
    for name in result.columns[2:]:
        result[name] = result[name].astype(np.float32)
    if result.isna().any().any():
        raise ValueError('Input contains missing values')
    return result


def preprocessor(window):
    return ColumnTransformer([
        ('categories', OneHotEncoder(handle_unknown='ignore', sparse_output=True, dtype=np.float32), ['bairro_chave', 'mes']),
        ('history', 'passthrough', feature_names(window)[2:]),
    ], sparse_threshold=1.0)


def prepare_phase(source, phase, window, config, ledger=None):
    spec = config['phases'][phase]
    cutoff = date.fromisoformat(spec['fit_cutoff'])
    ledger = pq.read_table(source / 'contribuicoes_alvo.parquet') if ledger is None else ledger
    original = pa.concat_tables([pq.read_table(source / split / f'janela_{window}.parquet') for split in spec['train_splits']])
    train = mature(available_targets(original, ledger, cutoff), cutoff, config['maturity_days']).to_pandas()
    evaluation = pq.read_table(source / spec['eval_split'] / f'janela_{window}.parquet')
    if phase == 'validacao':
        score_cutoff = date.fromisoformat(spec['score_cutoff'])
        evaluation = mature(available_targets(evaluation, ledger, score_cutoff), score_cutoff, config['maturity_days'])
        target = 'alvo_disponivel'
    else:
        target = 'alvo_retrospectivo'
    evaluation = evaluation.to_pandas()
    if train.empty or evaluation.empty:
        raise ValueError('Training or evaluation set is empty')
    if train[['bairro_chave', 'inicio_semana']].duplicated().any() or evaluation[['bairro_chave', 'inicio_semana']].duplicated().any():
        raise ValueError('Duplicate examples')
    if max(train['fim_semana']) >= min(evaluation['inicio_semana']):
        raise ValueError('Temporal overlap')
    return train, evaluation, features(train, window), features(evaluation, window), train['alvo_disponivel'].to_numpy(dtype=np.float64), evaluation[target].to_numpy(dtype=np.float64)


def baseline_predictions(frame):
    blocks = frame[[f'contagem_semana_anterior_{i:02}' for i in range(1, 5)]].to_numpy(dtype=np.float64)
    return {'zero': np.zeros(len(frame)), 'ultima_semana': blocks[:, 0], 'media_4_semanas': blocks.mean(axis=1)}


def metrics(y, prediction):
    y, prediction = np.asarray(y, dtype=np.float64), np.asarray(prediction, dtype=np.float64)
    if y.shape != prediction.shape or not len(y) or not np.isfinite(prediction).all() or not np.isfinite(y).all() or (prediction < 0).any():
        raise ValueError('Invalid targets or predictions')
    errors = prediction - y
    return dict(n=len(y), mae=float(np.abs(errors).mean()), rmse=float(np.sqrt(np.square(errors).mean())), alvo_total=float(y.sum()), previsao_total=float(prediction.sum()))


def choose_best(rows, tolerance=1e-6):
    best_mae = min(row['mae'] for row in rows)
    candidates = [r for r in rows if r['mae'] - best_mae < tolerance]
    return min(candidates, key=lambda r: (r['rmse'], r['window'], r['order']))


def prediction_frame(train, evaluation, y, prediction, model, window, phase):
    frame = evaluation[['bairro_chave', 'inicio_semana', 'fim_semana']].copy()
    volumes = train.groupby('bairro_chave')['alvo_disponivel'].sum()
    bounds = np.quantile(volumes.to_numpy(), [0.25, 0.5, 0.75])
    values = frame['bairro_chave'].map(volumes)
    frame['categoria_desconhecida'] = values.isna()
    frame['faixa_volume_treino'] = ['sem_historico_ajuste' if pd.isna(v) else f'Q{np.searchsorted(bounds, v, side="left") + 1}' for v in values]
    frame['alvo'] = y
    frame['previsao'] = prediction
    frame['modelo'] = model
    frame['janela_dias'] = window
    frame['fase'] = phase
    frame['alvo_recente'] = False
    if phase == 'avaliacao_2026':
        frame['alvo_recente'] = [d + timedelta(days=28) > date(2026, 6, 30) for d in frame['fim_semana']]
    return frame, bounds.tolist()


def grouped_metrics(frame):
    rows = []
    for model, predictions in frame.groupby('modelo', sort=True):
        for dimension in [None, 'inicio_semana', 'bairro_chave', 'categoria_desconhecida', 'faixa_volume_treino', 'alvo_recente']:
            groups = [('todos', predictions)] if dimension is None else predictions.groupby(dimension, sort=True)
            for value, group in groups:
                rows.append(dict(modelo=model, dimensao=dimension or 'geral', grupo=str(value), **metrics(group['alvo'], group['previsao'])))
    return pd.DataFrame(rows)


def block_bootstrap(frame, winner, reference, repetitions=1000, block=4, seed=42):
    a = frame[frame['modelo'] == winner].sort_values(['inicio_semana', 'bairro_chave'])
    b = frame[frame['modelo'] == reference].sort_values(['inicio_semana', 'bairro_chave'])
    if not a[['inicio_semana', 'bairro_chave']].reset_index(drop=True).equals(b[['inicio_semana', 'bairro_chave']].reset_index(drop=True)) or not np.array_equal(a['alvo'], b['alvo']):
        raise ValueError('Bootstrap requires identical pairs and targets')
    delta = a[['inicio_semana']].copy()
    delta['difference'] = np.abs(a['previsao'].to_numpy() - a['alvo'].to_numpy()) - np.abs(b['previsao'].to_numpy() - b['alvo'].to_numpy())
    weekly = delta.groupby('inicio_semana')['difference'].agg(['sum', 'count'])
    n = len(weekly)
    if n < block:
        raise ValueError('Too few weeks for bootstrap')
    rng = np.random.default_rng(seed)
    estimates = []
    for _ in range(repetitions):
        starts = rng.integers(0, n - block + 1, size=math.ceil(n / block))
        indices = np.concatenate([np.arange(start, start + block) for start in starts])[:n]
        sample = weekly.iloc[indices]
        estimates.append(float(sample['sum'].sum() / sample['count'].sum()))
    return dict(vencedor=winner, referencia=reference, diferenca_mae=float(delta['difference'].mean()), intervalo_95=np.quantile(estimates, [0.025, 0.975]).tolist(), repeticoes=repetitions, bloco_semanas=block, semanas=n, interpretacao='A negative difference favors the winner; approximate CI using moving blocks in evaluation-week order.')
