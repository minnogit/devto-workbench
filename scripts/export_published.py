#!/usr/bin/env python3
"""Esporta gli articoli pubblicati in una cartella a parte.

Pensato per alimentare una base di conoscenza (una wiki, un "second brain")
con i soli articoli già usciti, senza le bozze né la storia git delle bozze:
la cartella di destinazione sta in un altro repo e ci si committa il risultato.

Per ogni articolo con `published: true` scrive `<dest>/<nome file originale>`
con il body identico byte per byte e un front-matter ridotto ai campi stabili
(`id`, `title`, `url`, `tags`). Restano fuori `devto_synced_hash`, `stato`,
`reviewed` e `cover_image`: cambiano senza che il testo cambi, e chi controlla i
file con un checksum (come wiki-mcp-api, sull'intero file) vedrebbe sorgenti
"modificate" che non lo sono.

Non legge mai Dev.to e non scrive mai nella cartella degli articoli. Si fida di
`published` nel front-matter locale: conviene lanciarlo dopo un sync, perché un
articolo tornato bozza sul portale risulterebbe ancora esportato.

Uso:
    python3 scripts/export_published.py --dest /percorso/second-brain/raw/devto
    python3 scripts/export_published.py --dest ... --dry-run
    python3 scripts/export_published.py --dest ... --prune
"""
import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "web"))
import server  # noqa: E402  (solo per riusare parser e formato dei file articolo)

# La logica sta in web/server.py, condivisa con il pulsante della pagina.
EXPORT_FIELDS = server.EXPORT_FIELDS
overlaps = server.overlaps
export_published = server.export_published


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--articles-dir", default=os.environ.get("ARTICLES_DIR", "articles"),
                        help="cartella degli articoli (default: ARTICLES_DIR o ./articles)")
    parser.add_argument("--dest", required=True, help="cartella di destinazione, in un altro repo")
    parser.add_argument("--prune", action="store_true",
                        help="cancella dalla destinazione gli articoli esportati che non sono più pubblicati")
    parser.add_argument("--dry-run", action="store_true", help="mostra cosa farebbe senza scrivere")
    args = parser.parse_args()

    try:
        r = export_published(pathlib.Path(args.articles_dir), pathlib.Path(args.dest),
                             prune=args.prune, dry_run=args.dry_run)
    except ValueError as e:
        print(f"Errore: {e}", file=sys.stderr)
        return 1

    prefix = "[prova, nessun file toccato] " if args.dry_run else ""
    print(f"{prefix}aggiunti {len(r['added'])}, aggiornati {len(r['updated'])}, "
          f"invariati {len(r['unchanged'])}; bozze escluse {r['skipped_drafts']}")
    for name in r["added"]:
        print(f"  + {name}")
    for name in r["updated"]:
        print(f"  ~ {name}")
    if r["orphans"]:
        verb = "rimossi" if args.prune else "non più pubblicati (usa --prune per rimuoverli)"
        print(f"{len(r['orphans'])} file {verb}:")
        for name in r["orphans"]:
            print(f"  - {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
