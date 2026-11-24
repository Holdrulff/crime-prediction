from datetime import date, timedelta
import unittest

import numpy as np
import pandas as pd
import pyarrow as pa

from scripts.prepare_weekly_data import available_targets
from scripts.training_common import baseline_predictions, block_bootstrap, choose_best, feature_names, features, mature, metrics, preprocessor


class TrainingCommonTests(unittest.TestCase):
    def test_metrics_and_baselines(self):
        result = metrics([0, 2], [1, 4])
        self.assertEqual(result['mae'], 1.5)
        self.assertAlmostEqual(result['rmse'], np.sqrt(2.5))
        frame = pd.DataFrame({f'contagem_semana_anterior_{i:02}': [i, 0] for i in range(1, 5)})
        pred = baseline_predictions(frame)
        np.testing.assert_array_equal(pred['ultima_semana'], [1, 0])
        np.testing.assert_array_equal(pred['media_4_semanas'], [2.5, 0])
        with self.assertRaises(ValueError):
            metrics([0], [-1])

    def test_no_target_columns_or_future_categories(self):
        frame = pd.DataFrame({name: ['A', 'B'] if name == 'bairro_chave' else [1, 2] for name in feature_names(28)})
        frame['alvo_retrospectivo'] = [999, 999]
        x = features(frame, 28)
        self.assertNotIn('alvo_retrospectivo', x)
        encoder = preprocessor(28)
        encoder.fit(x)
        future = x.iloc[:1].copy()
        future['bairro_chave'] = 'FUTURO'
        encoder.transform(future)
        self.assertNotIn('FUTURO', encoder.named_transformers_['categories'].categories_[0])

    def test_cutoff_and_maturation(self):
        end = date(2023, 12, 3)
        examples = pa.table({'bairro_chave': ['A', 'A'], 'inicio_semana': [end - timedelta(days=6), end + timedelta(days=1)], 'fim_semana': [end, end + timedelta(days=7)], 'alvo_retrospectivo': [9, 9]})
        ledger = pa.table({'bairro_chave': ['A', 'A'], 'inicio_semana': [end - timedelta(days=6)] * 2, 'data_registro': [date(2023, 12, 20), date(2024, 1, 1)], 'quantidade_registros': [2, 7]})
        result = mature(available_targets(examples, ledger, date(2024, 1, 1)), date(2024, 1, 1), 28)
        self.assertEqual(result['alvo_disponivel'].to_pylist(), [2])

    def test_selection_and_paired_bootstrap(self):
        rows = [dict(mae=1, rmse=2, window=56, order=1), dict(mae=1 + 1e-7, rmse=1, window=28, order=0)]
        self.assertEqual(choose_best(rows)['window'], 28)
        records = []
        for i in range(8):
            for model, value in [('winner', 1), ('reference', 2)]:
                records.append(dict(inicio_semana=date(2025, 1, 6) + timedelta(weeks=i), bairro_chave='A', modelo=model, alvo=0, previsao=value))
        result = block_bootstrap(pd.DataFrame(records), 'winner', 'reference', 100, 4)
        self.assertEqual(result['diferenca_mae'], -1)
        self.assertEqual(result['intervalo_95'], [-1, -1])


if __name__ == '__main__':
    unittest.main()
