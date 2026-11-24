
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone, timedelta
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import time


for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[name] = '4'

import joblib
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from threadpoolctl import threadpool_limits

try:
    from .prepare_weekly_data import ROOT, sha256, write_json
    from .training_common import baseline_predictions, block_bootstrap, choose_best, grouped_metrics, load_config, metrics, prediction_frame, prepare_phase, preprocessor, verify_inputs
except ImportError:
    from prepare_weekly_data import ROOT, sha256, write_json
    from training_common import baseline_predictions, block_bootstrap, choose_best, grouped_metrics, load_config, metrics, prediction_frame, prepare_phase, preprocessor, verify_inputs


def model_builder(family):
    if family == 'rf':
        try:
            from .model_random_forest import build_model
        except ImportError:
            from model_random_forest import build_model
    elif family == 'xgb':
        try:
            from .model_xgboost import build_model
        except ImportError:
            from model_xgboost import build_model
    else:
        raise ValueError(family)
    return build_model


def provenance(source):
    return dict(config_sha256=sha256(ROOT / 'config/experimento_semanal.json'),
                scripts_sha256={p.name: sha256(p) for p in sorted((ROOT / 'scripts').glob('*.py'))},
                weekly_manifest_sha256=sha256(source / 'manifest.json'),
                python=platform.python_version(),
                versions={name: importlib.metadata.version(name) for name in ['numpy', 'scipy', 'pandas', 'pyarrow', 'scikit-learn', 'xgboost', 'joblib', 'matplotlib']})


def load_data(source, phase, window, config, ledger):
    train, evaluation, xtrain, xeval, ytrain, y = prepare_phase(source, phase, window, config, ledger)
    encoder = preprocessor(window)
    a = encoder.fit_transform(xtrain)
    b = encoder.transform(xeval)
    return dict(train=train, evaluation=evaluation, xtrain=a, xeval=b, ytrain=ytrain, y=y, encoder=encoder)


def check_population(reference, data):
    for key in ('train', 'evaluation'):
        columns = ['bairro_chave', 'inicio_semana', 'fim_semana']
        if not reference[key][columns].equals(data[key][columns]):
            raise ValueError('Windows contain different pairs')
    if not np.array_equal(reference['ytrain'], data['ytrain']) or not np.array_equal(reference['y'], data['y']):
        raise ValueError('Windows contain different targets')
    columns = [f'contagem_semana_anterior_{i:02}' for i in range(1, 5)]
    if not reference['evaluation'][columns].equals(data['evaluation'][columns]):
        raise ValueError('Inconsistent baselines across windows')


def verify_selection(output, prov):
    path = output / 'selecao.json'
    selection = json.loads(path.read_text(encoding='utf-8'))
    expected = (output / 'selecao.sha256').read_text(encoding='utf-8').strip()
    if sha256(path) != expected or selection['provenance'] != prov:
        raise ValueError('Selection or protocol changed after freezing')
    return selection


def verify_completed(destination, prov, selection_path):
    marker = json.loads((destination / 'concluido.json').read_text(encoding='utf-8'))
    if marker['provenance'] != prov or marker['selection_sha256'] != sha256(selection_path):
        raise ValueError('Result belongs to another selection or protocol')
    for name, expected in marker['outputs_sha256'].items():
        if sha256(destination / name) != expected:
            raise ValueError(f'Result has changed: {destination / name}')


def fit_predict(family, params, data, config):
    estimator = model_builder(family)(params, seed=config['seed'], threads=config['threads'])
    start = time.perf_counter()
    with threadpool_limits(limits=config['threads']):
        estimator.fit(data['xtrain'], data['ytrain'])
        fit_seconds = time.perf_counter() - start
        prediction = np.maximum(0, estimator.predict(data['xeval']).astype(np.float64))
    return estimator, prediction, fit_seconds, time.perf_counter() - start


def make_predictions(data, prediction, name, window, phase):
    return prediction_frame(data['train'], data['evaluation'], data['y'], prediction, name, window, phase)


