#!/usr/bin/env python3
"""One-off / re-runnable import: pull all of the author's Dev.to articles
(published + draft) into articles/, and mirror their embedded images into
assets/<id>/ as an offline backup.

The article's markdown body is stored VERBATIM (same bytes Dev.to would
return), image URLs included. This is deliberate: devto_synced_hash is a
sha256 of that verbatim body, and the sync skill relies on being able to
recompute the same hash from what Dev.to's API returns. Rewriting image
links to local paths would break that comparison, so assets/ is a backup
copy only -- the tracked markdown keeps pointing at the original Dev.to CDN
URLs.
"""
import hashlib
import json
import os
import pathlib
import re
import sys
import unicodedata

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "web"))
import server  # noqa: E402  (solo per la lettura a pagine degli elenchi, condivisa con la pagina web)

BASE_URL = "https://dev.to/api"
# Stessa convenzione di web/server.py: di default `articles/` e `assets/` nella
# cartella da cui si lancia lo script, sovrascrivibili con ARTICLES_DIR e
# ASSETS_DIR.
ARTICLES_DIR = pathlib.Path(os.environ.get("ARTICLES_DIR", "articles")).resolve()
# Le immagini seguono gli articoli: di default `assets/` accanto a ARTICLES_DIR.
ASSETS_DIR = pathlib.Path(os.environ.get("ASSETS_DIR") or ARTICLES_DIR.parent / "assets").resolve()

API_KEY = os.environ.get("DEV_TO_API_KEY")
if not API_KEY:
    sys.exit("DEV_TO_API_KEY is not set in the environment")

HEADERS = {"api-key": API_KEY}

IMG_URL_RE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")


def slugify(title: str) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:80] or "untitled"


def fetch_all_mine() -> list[dict]:
    articles = []
    with httpx.Client(headers=HEADERS, timeout=30.0) as client:
        for path in ("/articles/me", "/articles/me/unpublished"):
            def get_page(page: int, per_page: int, path: str = path) -> list:
                resp = client.get(f"{BASE_URL}{path}", params={"per_page": per_page, "page": page})
                resp.raise_for_status()
                return resp.json()
            articles.extend(server.fetch_paginated(get_page))
    return articles


def download_images(body_markdown: str, article_id: int, client: httpx.Client,
                    cover_image: str | None = None) -> None:
    """Copia in assets/<id>/ le immagini del testo e la copertina."""
    urls = IMG_URL_RE.findall(body_markdown)
    cover = server.cover_source_url(cover_image)
    if cover:
        urls.append(cover)
    if not urls:
        return
    out_dir = ASSETS_DIR / str(article_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        name = url.split("/")[-1].split("?")[0] or "image"
        dest = out_dir / name
        if dest.exists():
            continue
        try:
            resp = client.get(url, timeout=30.0)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            print(f"  saved asset {dest}")
        except httpx.HTTPError as e:
            print(f"  WARNING: could not download {url}: {e}")


def write_article_file(article: dict) -> pathlib.Path:
    article_id = article["id"]
    slug = slugify(article["title"])
    filename = f"{article_id}-{slug}.md"
    path = ARTICLES_DIR / filename

    body = article["body_markdown"]
    body_hash = hashlib.sha256(body.encode()).hexdigest()

    front_matter = {
        "id": article_id,
        "title": article["title"],
        "published": article["published"],
        "tags": article.get("tag_list", []),
        "url": article["url"],
        "canonical_url": article.get("canonical_url"),
        "cover_image": article.get("cover_image"),
        "devto_synced_hash": body_hash,
    }

    lines = ["---"]
    for key, value in front_matter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(body)

    path.write_text("\n".join(lines))
    return path


def main() -> None:
    articles = fetch_all_mine()
    print(f"Found {len(articles)} articles")

    with httpx.Client(timeout=30.0) as client:
        for article in articles:
            path = write_article_file(article)
            state = "published" if article["published"] else "draft"
            print(f"[{state}] {article['id']} -> {path}")
            download_images(article["body_markdown"], article["id"], client, article.get("cover_image"))


if __name__ == "__main__":
    main()
