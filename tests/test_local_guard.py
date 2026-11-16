# -*- coding: utf-8 -*-
"""Protezione del server locale (issue upstream #112).

- DNS rebinding: Host diverso da loopback/host di bind -> 403 su tutto, pagina inclusa;
- CSRF: Origin di un altro sito -> 403, anche con il token;
- token di sessione: obbligatorio per l'API (header X-PII-Token, o ?t= sulle GET
  di /doc/...), non per pagina, risorse statiche, /health e GET /config (Tauri);
- POST /config verso un host non di loopback solo con confirm_expose;
- /port-check solo su loopback/0.0.0.0/host di bind.

Tutti i valori sono SINTETICI.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "app"))
sys.path.insert(0, str(ROOT / "tests"))

import local_guard  # noqa: E402
from test_ora import APP, client  # noqa: E402

LOCAL = {"Host": "127.0.0.1:5005"}


class GuardPuro(unittest.TestCase):
    """local_guard da solo, senza Flask."""

    def test_host(self):
        for h in ("127.0.0.1:5005", "localhost:5005", "[::1]:5005", "127.0.0.2", "LOCALHOST"):
            self.assertTrue(local_guard.host_ok(h), h)
        for h in ("evil.example:5005", "", "192.168.1.10:5005", "127.0.0.1.evil.example"):
            self.assertFalse(local_guard.host_ok(h), h)
        self.assertTrue(local_guard.host_ok("192.168.1.10:5005", "192.168.1.10"))
        self.assertTrue(local_guard.host_ok("0.0.0.0:5005", "0.0.0.0"))
        self.assertFalse(local_guard.host_ok("evil.example", "0.0.0.0"))

    def test_host_extra_da_env(self):
        with patch.dict(os.environ, {"PII_ALLOWED_HOSTS": "pc-studio.lan, 10.0.0.5"}):
            self.assertTrue(local_guard.host_ok("pc-studio.lan:5005", "0.0.0.0"))
            self.assertTrue(local_guard.host_ok("10.0.0.5", "0.0.0.0"))

    def test_origin(self):
        self.assertTrue(local_guard.origin_ok(None))
        self.assertTrue(local_guard.origin_ok("http://127.0.0.1:5005"))
        self.assertTrue(local_guard.origin_ok("http://localhost:5005"))
        for o in ("null", "https://evil.example", "file://", "http://127.0.0.1.evil.example"):
            self.assertFalse(local_guard.origin_ok(o), o)

    def test_loopback(self):
        self.assertTrue(local_guard.is_loopback("127.0.0.1"))
        self.assertTrue(local_guard.is_loopback("::1"))
        self.assertFalse(local_guard.is_loopback("0.0.0.0"))
        self.assertFalse(local_guard.is_loopback("192.168.1.2"))


@unittest.skipIf(APP is None, "app.py non importabile (flask/fitz assenti)")
class GuardHttp(unittest.TestCase):
    def setUp(self):
        self.anon = APP.app.test_client()     # senza token
        self.ok = client()                    # con token

    def _nlp(self):
        return patch.object(APP, "nlp", lambda chunks: [[] for _ in chunks])

    def test_rebinding_rifiutato_ovunque(self):
        evil = {"Host": "evil.example:5005"}
        for path in ("/", "/health", "/static/app.js", "/config"):
            r = self.ok.get(path, headers=evil)
            self.assertEqual(r.status_code, 403, path)
            r.close()
        r = self.ok.post("/analyze", json={"text": "Mario Rossi"}, headers=evil)
        self.assertEqual(r.status_code, 403)

    def test_origin_estranea_rifiutata_anche_col_token(self):
        h = dict(LOCAL, Origin="https://evil.example")
        r = self.ok.post("/settings", json={"mapping_enabled": False}, headers=h)
        self.assertEqual(r.status_code, 403)

    def test_token_obbligatorio_per_api(self):
        for method, path, kw in (("post", "/analyze", {"json": {"text": "x"}}),
                                 ("get", "/settings", {}),
                                 ("post", "/config", {"json": {"host": "127.0.0.1", "port": 5006}}),
                                 ("get", "/port-check", {}),
                                 ("get", "/doc/abc/file.pdf", {})):
            r = getattr(self.anon, method)(path, headers=LOCAL, **kw)
            self.assertEqual(r.status_code, 403, path)
        r = self.anon.get("/settings", headers=dict(LOCAL, **{"X-PII-Token": "sbagliato"}))
        self.assertEqual(r.status_code, 403)

    def test_percorsi_aperti_senza_token(self):
        for path in ("/", "/health", "/config", "/static/app.css"):
            r = self.anon.get(path, headers=LOCAL)
            self.assertNotEqual(r.status_code, 403, path)
            r.close()

    def test_pagina_porta_il_token(self):
        html = self.anon.get("/", headers=LOCAL).get_data(as_text=True)
        self.assertIn(local_guard.API_TOKEN, html)
        self.assertNotIn("__TOKEN__", html)

    def test_analyze_col_token(self):
        with self._nlp():
            r = self.ok.post("/analyze", json={"text": "IBAN IT60X0542811101000000123456"},
                             headers=LOCAL)
        self.assertEqual(r.status_code, 200)
        self.assertIn("[IBAN_1]", r.get_json()["anonymized_text"])

    def test_doc_col_token_in_query(self):
        doc = APP.fitz.open()
        doc.new_page()
        doc_id, _ = APP._store_doc(doc.tobytes(), "x.pdf")
        doc.close()
        r = self.anon.get(f"/doc/{doc_id}/file.pdf", headers=LOCAL)
        self.assertEqual(r.status_code, 403)
        r = self.anon.get(f"/doc/{doc_id}/file.pdf?t={local_guard.API_TOKEN}", headers=LOCAL)
        self.assertEqual(r.status_code, 200)
        # il token in query vale SOLO per /doc
        r = self.anon.get(f"/settings?t={local_guard.API_TOKEN}", headers=LOCAL)
        self.assertEqual(r.status_code, 403)

    def test_config_esporre_richiede_conferma(self):
        with patch.object(APP.server_config, "save_config") as save:
            r = self.ok.post("/config", json={"host": "0.0.0.0", "port": 5005}, headers=LOCAL)
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.get_json()["code"], "confirm_expose")
            save.assert_not_called()
            r = self.ok.post("/config", json={"host": "0.0.0.0", "port": 5005,
                                              "confirm_expose": True}, headers=LOCAL)
            self.assertEqual(r.status_code, 200)
            save.assert_called_once_with("0.0.0.0", 5005)
            r = self.ok.post("/config", json={"host": "127.0.0.1", "port": 5006}, headers=LOCAL)
            self.assertEqual(r.status_code, 200)
            r = self.ok.post("/config", json={"host": "http://x y", "port": 5006}, headers=LOCAL)
            self.assertEqual(r.status_code, 400)

    def test_port_check_solo_indirizzi_locali(self):
        r = self.ok.get("/port-check?host=192.168.1.1&port=22", headers=LOCAL)
        self.assertEqual(r.status_code, 400)
        r = self.ok.get("/port-check?host=127.0.0.1&port=59999", headers=LOCAL)
        self.assertEqual(r.status_code, 200)
        self.assertIn("available", r.get_json())

    def test_header_di_sicurezza(self):
        r = self.anon.get("/", headers=LOCAL)
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_token_disattivabile(self):
        with patch.dict(os.environ, {"PII_REQUIRE_TOKEN": "0"}):
            r = self.anon.get("/settings", headers=LOCAL)
            self.assertEqual(r.status_code, 200)
            # Host e Origin restano controllati
            r = self.anon.get("/settings", headers={"Host": "evil.example"})
            self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
