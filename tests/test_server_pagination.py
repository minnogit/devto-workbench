"""Test della lettura a pagine degli elenchi di Dev.to (web/server.py).
Senza rete: urlopen è sostituito da una finta API che pagina davvero.

    python3 -m unittest discover tests
"""
import importlib.util
import io
import json
import pathlib
import unittest
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("server_pagination_under_test", ROOT / "web" / "server.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)


def arts(start, n):
    return [{"id": i, "title": f"A{i}"} for i in range(start, start + n)]


class FakeApi:
    """Risponde come /articles/me...: `page` da 1, `per_page` (default 30)."""

    def __init__(self, data_by_path):
        self.data = data_by_path
        self.calls = []

    def __call__(self, req, timeout=None):
        url = urllib.parse.urlparse(req.full_url)
        q = urllib.parse.parse_qs(url.query)
        per_page = int(q.get("per_page", ["30"])[0])
        page = int(q.get("page", ["1"])[0])
        path = url.path.replace("/api", "", 1)
        self.calls.append((path, per_page, page))
        chunk = self.data[path][(page - 1) * per_page: page * per_page]

        class Resp(io.BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                self_inner.close()
        return Resp(json.dumps(chunk).encode("utf-8"))


class FetchPaginatedTest(unittest.TestCase):
    def pages(self, items):
        calls = []

        def get_page(page, per_page):
            calls.append((page, per_page))
            return items[(page - 1) * per_page: page * per_page]
        return get_page, calls

    def test_piu_pagine(self):
        get_page, calls = self.pages(arts(1, 250))
        self.assertEqual(len(srv.fetch_paginated(get_page)), 250)
        self.assertEqual(calls, [(1, 100), (2, 100), (3, 100)])

    def test_multiplo_esatto_chiede_una_pagina_vuota(self):
        get_page, calls = self.pages(arts(1, 200))
        self.assertEqual(len(srv.fetch_paginated(get_page)), 200)
        self.assertEqual(len(calls), 3)

    def test_pochi_articoli_una_sola_richiesta(self):
        get_page, calls = self.pages(arts(1, 5))
        self.assertEqual(len(srv.fetch_paginated(get_page)), 5)
        self.assertEqual(calls, [(1, 100)])

    def test_elenco_vuoto(self):
        get_page, calls = self.pages([])
        self.assertEqual(srv.fetch_paginated(get_page), [])
        self.assertEqual(len(calls), 1)

    def test_voci_ripetute_tra_pagine_si_tolgono(self):
        data = [arts(1, 100), arts(100, 3)]  # l'id 100 compare in entrambe

        def get_page(page, per_page):
            return data[page - 1] if page <= len(data) else []
        out = srv.fetch_paginated(get_page)
        self.assertEqual(sorted(a["id"] for a in out), list(range(1, 103)))

    def test_tetto_di_pagine_non_tronca_in_silenzio(self):
        get_page, _ = self.pages(arts(1, 400))
        with self.assertRaises(RuntimeError) as cm:
            srv.fetch_paginated(get_page, per_page=100, max_pages=3)
        self.assertIn("più di 300", str(cm.exception))

    def test_un_errore_a_meta_non_restituisce_un_elenco_parziale(self):
        def get_page(page, per_page):
            if page == 2:
                raise OSError("rete caduta")
            return arts(1, per_page)
        with self.assertRaises(OSError):
            srv.fetch_paginated(get_page)


class FetchRemoteArticlesTest(unittest.TestCase):
    def setUp(self):
        self._saved = (srv.API_KEY, srv.urllib.request.urlopen)
        self.addCleanup(self._restore)
        srv.API_KEY = "chiave-finta"

    def _restore(self):
        srv.API_KEY, srv.urllib.request.urlopen = self._saved

    def test_oltre_i_30_di_default_non_si_perde_niente(self):
        api = FakeApi({"/articles/me": arts(1, 17), "/articles/me/unpublished": arts(1000, 130)})
        srv.urllib.request.urlopen = api
        out = srv.fetch_remote_articles()
        self.assertEqual(len(out), 17 + 130)
        self.assertEqual({p for p, _, _ in api.calls}, {"/articles/me", "/articles/me/unpublished"})
        self.assertTrue(all(pp == 100 for _, pp, _ in api.calls))
        self.assertEqual([c for c in api.calls if c[0].endswith("unpublished")], [
            ("/articles/me/unpublished", 100, 1), ("/articles/me/unpublished", 100, 2)])

    def test_senza_chiave_errore_chiaro(self):
        srv.API_KEY = None
        with self.assertRaises(RuntimeError):
            srv.fetch_remote_articles()


if __name__ == "__main__":
    unittest.main()
