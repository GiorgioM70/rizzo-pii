# -*- coding: utf-8 -*-
"""UI in file statici e modello caricato in modo pigro.

- `GET /` serve static/index.html con la versione sostituita e i riferimenti a
  /static/app.css e /static/app.js, che a loro volta rispondono 200;
- `import app` non carica il modello (nlp resta None finche' qualcuno non chiama
  load_model() o fa un'inferenza) e non importa torch.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from test_ora import APP, client  # noqa: E402


@unittest.skipIf(APP is None, "app.py non importabile (flask/fitz assenti)")
class UiStatica(unittest.TestCase):
    def setUp(self):
        self.c = client()

    def test_index_con_versione_e_risorse(self):
        r = self.c.get("/", headers={"Host": "127.0.0.1:5005"})
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("__VERSION__", html)
        self.assertIn(f"v{APP.APP_VERSION}", html)
        self.assertIn("/static/app.css", html)
        self.assertIn("/static/app.js", html)

    def test_risorse_statiche(self):
        for path, pezzo in (("/static/app.js", "function"), ("/static/app.css", "{")):
            r = self.c.get(path, headers={"Host": "127.0.0.1:5005"})
            self.assertEqual(r.status_code, 200, path)
            self.assertIn(pezzo, r.get_data(as_text=True))
            r.close()

    def test_modello_pigro(self):
        # nessuno ha chiamato load_model(): l'import non ha caricato niente
        self.assertIsNone(APP.nlp)
        self.assertTrue(callable(APP.load_model))
        r = self.c.get("/health", headers={"Host": "127.0.0.1:5005"})
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
