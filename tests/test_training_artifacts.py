from pathlib import Path
import tempfile
import unittest

from scripts.prepare_weekly_data import sha256, write_json
from scripts.train_models import verify_completed, verify_selection


class ArtifactTests(unittest.TestCase):
    def test_selection_and_result_corruption_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection = root / 'selecao.json'
            prov = {'version': 'test'}
            write_json(selection, {'provenance': prov})
            (root / 'selecao.sha256').write_text(sha256(selection), encoding='utf-8')
            self.assertEqual(verify_selection(root, prov)['provenance'], prov)
            dest = root / 'teste'
            dest.mkdir()
            result = dest / 'resultado.txt'
            result.write_text('ok', encoding='utf-8')
            write_json(dest / 'concluido.json', dict(provenance=prov, selection_sha256=sha256(selection), outputs_sha256={'resultado.txt': sha256(result)}))
            verify_completed(dest, prov, selection)
            result.write_text('alterado', encoding='utf-8')
            with self.assertRaises(ValueError):
                verify_completed(dest, prov, selection)
            write_json(selection, {'provenance': prov, 'alterado': True})
            with self.assertRaises(ValueError):
                verify_selection(root, prov)


if __name__ == '__main__':
    unittest.main()
