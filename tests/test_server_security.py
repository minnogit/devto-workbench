"""Test della difesa contro richieste che non vengono dalla pagina (web/server.py).
Avvia il vero gestore su una porta libera, senza rete esterna:

    python3 -m unittest discover tests
"""
import http.client
import importlib.util
import json
import pathlib
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("server_security_under_test", ROOT / "web" / "server.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)


class SecurityTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._log = srv.Handler.log_message
        srv.Handler.log_message = lambda *a, **k: None
        self.addCleanup(lambda: setattr(srv.Handler, "log_message", self._log))
        self._saved = srv.toggle_reviewed
        srv.toggle_reviewed = lambda i: (self.calls.append(i), {"reviewed": True})[1]
        self.addCleanup(lambda: setattr(srv, "toggle_reviewed", self._saved))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        conn.close()
        return resp.status, body

    def own(self, **extra):
        h = {"Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}", srv.WRITE_HEADER: "1"}
        h.update(extra)
        return {k: v for k, v in h.items() if v is not None}

    # ---- POST
    def test_post_della_pagina_passa(self):
        status, body = self.request("POST", "/api/toggle-reviewed/5", self.own())
        self.assertEqual(status, 200)
        self.assertEqual(self.calls, [5])

    def test_post_senza_origin_ma_con_intestazione_passa(self):
        # curl e gli script non mandano Origin
        status, _ = self.request("POST", "/api/toggle-reviewed/5", self.own(Origin=None))
        self.assertEqual(status, 200)

    def test_post_da_un_altro_sito_non_viene_eseguita(self):
        for origin in ("http://evil.example", "http://127.0.0.1:9999", f"http://localhost:{self.port + 1}", "null"):
            status, body = self.request("POST", "/api/toggle-reviewed/5", self.own(Origin=origin))
            self.assertEqual(status, 403, origin)
            self.assertIn("Origin", json.loads(body)["error"])
        self.assertEqual(self.calls, [])

    def test_post_senza_intestazione_propria_non_viene_eseguita(self):
        # una richiesta «semplice» di un altro sito non può aggiungerla
        status, body = self.request("POST", "/api/toggle-reviewed/5", self.own(**{srv.WRITE_HEADER: None}))
        self.assertEqual(status, 403)
        self.assertIn(srv.WRITE_HEADER, json.loads(body)["error"])
        self.assertEqual(self.calls, [])

    def test_post_con_host_estraneo_non_viene_eseguita(self):
        # DNS rebinding: il browser pensa di parlare con evil.example, che risolve a 127.0.0.1
        status, _ = self.request("POST", "/api/toggle-reviewed/5", self.own(Host="evil.example"))
        self.assertEqual(status, 403)
        self.assertEqual(self.calls, [])

    def test_i_controlli_valgono_anche_per_rotte_inesistenti(self):
        status, _ = self.request("POST", "/api/non-esiste/5", self.own(Origin="http://evil.example"))
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/api/non-esiste/5", self.own())
        self.assertEqual(status, 404)

    def test_il_corpo_json_con_tipo_semplice_non_aggira_i_controlli(self):
        # prima il server leggeva il corpo come JSON qualunque fosse il Content-Type
        h = self.own(Origin="http://evil.example", **{"Content-Type": "text/plain", "Content-Length": "0"})
        status, _ = self.request("POST", "/api/stato/5", h)
        self.assertEqual(status, 403)

    # ---- GET
    def test_get_della_pagina_passa(self):
        status, body = self.request("GET", "/", {"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        self.assertIn("Articoli dev.to", body)

    def test_get_con_host_estraneo_viene_rifiutata(self):
        for path in ("/", "/api/me", "/api/articles"):
            status, _ = self.request("GET", path, {"Host": "evil.example"})
            self.assertEqual(status, 403, path)

    def test_host_ignora_le_maiuscole(self):
        status, _ = self.request("GET", "/", {"Host": f"LOCALHOST:{self.port}"})
        self.assertEqual(status, 200)

    def test_la_pagina_manda_l_intestazione_su_ogni_post(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        posts = html.count('method: "POST"')
        self.assertGreater(posts, 0)
        self.assertGreaterEqual(html.count("WB_HEADERS"), posts + 1)  # definizione + un uso per POST


if __name__ == "__main__":
    unittest.main()
