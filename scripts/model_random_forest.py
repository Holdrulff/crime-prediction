
from sklearn.ensemble import RandomForestRegressor


def build_model(params: dict, seed: int = 42, threads: int = 4) -> RandomForestRegressor:
    if not isinstance(threads, int) or isinstance(threads, bool) or threads < 1:
        raise ValueError('threads must be a positive integer')
    options = dict(params)
    options.update(random_state=seed, n_jobs=min(threads, 4))
    return RandomForestRegressor(**options)
