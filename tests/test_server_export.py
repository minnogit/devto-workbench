"""Test dell'esportazione dalla pagina (GET e POST /api/export in web/server.py).
Avvia il vero gestore su una porta libera, senza rete esterna:

    python3 -m unittest discover tests
"""
import http.client
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("server_export_under_test", ROOT / "web" / "server.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

BODY = "Primo paragrafo.\n\nUltima riga senza a capo finale"


def article(id_, published, body=BODY):
    fm = {"id": id_, "title": f"Titolo {id_}", "published": published, "tags": ["git"],
          "url": f"https://dev.to/u/a-{id_}", "canonical_url": f"https://dev.to/u/a-{id_}",
          "cover_image": None, "devto_synced_hash": "aa" * 32, "stato": "pronta", "reviewed": True}
    return srv.render_article(fm, body)


class ExportEndpointTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = pathlib.Path(self._tmp.name)
        self.articles = root / "articles"
        self.articles.mkdir()
        self.repo = root / "brain"
        self.dest = self.repo / "raw" / "devto"
        self.altro = root / "altrove"
        (self.articles / "1-pubblicato.md").write_text(article(1, True), encoding="utf-8")
        (self.articles / "2-bozza.md").write_text(article(2, False), encoding="utf-8")

        self._saved = (srv.ARTICLES_DIR, srv.EXPORT_DEST, srv.build_report, srv.Handler.log_message)
        self.addCleanup(self._restore)
        srv.ARTICLES_DIR = self.articles
        srv.EXPORT_DEST = self.dest
        srv.build_report = lambda: self.rows  # niente rete
        srv.Handler.log_message = lambda *a, **k: None
        self.rows = []

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.httpd.shutdown(), self.httpd.server_close()))

    def _restore(self):
        srv.ARTICLES_DIR, srv.EXPORT_DEST, srv.build_report, srv.Handler.log_message = self._saved

    def call(self, method, path, body=None, headers=None):
        h = {"Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}", srv.WRITE_HEADER: "1"}
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = None if body is None else json.dumps(body)
        if payload is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=h)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8") or "{}")
        conn.close()
        return resp.status, data

    # ---- configurazione
    def test_spenta_senza_destinazione(self):
        srv.EXPORT_DEST = None
        self.assertEqual(self.call("GET", "/api/export"), (200, {"enabled": False}))
        status, data = self.call("POST", "/api/export", {})
        self.assertEqual(status, 409)
        self.assertIn("EXPORT_DEST", data["error"])

    # ---- anteprima
    def test_anteprima_non_scrive(self):
        status, data = self.call("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertTrue(data["enabled"])
        self.assertEqual(data["dest"], str(self.dest))
        self.assertEqual(data["risultato"]["added"], ["1-pubblicato.md"])
        self.assertEqual(data["risultato"]["skipped_drafts"], 1)
        self.assertFalse(self.dest.exists())

    def test_anteprima_segnala_i_pubblicati_che_nel_file_sono_bozze(self):
        # su Dev.to è pubblicato, nel file no: lo script non lo esporterebbe
        self.rows = [{"id": 2, "title": "Titolo 2", "published": True, "local_file": "2-bozza.md"},
                     {"id": 1, "title": "Titolo 1", "published": True, "local_file": "1-pubblicato.md"}]
        _, data = self.call("GET", "/api/export")
        self.assertEqual(data["pendenti"], [{"id": 2, "title": "Titolo 2"}])

    def test_anteprima_senza_rete_non_si_rompe(self):
        def fallisce():
            raise OSError("rete assente")
        srv.build_report = fallisce
        status, data = self.call("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIsNone(data["pendenti"])
        self.assertEqual(data["risultato"]["added"], ["1-pubblicato.md"])

    # ---- esportazione
    def test_esporta_scrive_solo_i_pubblicati_con_il_testo_identico(self):
        status, data = self.call("POST", "/api/export", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["added"], ["1-pubblicato.md"])
        self.assertEqual([p.name for p in self.dest.glob("*.md")], ["1-pubblicato.md"])
        fm, body = srv.split_front_matter((self.dest / "1-pubblicato.md").read_text(encoding="utf-8"))
        self.assertEqual(body, BODY)
        self.assertEqual(sorted(fm), ["id", "tags", "title", "url"])

    def test_la_richiesta_non_puo_scegliere_la_destinazione(self):
        for chiave in ("dest", "destinazione", "path", "EXPORT_DEST"):
            status, _ = self.call("POST", "/api/export", {chiave: str(self.altro)})
            self.assertEqual(status, 200)
        self.assertFalse(self.altro.exists())
        self.assertTrue((self.dest / "1-pubblicato.md").exists())

    def test_comando_di_commit_solo_se_la_destinazione_e_in_un_repo(self):
        status, data = self.call("POST", "/api/export", {})
        self.assertIsNone(data["comando_commit"])  # la cartella non è ancora in un repo git
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.articles / "3-nuovo.md").write_text(article(3, True), encoding="utf-8")
        status, data = self.call("POST", "/api/export", {})
        self.assertIn("git -C", data["comando_commit"])
        self.assertIn('add "raw/devto"', data["comando_commit"])

    def test_senza_novita_non_propone_nessun_commit(self):
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.call("POST", "/api/export", {})
        _, data = self.call("POST", "/api/export", {})
        self.assertEqual((data["added"], data["updated"]), ([], []))
        self.assertIsNone(data["comando_commit"])

    # ---- orfani e prune
    def test_prune_solo_se_richiesto_e_booleano(self):
        self.call("POST", "/api/export", {})
        (self.articles / "1-pubblicato.md").write_text(article(1, False), encoding="utf-8")  # tornato bozza
        _, data = self.call("POST", "/api/export", {})
        self.assertEqual((data["orphans"], data["removed"]), (["1-pubblicato.md"], []))
        self.assertTrue((self.dest / "1-pubblicato.md").exists())
        for valore in ("yes", 1, "true"):
            status, d = self.call("POST", "/api/export", {"prune": valore})
            self.assertEqual(status, 409, valore)
            self.assertIn("prune", d["error"])
        self.assertTrue((self.dest / "1-pubblicato.md").exists())
        _, data = self.call("POST", "/api/export", {"prune": True})
        self.assertEqual(data["removed"], ["1-pubblicato.md"])
        self.assertFalse((self.dest / "1-pubblicato.md").exists())

    # ---- sicurezza e limiti
    def test_servono_origin_e_intestazione_della_pagina(self):
        status, _ = self.call("POST", "/api/export", {}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.call("POST", "/api/export", {}, {srv.WRITE_HEADER: "0"})
        self.assertEqual(status, 403)
        self.assertFalse(self.dest.exists())

    def test_destinazione_dentro_gli_articoli_viene_rifiutata(self):
        srv.EXPORT_DEST = self.articles / "export"
        status, data = self.call("POST", "/api/export", {})
        self.assertEqual(status, 409)
        self.assertFalse((self.articles / "export").exists())
        status, _ = self.call("GET", "/api/export")
        self.assertEqual(status, 409)

    def test_lo_script_e_la_pagina_condividono_la_stessa_logica(self):
        script = importlib.util.spec_from_file_location("export_script", ROOT / "scripts" / "export_published.py")
        mod = importlib.util.module_from_spec(script)
        script.loader.exec_module(mod)
        self.assertEqual(mod.export_published.__name__, "export_published")
        self.assertEqual(mod.export_published.__module__, mod.server.export_published.__module__)

    def test_la_pagina_ha_il_pulsante_e_manda_l_intestazione(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="exportBtn"', html)
        self.assertIn('fetch("/api/export", {', html)
        self.assertIn("...WB_HEADERS", html)


if __name__ == "__main__":
    unittest.main()