def save_phase_summary(destination, frames, records):
    frame = pd.concat(frames, ignore_index=True)
    frame.to_parquet(destination / 'previsoes.parquet', index=False, compression='zstd')
    grouped_metrics(frame).to_csv(destination / 'metricas.csv', index=False)
    write_json(destination / 'execucoes.json', records)
    make_plots(destination, frame)
    return frame


def make_plots(destination, frame):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    totals = frame.groupby(['modelo', 'inicio_semana'])[['alvo', 'previsao']].sum()
    fig, ax = plt.subplots(figsize=(11, 4.5), layout='constrained')
    first = next(iter(frame['modelo'].unique()))
    real = totals.loc[first]
    ax.plot(real.index, real['alvo'], color='black', linewidth=2, label='Observed in the evaluated population')
    for model in frame['modelo'].unique():
        if model == 'zero':
            continue
        values = totals.loc[model]
        ax.plot(values.index, values['previsao'], label=model, alpha=.8)
    ax.set(xlabel='Forecast week', ylabel='Records', title='Weekly counts in the evaluated neighborhoods')
    ax.legend(fontsize=8)
    fig.savefig(destination / 'contagens_semanais.png', dpi=160)
    plt.close(fig)
    rows = [(model, metrics(g['alvo'], g['previsao'])) for model, g in frame.groupby('modelo')]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), layout='constrained')
    for ax, metric in zip(axes, ['mae', 'rmse']):
        ax.barh([r[0] for r in rows], [r[1][metric] for r in rows])
        ax.set(xlabel=metric.upper(), title=f'Comparison by {metric.upper()}')
    fig.savefig(destination / 'comparacao_erros.png', dpi=160)
    plt.close(fig)


def maturity_report(source, cutoff):
    eligible, covered = 0, 0
    for path in sorted((source.parent / 'registros').glob('*.parquet')):
        data = pq.read_table(path, columns=['elegivel_painel', 'data_registro', 'data_ocorrencia', 'atraso_registro_dias']).to_pydict()
        for flag, registered, event, delay in zip(*data.values()):
            if flag and registered < cutoff and event < cutoff:
                eligible += 1
                covered += delay <= 28
    return dict(registros_disponiveis=eligible, atraso_ate_28_dias=covered, proporcao=float(covered / eligible), aviso='Proportion among records already available at the cutoff; does not estimate reports not yet registered.')


