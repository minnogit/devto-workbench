# devto-workbench

Strumenti per tenere i propri articoli Dev.to come file Markdown in un repository Git e lavorarci senza perdere le modifiche fatte direttamente dal portale.

Dev.to non offre una storia delle modifiche, e chi scrive può modificare un articolo sia dal portale sia dal repo. Qui ogni file ricorda l'hash dell'ultima versione sincronizzata, così prima di sovrascrivere in un senso o nell'altro si vede se l'altro lato è cambiato nel frattempo.

Il repo contiene tre cose:

- `web/`: un'interfaccia web locale (solo standard library Python) con vista elenco, card e kanban per stato editoriale, stato di sync di ogni articolo, confronto riga per riga e pull/push per i casi non ambigui;
- `.claude/skills/devto-articles/`: una skill per Claude Code che gestisce import, pull, push, pubblicazione e conflitti, appoggiandosi a un server MCP per Dev.to;
- `scripts/import_articles.py`: l'importazione bulk iniziale di tutti gli articoli.

Gli articoli non stanno qui. Servono un repo a parte, meglio privato perché le bozze lo sono, con una cartella `articles/`.

## Requisiti

- Python 3.10 o successivo (nel codice c'è la sintassi `X | None`). L'ho provato con Python 3.14 su Linux, non su Windows.
- Una API key Dev.to ([Settings → Extensions](https://dev.to/settings/extensions)), esportata come `DEV_TO_API_KEY`. Non va mai committata.
- Per lo script di import: `pip install httpx`. Il server web non ha dipendenze.
- Per la skill: un server MCP per Dev.to configurato nel repo dei contenuti (vedi `.mcp.json.example`), in una versione che supporti la lettura delle proprie bozze e il campo `ai_disclosure_level`. La skill indica quali tool usa.

## Come sono organizzati i contenuti

```
il-tuo-repo/
  articles/<id>-<slug>.md     un file per articolo
  assets/<id>/                copia offline delle immagini (facoltativa)
```

Il nome del file è comodo ma non conta: l'identificatore è l'`id` nel front-matter. Il front-matter ha una riga per campo, nella forma `chiave: <valore JSON>` (non è YAML vero, anche se ci assomiglia):

```
---
id: 1234567
title: "Titolo"
published: false
tags: ["git", "linux"]
url: "https://dev.to/..."
canonical_url: "https://dev.to/..."
cover_image: null
devto_synced_hash: "sha256 del body come era su Dev.to all'ultimo sync"
stato: "revisionata"
reviewed: true
---

Il testo dell'articolo...
```

Il testo dopo il front-matter è il `body_markdown` esatto di Dev.to, byte per byte. Non vanno normalizzati spazi o markdown: cambierebbe l'hash e produrrebbe falsi conflitti. Il documento [`CLAUDE.md`](./CLAUDE.md) spiega schema e meccanismo a hash in dettaglio.

`stato` e `reviewed` sono campi solo locali, mai inviati a Dev.to. `stato` vale `bozza` (se manca), `revisionata` o `pronta`. «Pubblicato» non è un valore: è lo stato di un articolo con `published: true`.

## Interfaccia web

Dalla radice del repo dei contenuti:

```bash
python3 /percorso/di/devto-workbench/web/server.py
```

poi apri http://localhost:8787. Il server ascolta solo su `127.0.0.1`.

| Variabile | Significato |
|---|---|
| `DEV_TO_API_KEY` | Obbligatoria per parlare con Dev.to. Senza, il server parte ma `/api/articles` risponde con un errore. |
| `ARTICLES_DIR`, `ASSETS_DIR` | Dove cercare i file. Di default `articles/` e `assets/` nella cartella da cui lanci il server. |
| `PORT` | Porta del server, di default 8787. |
| `DEVTO_USERNAME` | Nome mostrato nel titolo. Se manca lo chiede a Dev.to. |

Il server non ricarica il codice da solo: dopo una modifica a `web/server.py` o `web/index.html` va riavviato.

### Stati di sync

Per ogni articolo confronta tre cose: l'hash del body locale, quello del body remoto e `devto_synced_hash`.

| Stato | Significato | Azione dalla pagina |
|---|---|---|
| allineato | Nessuno ha cambiato nulla | nessuna |
| remoto più recente | È cambiato solo il portale | scarica l'aggiornamento (pull) |
| locale più recente | È cambiato solo il file | manda online (push) |
| conflitto | Sono cambiati entrambi, in modo diverso | nessuna: il merge non è automatico, si risolve con la skill |
| solo su Dev.to | Esiste sul portale ma non c'è il file | scarica in locale |
| solo locale | C'è il file ma non l'articolo | nessuna |

### Kanban

La vista Kanban ha una colonna per stato editoriale: Bozza, Revisionata, Pronta, Pubblicato. Trascinare una bozza tra le prime tre colonne imposta `stato` nel front-matter del file, senza chiamate a Dev.to. La colonna Pubblicato non accetta drop: pubblicare è un'azione a parte, non un trascinamento. Lo stato di sync resta un badge sulla card.

## Skill per Claude Code

`.claude/skills/devto-articles/` va nella cartella `.claude/skills/` del repo dei contenuti (copiata o collegata con un link simbolico: non ho verificato che Claude Code segua i link simbolici per le skill). Descrive le operazioni in cui un confronto a hash evita di sovrascrivere una modifica altrui: creazione di un articolo nuovo, pull, push, conflitti, pubblicazione. Non contiene norme di scrittura: quelle sono del repo dei contenuti.

## Sicurezza

- La chiave API vive solo nell'ambiente. Il server la aggiunge lui alle richieste verso Dev.to, il browser non la vede mai.
- I punti di scrittura del server (`/api/pull`, `/api/push`, `/api/stato` e gli altri) non hanno autenticazione: chiunque possa connettersi a `127.0.0.1` sulla tua macchina può chiamarli. Non esporre la porta.
- Tieni privato il repo dei contenuti: le bozze possono contenere appunti non destinati alla pubblicazione.

## Licenza

MIT, vedi [`LICENSE`](./LICENSE).
