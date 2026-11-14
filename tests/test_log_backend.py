# -*- coding: utf-8 -*-
"""Il log del backend (`serve.py`) deve sopravvivere al riavvio del sidecar.

Quando il backend non parte, lo splash di Tauri dice all'utente di guardare
`backend.log`. Ma il riavvio del sidecar (`retry_backend`, o l'utente che riapre
l'app) riapre quel file: se lo apre in troncamento, cancella proprio il log
dell'avvio fallito che si voleva leggere.

`serve.py` non e' importabile in un test (a import time carica il modello), quindi
qui si esegue il suo PREAMBOLO vero, ritagliato dal file, con la cartella dati
(LOCALAPPDATA / XDG_DATA_HOME / HOME) puntata a una cartella usa e getta: si testa il
codice che viene spedito, non una copia.

Il log sta nella cartella dati dell'app (`server_config.config_dir()`, la stessa di
config.json), non nella home: prima su macOS e Linux finiva in ~/rizzo-pii.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "src" / "app"
SERVE = APP_DIR / "serve.py"
LIB_RS = ROOT / "tauri" / "src-tauri" / "src" / "lib.rs"


def preambolo():
    """Le righe di serve.py fino a prima della lettura di host e porta."""
    src = SERVE.read_text(encoding="utf-8")
    testa = src[:src.index("HOST, PORT = server_config.resolve()")]
    assert "backend.log" in testa, "il preambolo del log non e' piu' li'"
    return testa


def avvia(testa, home, marcatore):
    """Un avvio del sidecar: esegue il preambolo e scrive un marcatore nel log."""
    script = Path(home) / "_avvio.py"
    script.write_text(testa + "\nprint(%r)\n" % marcatore, encoding="utf-8")
    env = dict(os.environ, LOCALAPPDATA=home, XDG_DATA_HOME=home, HOME=home,
               PYTHONPATH=str(APP_DIR), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(script)], env=env,
                          capture_output=True, text=True)


def cartella(home):
    """Dove server_config.config_dir() mette la cartella dati con quell'ambiente."""
    if sys.platform == "darwin":
        return Path(home) / "Library" / "Application Support" / "rizzo-pii"
    return Path(home) / "rizzo-pii"         # Windows: LOCALAPPDATA, Linux: XDG_DATA_HOME


def log(home, nome="backend.log"):
    p = cartella(home) / nome
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else None


class LogBackend(unittest.TestCase):
    def setUp(self):
        self.testa = preambolo()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def test_il_riavvio_non_cancella_il_log_precedente(self):
        avvia(self.testa, self.home, "AVVIO-FALLITO")
        avvia(self.testa, self.home, "RIAVVIO")
        testo = log(self.home) or ""
        self.assertIn("AVVIO-FALLITO", testo, "il log del crash e' stato cancellato")
        self.assertIn("RIAVVIO", testo)

    def test_ruota_oltre_il_massimo_invece_di_crescere(self):
        d = cartella(self.home)
        d.mkdir(parents=True)
        (d / "backend.log").write_text("VECCHIO\n" + "x" * 1_100_000, encoding="utf-8")
        avvia(self.testa, self.home, "DOPO-ROTAZIONE")
        self.assertIn("VECCHIO", log(self.home, "backend.log.1") or "")
        nuovo = log(self.home) or ""
        self.assertIn("DOPO-ROTAZIONE", nuovo)
        self.assertLess(len(nuovo), 10_000, "il log non e' ripartito dopo la rotazione")
        # lo splash manda l'utente su backend.log: se la traccia e' finita in .1,
        # il file che il messaggio nomina deve almeno dirglielo
        self.assertIn("backend.log.1", nuovo)

    def test_tre_avvii_di_fila_nessuno_perso(self):
        for k in range(3):
            avvia(self.testa, self.home, "AVVIO-%d" % k)
        testo = log(self.home) or ""
        for k in range(3):
            self.assertIn("AVVIO-%d" % k, testo)

    def test_se_la_rotazione_fallisce_si_logga_lo_stesso(self):
        # .1 occupato da una cartella non vuota -> os.replace non puo' riuscire
        d = cartella(self.home)
        (d / "backend.log.1" / "occupata").mkdir(parents=True)
        (d / "backend.log").write_text("y" * 1_100_000, encoding="utf-8")
        r = avvia(self.testa, self.home, "ROTAZIONE-FALLITA")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ROTAZIONE-FALLITA", log(self.home) or "")

    def test_non_scrive_nella_home(self):
        """Con la cartella dati altrove, nella home non compare nessun ~/rizzo-pii."""
        with tempfile.TemporaryDirectory() as dati:
            env_home = self.home
            script = Path(env_home) / "_avvio.py"
            script.write_text(self.testa + "\nprint('X')\n", encoding="utf-8")
            env = dict(os.environ, XDG_DATA_HOME=dati, HOME=env_home,
                       PYTHONPATH=str(APP_DIR), PYTHONIOENCODING="utf-8")
            if sys.platform == "win32":
                env["LOCALAPPDATA"] = dati
            else:                              # come su un Mac o un Linux veri
                env.pop("LOCALAPPDATA", None)
            r = subprocess.run([sys.executable, str(script)], env=env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            if sys.platform != "darwin":       # su macOS la cartella dati e' sotto HOME
                self.assertFalse((Path(env_home) / "rizzo-pii").exists())
                self.assertTrue((Path(dati) / "rizzo-pii" / "backend.log").exists())

    def test_tauri_logga_nella_stessa_cartella(self):
        """lib.rs: tauri.log va in config_dir(), non in dirs::home_dir()."""
        src = LIB_RS.read_text(encoding="utf-8")
        tlog = src[src.index("fn tlog("):src.index("fn chrono_timestamp(")]
        self.assertIn("config_dir()", tlog)
        self.assertNotIn("home_dir", tlog)
        # lo splash non deve piu' indicare un percorso Windows scritto a mano
        self.assertNotIn("%LOCALAPPDATA%", src[src.index("timeout raggiunto"):])

    def test_primo_avvio_crea_la_cartella(self):
        r = avvia(self.testa, self.home, "PRIMO-AVVIO")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PRIMO-AVVIO", log(self.home) or "")


if __name__ == "__main__":
    unittest.main()