def validate(source, output, config, prov, ledger):
    destination = output / 'validacao'
    destination.mkdir(parents=True, exist_ok=True)
    selection_path = output / 'selecao.json'
    if selection_path.exists():
        verify_selection(output, prov)
        write_report(output)
        print('Selection is already frozen; use the test or supplementary evaluation phases.', flush=True)
        return
    rows, baseline_rows = [], []
    data_cache = {}
    for family in ('rf', 'xgb'):
        for window in config['windows']:
            if window not in data_cache:
                data_cache[window] = load_data(source, 'validacao', window, config, ledger)
                if len(data_cache) > 1:
                    check_population(data_cache[config['windows'][0]], data_cache[window])
            data = data_cache[window]
            for order, grid in enumerate(config[family]['grid']):
                params = config[family]['fixed'] | grid
                name = f'{family}_w{window}_c{order}'
                record_path = destination / f'{name}.json'
                prediction_path = destination / f'{name}.parquet'
                if record_path.exists() and prediction_path.exists():
                    row = json.loads(record_path.read_text(encoding='utf-8'))
                    if row['provenance'] != prov or row['prediction_sha256'] != sha256(prediction_path):
                        raise ValueError(f'Incompatible cache: {name}')
                    print(f'{name}: Existing result verified', flush=True)
                else:
                    print(f'Training {name}: {len(data["ytrain"])} examples', flush=True)
                    estimator, prediction, fit_seconds, total_seconds = fit_predict(family, params, data, config)
                    frame, bounds = make_predictions(data, prediction, name, window, 'validacao')
                    frame.to_parquet(prediction_path, index=False, compression='zstd')
                    row = dict(family=family, model=name, window=window, order=order, params=params, **metrics(data['y'], prediction), fit_seconds=fit_seconds, total_seconds=total_seconds, treino_n=len(data['ytrain']), quartis_volume=bounds, provenance=prov, prediction_sha256=sha256(prediction_path))
                    write_json(record_path, row)
                    print(f'{name}: MAE={row["mae"]:.6f}; RMSE={row["rmse"]:.6f}; fit={fit_seconds:.1f}s', flush=True)
                    del estimator
                rows.append(row)
    data = data_cache[28]
    frames = []
    for order, (name, prediction) in enumerate(baseline_predictions(data['evaluation']).items()):
        frame, _ = make_predictions(data, prediction, name, 28, 'validacao')
        frames.append(frame)
        baseline_rows.append(dict(model=name, window=28, order=order, **metrics(data['y'], prediction)))
    winners = {family: choose_best([r for r in rows if r['family'] == family], config['selection_tolerance']) for family in ('rf', 'xgb')}
    best_model = choose_best(list(winners.values()), config['selection_tolerance'])
    reference = choose_best(baseline_rows, config['selection_tolerance'])
    reproduction = {}
    for family, row in winners.items():
        print(f'Reproducing the validation winner: {row["model"]}', flush=True)
        estimator, prediction, _, _ = fit_predict(family, row['params'], data_cache[row['window']], config)
        saved = pd.read_parquet(destination / f'{row["model"]}.parquet')
        np.testing.assert_allclose(prediction, saved['previsao'], rtol=1e-6, atol=1e-7)
        reproduction[family] = dict(rtol=1e-6, atol=1e-7, diferenca_maxima=float(np.max(np.abs(prediction - saved['previsao']))))
        saved['modelo'] = family
        frames.append(saved)
        del estimator
    frame = save_phase_summary(destination, frames, rows + baseline_rows)

    selection = dict(provenance=prov, created_at_utc=datetime.now(timezone.utc).isoformat(), winners=winners, best_family=best_model['family'], best_reference=reference['model'], baselines=baseline_rows, reproduction=reproduction, score_cutoff='2025-01-01', maturity_days=config['maturity_days'], score_weeks=int(frame['inicio_semana'].nunique()), score_target='Records available before 2025-01-01; weeks matured for 28 days.', maturity=maturity_report(source, date(2024, 1, 1)))
    write_json(selection_path, selection)
    (output / 'selecao.sha256').write_text(sha256(selection_path) + '\n', encoding='utf-8')
    write_report(output)
    print(f'Selection frozen: {selection["best_family"]}; reference: {selection["best_reference"]}', flush=True)


