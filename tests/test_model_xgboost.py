import json
from pathlib import Path
import unittest

import numpy as np
from scipy.sparse import csr_matrix

from scripts.model_xgboost import build_model


ROOT = Path(__file__).resolve().parents[1]


class XGBoostTests(unittest.TestCase):
    def setUp(self):
        config = json.loads((ROOT / 'config/experimento_semanal.json').read_text(encoding='utf-8'))
        self.params = {**config['xgb']['fixed'], **config['xgb']['grid'][0]}

    def test_config_and_resource_limits(self):
        model = build_model(self.params, threads=12)
        actual = model.get_params()
        for name, value in self.params.items():
            self.assertEqual(actual[name], value)
        self.assertEqual(actual['random_state'], 42)
        self.assertEqual(actual['n_jobs'], 4)
        self.assertIsNone(actual['early_stopping_rounds'])
        self.assertEqual(build_model(self.params, seed=7, threads=0).get_params()['n_jobs'], 1)
        self.assertEqual(build_model(self.params, seed=7).get_params()['random_state'], 7)

    def test_sparse_fit_and_reproducibility(self):
        inputs = csr_matrix(np.array([
            [1, 0, 0], [1, 0, 1], [0, 1, 2], [0, 1, 3],
            [1, 0, 4], [1, 0, 5], [0, 1, 6], [0, 1, 7],
        ], dtype=np.float32))
        targets = np.array([0, 1, 1, 2, 3, 3, 4, 5], dtype=np.float32)
        params = {**self.params, 'n_estimators': 4}
        first = build_model(params).fit(inputs, targets).predict(inputs)
        repeated = build_model(params).fit(inputs, targets).predict(inputs)
        self.assertEqual(first.shape, targets.shape)
        self.assertTrue(np.isfinite(first).all())
        self.assertGreater(np.ptp(first), 0)
        np.testing.assert_allclose(first, repeated, rtol=1e-7, atol=1e-7)


if __name__ == '__main__':
    unittest.main()
