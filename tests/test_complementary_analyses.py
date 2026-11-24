from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from scipy.sparse import isspmatrix_csr

from scripts.analises_complementares import run as analysis


class ComplementaryTests(unittest.TestCase):
    def test_feature_groups_and_train_only_categories(self):
        complete = analysis.columns_for(['historico', 'resumos', 'calendario', 'bairro'])
        self.assertEqual(complete, analysis.feature_names(84))
        reduced = analysis.columns_for(['historico', 'resumos'])
        self.assertEqual(len(reduced), 16)
        self.assertNotIn('bairro_chave', reduced)
        self.assertNotIn('mes', reduced)
        for bad in [[], ['alvo'], ['historico', 'historico']]:
            with self.assertRaises(ValueError):
                analysis.columns_for(bad)
        frame = pd.DataFrame({c: ['A', 'B'] if c == 'bairro_chave' else
                              ['1', '2'] if c == 'mes' else np.array([0, 2], dtype=np.float32)
                              for c in complete})
        encoder = analysis.encoder_for(complete)
        encoder.fit(frame)
        future = frame.iloc[:1].copy()
        future['bairro_chave'], future['mes'] = 'FUTURO', '12'
        self.assertTrue(np.isfinite(encoder.transform(future).toarray()).all())
        categories = encoder.named_transformers_['categories'].categories_
        self.assertNotIn('FUTURO', categories[0])
        self.assertNotIn('12', categories[1])
        numeric = analysis.encoder_for(reduced).fit_transform(frame[reduced])
        np.testing.assert_array_equal(numeric, frame[reduced])
        a, b = analysis.encode_inputs(analysis.encoder_for(reduced), frame[reduced], frame[reduced])
        self.assertTrue(isspmatrix_csr(a) and isspmatrix_csr(b))
        np.testing.assert_array_equal(a.toarray(), numeric)
        full_a, full_b = analysis.encode_inputs(analysis.encoder_for(complete), frame, frame)
        self.assertTrue(isspmatrix_csr(full_a) and isspmatrix_csr(full_b))

    def test_manual_errors_and_reconciliation(self):
        frame = pd.DataFrame({'bairro_chave': ['A', 'B'], 'inicio_semana': [date(2025, 1, 6)]*2,
                              'alvo': [0., 2.], 'previsao': [1., 4.]})
        result = analysis.error_summary(frame)
        self.assertEqual(result['mae'], 1.5)
        self.assertEqual(result['erro_medio'], 1.5)
        self.assertEqual(result['soma_erro_absoluto'], 3)
        self.assertEqual(result['soma_erro_quadratico'], 5)
        self.assertEqual(result['proporcao_zero'], .5)
        self.assertAlmostEqual(result['rmse'], np.sqrt(2.5))
        rows = [analysis.error_summary(frame.iloc[[i]]) for i in range(2)]
        analysis.reconcile(rows, result)
        with self.assertRaises(AssertionError):
            analysis.reconcile(rows[:1], result)

    def test_pairing_bootstrap_and_empty_group_weeks(self):
        weeks = [date(2025, 1, 6) + timedelta(weeks=i) for i in range(8)]
        a = pd.DataFrame({'bairro_chave': ['A']*4, 'inicio_semana': weeks[:4],
                          'alvo': [0.]*4, 'previsao': [1.]*4})
        b = a.assign(previsao=2.)
        samples = analysis.block_indices(8, 100, 4, 42)
        np.testing.assert_array_equal(samples, analysis.block_indices(8, 100, 4, 42))
        for start in range(0, 8, 4):
            np.testing.assert_array_equal(np.diff(samples[:, start:start+4], axis=1), np.ones((100, 3)))
        result = analysis.contrast(a, b.iloc[::-1], weeks, samples)
        self.assertEqual(result['delta_mae'], -1)
        self.assertEqual(result['ic95_inferior'], -1)
        self.assertEqual(result['ic95_superior'], -1)
        self.assertEqual(result['repeticoes_validas'] + result['repeticoes_descartadas'], 100)
        self.assertIsNone(analysis.contrast(a.iloc[:3], b.iloc[:3], weeks, samples)['ic95_inferior'])
        with self.assertRaises(ValueError):
            analysis.aligned(a, b.assign(alvo=99))
        with self.assertRaises(ValueError):
            analysis.aligned(pd.concat([a, a]), b)

    def test_manifest_detects_changed_output_and_isolates_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / 'results/analises_complementares'
            dest = out / 'volume'
            dest.mkdir(parents=True)
            (root / 'scripts').mkdir()
            (root / 'config').mkdir()
            (root / 'config/experimento_semanal.json').write_text('{}')
            (root / 'results/original.csv').write_text('original')
            (out / 'registro.json').write_text('{}')
            saved = dest / 'resultado.csv'
            saved.write_text('ok')
            with patch.object(analysis, 'ROOT', root), patch.object(analysis, 'OUT', out):
                files = analysis.original_files()
                self.assertIn(root / 'results/original.csv', files)
                self.assertNotIn(saved, files)
                analysis.seal(dest)
                analysis.check_seal(dest)
                saved.write_text('corrompido')
                with self.assertRaises(ValueError):
                    analysis.check_seal(dest)


if __name__ == '__main__':
    unittest.main()