def evaluate(source, output, phase, config, prov, ledger):
    selection_path = output / 'selecao.json'
    selection = verify_selection(output, prov)
    if phase == 'avaliacao_2026' and not (output / 'teste/concluido.json').exists():
        raise ValueError('Complete the test before supplementary evaluation')
    if phase == 'avaliacao_2026':
        verify_completed(output / 'teste', prov, selection_path)
    destination = output / phase
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / 'concluido.json').exists():
        verify_completed(destination, prov, selection_path)
        write_report(output)
        print(f'{phase} already completed; results preserved.', flush=True)
        return
    frames, records, data_cache = [], [], {}
    for family, row in selection['winners'].items():
        window = row['window']
        if window not in data_cache:
            data_cache[window] = load_data(source, phase, window, config, ledger)
        data = data_cache[window]
        print(f'{phase}: training {family}, window={window}, n={len(data["ytrain"])}', flush=True)
        estimator, prediction, fit_seconds, total_seconds = fit_predict(family, row['params'], data, config)
        frame, bounds = make_predictions(data, prediction, family, window, phase)
        frames.append(frame)
        records.append(dict(model=family, window=window, params=row['params'], **metrics(data['y'], prediction), fit_seconds=fit_seconds, total_seconds=total_seconds, treino_n=len(data['ytrain']), quartis_volume=bounds))
        models = destination / 'models'
        models.mkdir(exist_ok=True)
        model_path = models / f'{family}.joblib'
        joblib.dump(dict(encoder=data['encoder'], estimator=estimator, window=window, cutoff=config['phases'][phase]['fit_cutoff'], provenance=prov), model_path, compress=3)
        restored = joblib.load(model_path)
        with threadpool_limits(limits=config['threads']):
            np.testing.assert_allclose(np.maximum(0, restored['estimator'].predict(data['xeval'])), prediction, rtol=1e-6, atol=1e-7)
        del estimator, restored
        print(f'{phase}/{family}: MAE={records[-1]["mae"]:.6f}; fit={fit_seconds:.1f}s', flush=True)

    a, b = frames
    if not a[['bairro_chave', 'inicio_semana', 'alvo']].equals(b[['bairro_chave', 'inicio_semana', 'alvo']]):
        raise ValueError('Different evaluation populations')
    data = next(iter(data_cache.values()))
    for name, prediction in baseline_predictions(data['evaluation']).items():
        frame, _ = make_predictions(data, prediction, name, 28, phase)
        frames.append(frame)
        records.append(dict(model=name, window=28, **metrics(data['y'], prediction)))
    frame = save_phase_summary(destination, frames, records)
    if phase == 'teste':
        bootstrap = block_bootstrap(frame, selection['best_family'], selection['best_reference'], config['bootstrap']['repetitions'], config['bootstrap']['block_weeks'], config['seed'])
        write_json(destination / 'bootstrap.json', bootstrap)
    marker = dict(provenance=prov, selection_sha256=sha256(selection_path), maturity=maturity_report(source, date.fromisoformat(config['phases'][phase]['fit_cutoff'])), outputs_sha256={p.relative_to(destination).as_posix(): sha256(p) for p in sorted(destination.rglob('*')) if p.is_file()})
    write_json(destination / 'concluido.json', marker)
    write_report(output)


