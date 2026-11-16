# -*- coding: utf-8 -*-
"""Il dizionario {segnaposto -> valore} non resta in chiaro nel localStorage.

Vive in sessionStorage (muore con la finestra). Un dizionario lasciato dalle versioni
precedenti nel localStorage viene portato una volta nella sessione e cancellato dal
disco, marcato come "estraneo" (MAP_FOREIGN): alla prossima analisi si sostituisce
invece di fondersi, perche' la sua numerazione non e' quella della sessione.

La parte comportamentale gira con node (presente sui runner GitHub e richiesto per
il frontend Tauri); senza node resta il controllo statico.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "src" / "app" / "static" / "app.js"
SRC = JS.read_text(encoding="utf-8")

HARNESS = r"""
function mkStore(init){const d={...init};return{getItem:k=>k in d?d[k]:null,
  setItem:(k,v)=>{d[k]=String(v)},removeItem:k=>{delete d[k]},_d:d};}
const localStorage=mkStore(INIT_LOCAL), sessionStorage=mkStore({});
const els={};const $=id=>(els[id]=els[id]||{textContent:''});
const T={it:{dict_loaded_n:n=>'caricato '+n,dict_session:n=>'sessione '+n}};const L='it';
let MAP={};
"""


class DizionarioStorage(unittest.TestCase):
    def test_nessuna_scrittura_del_dizionario_nel_localstorage(self):
        scritture = re.findall(r"localStorage\.setItem\(([^,]+),", SRC)
        self.assertEqual([s.strip() for s in scritture], ["'pii_lang'"])
        self.assertIn("sessionStorage.setItem(MAP_KEY", SRC)

    @unittest.skipIf(shutil.which("node") is None, "node non installato")
    def test_migrazione_dal_localstorage(self):
        start = SRC.index("const MAP_KEY=")
        end = SRC.index("let MAP_FOREIGN=false;") + len("let MAP_FOREIGN=false;")
        decl = SRC[start:end]
        mstart = SRC.index("try{const m=sessionStorage.getItem(MAP_KEY)")
        boot = SRC[mstart:]          # sono le ultime righe di app.js (avvio della pagina)
        vecchio = {"[FULLNAME_1]": "Mario Rossi"}
        prog = (HARNESS.replace("INIT_LOCAL", json.dumps({"pii_map": json.dumps(vecchio),
                                                           "pii_lang": "it"}))
                + decl + "\n" + boot +
                "\nconsole.log(JSON.stringify({local:localStorage._d,session:sessionStorage._d,"
                "MAP,MAP_FOREIGN}));")
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        r = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertNotIn("pii_map", r["local"])            # cancellato dal disco
        self.assertEqual(r["local"].get("pii_lang"), "it")  # il resto non si tocca
        self.assertEqual(json.loads(r["session"]["pii_map"]), vecchio)
        self.assertEqual(r["MAP"], vecchio)
        self.assertTrue(r["MAP_FOREIGN"])


if __name__ == "__main__":
    unittest.main()
