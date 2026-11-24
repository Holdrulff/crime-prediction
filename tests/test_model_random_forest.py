import unittest

import numpy as np
from scipy.sparse import csr_matrix

from scripts.model_random_forest import build_model
from scripts.training_common import load_config


class RandomForestTests(unittest.TestCase):
    def test_sparse_predictions_are_finite_nonnegative_and_reproducible(self):
        config = load_config()['rf']
        params = {**config['fixed'], **config['grid'][0], 'n_estimators': 4}
        x = csr_matrix(np.arange(120, dtype=np.float32).reshape(40, 3) % 13)
        y = np.arange(40, dtype=np.float64) % 7
        first = build_model(params).fit(x, y)
        second = build_model(params).fit(x, y)
        predictions = first.predict(x)
        self.assertTrue(np.isfinite(predictions).all())
        self.assertTrue((predictions >= 0).all())
        np.testing.assert_allclose(predictions, second.predict(x), rtol=0, atol=1e-12)
        self.assertEqual(params['n_estimators'], 4)

    def test_config_parameters_and_worker_limit(self):
        config = load_config()['rf']
        params = {**config['fixed'], **config['grid'][0]}
        model = build_model(params, seed=17, threads=12)
        for name, value in params.items():
            self.assertEqual(model.get_params()[name], value)
        self.assertEqual(model.n_jobs, 4)
        self.assertEqual(model.random_state, 17)
        self.assertFalse(hasattr(model, 'estimators_'))
        for threads in [0, -1, True, 1.5]:
            with self.assertRaises(ValueError):
                build_model(params, threads=threads)
