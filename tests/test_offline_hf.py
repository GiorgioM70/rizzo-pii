# -*- coding: utf-8 -*-
"""L'app gira offline anche fuori dal Docker: HF_HUB_OFFLINE e TRANSFORMERS_OFFLINE.

Il Dockerfile li imposta da sempre; l'app desktop (serve.py, desktop_app.py) e
`python src/app/app.py` no, quindi transformers/huggingface_hub potevano cercare la
rete (controllo versioni, file mancanti). Ora li fissa app.py, prima che
transformers venga importato, con setdefault: chi vuole la rete mette
HF_HUB_OFFLINE=0.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "src" / "app"

# importa app con un transformers finto (come tests/test_ora.py) e stampa le variabili
SONDA = r"""
import os, sys
from unittest.mock import MagicMock
sys.modules["transformers"] = MagicMock()
import app
print(os.environ.get("HF_HUB_OFFLINE"), os.environ.get("TRANSFORMERS_OFFLINE"))
"""


def sonda(**env_extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")}
    env.update(PII_MODEL_DIR=str(ROOT / "tests"), PYTHONPATH=str(APP_DIR),
               PYTHONIOENCODING="utf-8", **env_extra)
    return subprocess.run([sys.executable, "-c", SONDA], env=env, cwd=str(APP_DIR),
                          capture_output=True, text=True, timeout=60)


def _importabile():
    try:
        import flask  # noqa: F401
        import pymupdf  # noqa: F401
        return True
    except ImportError:
        return False


class OfflineHF(unittest.TestCase):
    def test_impostate_prima_di_transformers(self):
        src = (APP_DIR / "app.py").read_text(encoding="utf-8")
        i = src.index('os.environ.setdefault("HF_HUB_OFFLINE", "1")')
        self.assertIn('os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")', src)
        self.assertLess(i, src.index("from transformers import"))
        self.assertNotIn("\nimport transformers", src[:i])
        self.assertNotIn("\nimport huggingface_hub", src[:i])

    @unittest.skipUnless(_importabile(), "flask/pymupdf assenti")
    def test_import_di_app_le_imposta(self):
        r = sonda()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split()[-2:], ["1", "1"])

    @unittest.skipUnless(_importabile(), "flask/pymupdf assenti")
    def test_valore_esplicito_rispettato(self):
        r = sonda(HF_HUB_OFFLINE="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split()[-2:], ["0", "1"])


if __name__ == "__main__":
    unittest.main()