def write_report(output):
    selection = json.loads((output / 'selecao.json').read_text(encoding='utf-8'))
    lines = ['# Weekly forecasting of robbery records', '', 'SSP-SP, occurrence municipality of São Paulo. Target: records by offense classification, not unique crimes or individual risk. No socioeconomic integration.', '', '## Protocol', '', 'Global models remain fixed within each year, with inputs updated weekly. Fit cutoffs are 2024-01-01, 2025-01-01 and 2026-01-01. Training targets are reconstructed at the cutoff, using weeks matured for at least 28 days. Neighborhood and month encoding is fitted only on training data. Negative predictions are clipped to zero without rounding.', '', f'Selection: {selection["score_weeks"]} weeks in 2024, with labels available before 2025-01-01 and a 28-day maturity margin. Pre-test winner: **{selection["best_family"]}**; pre-test reference: **{selection["best_reference"]}**. Test results do not change any choices.', '', '## Selected configurations', '']
    for family, row in selection['winners'].items():
        lines.append(f'- {family}: window {row["window"]} days; parameters `{json.dumps(row["params"], ensure_ascii=False, sort_keys=True)}`.')
    for phase in ('validacao', 'teste', 'avaliacao_2026'):
        path = output / phase / 'metricas.csv'
        if not path.exists():
            continue
        table = pd.read_csv(path)
        table = table[table['dimensao'] == 'geral']
        lines += ['', f'## {phase}', '', '| Model | Examples | MAE | RMSE |', '|---|---:|---:|---:|']
        lines += [f'| {r.modelo} | {r.n} | {r.mae:.6f} | {r.rmse:.6f} |' for r in table.itertuples()]
        lines += ['', f'![Error comparison]({phase}/comparacao_erros.png)', '', f'![Weekly counts]({phase}/contagens_semanais.png)']
        if phase != 'validacao':
            winner_mae = float(table.loc[table['modelo'] == selection['best_family'], 'mae'].iloc[0])
            reference_mae = float(table.loc[table['modelo'] == selection['best_reference'], 'mae'].iloc[0])
            lines += ['', f'MAE difference between the preselected winner and the preselected reference: {winner_mae - reference_mae:.6f}. ' + ('MAE was reduced in this population.' if winner_mae < reference_mae else 'MAE was not reduced in this population; configurations were kept unchanged.')]
    if (output / 'teste/bootstrap.json').exists():
        bootstrap = json.loads((output / 'teste/bootstrap.json').read_text(encoding='utf-8'))
        lo, hi = bootstrap['intervalo_95']
        lines += ['', f'Approximate 95% CI for the test MAE difference: [{lo:.6f}, {hi:.6f}], from 1,000 resamples in four-week blocks. Negative differences favor the model. Blocks follow evaluated weeks, including any excluded calendar gaps.']
    lines += ['', '## Limitations and interpretation', '', '- Registration date is a proxy for public availability; complete historical snapshots of report versions are unavailable.', '- The 28-day margin reduces, but does not eliminate, incomplete training targets. Delay proportions in metadata include only records available at the cutoff.', '- Test targets are retrospective in the 2026-10-16 snapshot. Differences from the provisional labels used for selection must be considered when comparing phases.', '- Neighborhoods unknown at forecast origin are excluded from examples and counted in the weekly audit. Categories known at forecast origin but absent from training remain in evaluation and receive separate metrics.', '- Volume quartiles use sums of available training targets; tied boundaries may produce empty groups. These are not population rates.', '- Plot totals cover evaluated neighborhoods and do not necessarily represent the complete municipal total.', '- In 2026, weeks with fewer than 28 days before June 30 are marked as recent and also evaluated separately; earlier weeks may still lack late reports.', '- Results show predictive association within this scope; they do not explain crime causation or guarantee performance in other periods.', '', 'Predictions, grouped metrics, timings, versions, hashes and reproduction checks are stored in each phase directory and in selecao.json. Model weights are stored in the models subdirectories and can be rebuilt with the same commands.']
    (output / 'relatorio.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def smoke(source, output, config, ledger):
    data = load_data(source, 'validacao', 28, config, ledger)
    rows = np.linspace(0, len(data['ytrain']) - 1, min(2000, len(data['ytrain'])), dtype=int)
    columns = np.linspace(0, len(data['y']) - 1, min(512, len(data['y'])), dtype=int)
    small = data | dict(xtrain=data['xtrain'][rows], ytrain=data['ytrain'][rows], xeval=data['xeval'][columns])
    results = {}
    for family in ('rf', 'xgb'):
        params = config[family]['fixed'] | config[family]['grid'][0] | dict(n_estimators=3, max_depth=3)
        _, prediction, _, _ = fit_predict(family, params, small, config)
        results[family] = metrics(data['y'][columns], prediction)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / 'smoke.json', results)
    print(json.dumps(results, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description='Run weekly forecasting experiments with selection before testing.')
    parser.add_argument('--phase', choices=['smoke', 'validacao', 'teste', 'avaliacao_2026'], required=True)
    parser.add_argument('--source', type=Path, default=ROOT / 'normalized-data/modelagem')
    parser.add_argument('--output', type=Path, default=ROOT / 'results')
    args = parser.parse_args()
    config = load_config()
    verify_inputs(args.source)
    ledger = pq.read_table(args.source / 'contribuicoes_alvo.parquet')
    prov = provenance(args.source)
    if args.phase == 'smoke':
        smoke(args.source, args.output / 'smoke', config, ledger)
    elif args.phase == 'validacao':
        validate(args.source, args.output, config, prov, ledger)
    else:
        evaluate(args.source, args.output, args.phase, config, prov, ledger)


if __name__ == '__main__':
    main()
