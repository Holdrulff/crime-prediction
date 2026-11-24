"""Adaptador XGBoost para os experimentos semanais compartilhados."""

from xgboost import XGBRegressor


def build_model(params: dict, seed: int = 42, threads: int = 4) -> XGBRegressor:
    return XGBRegressor(**{
        **params,
        'random_state': seed,
        'n_jobs': max(1, min(threads, 4)),
        'tree_method': 'hist',
        'device': 'cpu',
        'early_stopping_rounds': None,
    })
