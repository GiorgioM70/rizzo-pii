# -*- coding: utf-8 -*-
"""Le versioni delle dipendenze dell'app sono bloccate e uguali in ogni file che le installa.

Prima ogni build aveva la sua lista: requirements.txt senza versioni, Dockerfile con torch
e transformers pinnati e il resto libero, Dockerfile.linux con il solo transformers. La
stessa build ripetuta fra qualche mese avrebbe tirato versioni diverse.

Eccezione voluta: il Dockerfile del server usa transformers 5.x, che richiede
huggingface_hub 1.x; le build PyInstaller (desktop) restano su transformers 4.57.3 e
quindi su huggingface_hub 0.36.x. Il resto e' identico ovunque.

Nessuna installazione: legge solo i file.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMUNI = {"tokenizers": "0.22.2", "safetensors": "0.8.0",
          "flask": "3.1.3", "pymupdf": "1.28.2"}
DESKTOP = {**COMUNI, "torch": "2.13.0", "transformers": "4.57.3",
           "huggingface_hub": "0.36.2", "pyinstaller": "6.22.3"}
SERVER = {**COMUNI, "torch": "2.13.0", "transformers": "5.14.1",
          "huggingface_hub": "1.33.0", "gunicorn": "27.0.1"}

FILE_DESKTOP = ["Dockerfile.linux", "build_linux.sh", "build_macos.sh",
                "docs/BUILD.md", ".github/workflows/build-windows.yml"]

# nome[extra]==versione, con o senza virgolette
_PIN = re.compile(r"(?<![\w.-])([A-Za-z][\w.-]*)(?:\[[\w,]+\])?==([\w.]+)")
# nomi che devono comparire solo pinnati nei comandi pip install
_NOMI = sorted({*DESKTOP, *SERVER}, key=len, reverse=True)


def pin(path):
    testo = (ROOT / path).read_text(encoding="utf-8")
    trovati = {}
    for nome, ver in _PIN.findall(testo):
        trovati.setdefault(nome.lower().replace("-", "_"), set()).add(ver)
    return testo, trovati


def righe_pip(testo):
    """Le righe di `pip install` con le loro continuazioni (\\ o ` in PowerShell)."""
    righe, dentro = [], False
    for r in testo.splitlines():
        if "pip" in r and "install" in r:
            dentro = True
        if dentro:
            righe.append(r)
            dentro = r.rstrip().endswith(("\\", "`"))
    return righe


class VersioniBloccate(unittest.TestCase):
    def _controlla(self, path, attese):
        testo, trovati = pin(path)
        for nome, ver in attese.items():
            if nome not in trovati and nome in ("gunicorn", "pyinstaller", "torch"):
                continue                         # non tutti i file installano tutto
            with self.subTest(file=path, pacchetto=nome):
                self.assertEqual(trovati.get(nome), {ver})
        # nessun pacchetto dell'app installato senza versione
        for r in righe_pip(testo):
            for tok in r.split():
                tok = tok.strip("\"'`\\")
                nome = tok.split("==")[0].split("[")[0].lower().replace("-", "_")
                if nome in attese and "==" not in tok:
                    self.fail(f"{path}: '{tok}' senza versione in: {r.strip()}")

    def test_build_desktop(self):
        for path in FILE_DESKTOP:
            self._controlla(path, DESKTOP)

    def test_dockerfile_server(self):
        self._controlla("Dockerfile", SERVER)

    def test_requirements(self):
        _, trovati = pin("requirements.txt")
        for nome in ("transformers", "tokenizers", "safetensors", "huggingface_hub",
                     "flask", "pymupdf"):
            self.assertEqual(trovati.get(nome), {DESKTOP[nome]}, nome)

    def test_ci_test_usa_lo_stesso_pymupdf(self):
        _, trovati = pin(".github/workflows/tests.yml")
        self.assertEqual(trovati.get("pymupdf"), {COMUNI["pymupdf"]})
        self.assertEqual(trovati.get("flask"), {COMUNI["flask"]})

    def test_niente_import_fitz_deprecato(self):
        """`import fitz` e' il nome storico: PyMuPDF raccomanda `import pymupdf`."""
        vecchi = []
        for p in list((ROOT / "src").rglob("*.py")) + list((ROOT / "tests").rglob("*.py")):
            if p.name == Path(__file__).name:
                continue
            for i, r in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if re.match(r"\s*(import fitz\b|from fitz\b)", r):
                    vecchi.append(f"{p.relative_to(ROOT)}:{i}")
        self.assertEqual(vecchi, [])


if __name__ == "__main__":
    unittest.main()
