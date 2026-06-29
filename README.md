# Et al.

*Your papers and everyone else's.*

Local PDF library manager for clinical research. Drop a PDF, it pulls metadata from CrossRef, classifies it into a topic with AI (within your declared field), renames the file as `Author-Title-Journal-Year.pdf`, files it under the matching topic folder, and indexes it in `library.md`.

## Quick start

```bash
cd article-organizer
python -m venv .venv && source .venv/bin/activate    # macOS/Linux
# .venv\Scripts\activate                              # Windows
pip install -r requirements.txt
python app.py
```

A native window opens. On first run:

1. **Choose folder…** — where the library lives. To use it on more than one computer, pick a **synced folder** (the setup screen suggests detected ones like Google Drive). If the folder already holds an Et al. library, you get an **Open this library** button instead (see *Sync* below).
2. **Choose your field(s)** of practice (Cardiology, Oncology, Radiology, …, or a custom one). These scope how new papers are classified — they don't predefine any topics.

The app creates:

```
EtAl/
├── library.db           # SQLite — source of truth
├── library.md           # auto-regenerated index
├── topics.yaml          # { fields: [...], topics: {...} }
├── _uncategorized/      # overflow / out-of-field
├── _inbox/
└── ...                  # topic folders, created as your library grows
```

There are **no preset topics**. Your taxonomy grows organically: as you add papers, the AI files each one into an existing topic or proposes a new one within your field(s).

## How it works

- **Drop a PDF** → first-page text → DOI regex → CrossRef → metadata → **AI suggests a topic** within your field(s) (reusing an existing topic or proposing a new one) → you confirm → the file is renamed, moved, and indexed.
- **AI summary** → one click writes a faithful 2–3 sentence summary you can store with the article.
- **No DOI found?** Paste a DOI/URL or fill the 4 fields by hand.
- **Duplicate DOI?** Skipped silently with a toast notification.
- **Library tab** → SQLite FTS5 search across title/abstract/author/tags, filter by topic, click to read PDF inline.
- **Topics tab** → add/edit/delete topics, edit your **fields**, optionally install a ready-made topic **pack**. Renaming a topic moves its folder and updates the DB. Deleting a topic with articles in it is blocked.

Everything is confirmation-before-save: nothing is filed until you click **Save**.

## Bulk import

Got hundreds of PDFs? **Inbox → "Bulk import a folder…"**, pick a folder, and the app processes every PDF in the background (DOI → CrossRef → AI topic). You then get a **review table** — adjust topics, uncheck any you don't want (duplicates are pre-unchecked) — and click **Import selected** to commit the whole batch at once. Originals are copied, not moved. If the AI hits its rate limit mid-run, the rest fall back to keyword matching (flagged in the table) and you can reclassify later from Tools.

## Sync across computers

The library folder is fully portable. To use one library on several machines (e.g. a Windows PC and a MacBook):

1. Put the library in a **cloud-synced folder** (Google Drive, Dropbox, OneDrive, iCloud).
2. On the first machine, create it there as usual.
3. On the second machine, install Et al. and on the folder step pick the **same synced folder** → **Open this library** (or, later, **Tools → Open / switch library**).

The cloud service handles the actual syncing; Et al. just points at the folder.

> ⚠️ **Don't run the app on two machines at the same time** (or before the sync finishes). The SQLite database can be corrupted by concurrent/mid-sync writes. Open it on one machine at a time and let the sync settle before switching.

## AI assist — optional

Topic classification and summaries can use any **OpenAI-compatible** LLM provider. It's **off until you add a key**; without one, the app classifies by keywords. In **Tools → AI assist**, pick a provider (it fills the endpoint + model), paste a key, tick **Enable**, **Save**:

| Provider | Cost | Notes |
|---|---|---|
| **Google Gemini** | Free tier | Generous limits — best for large batches. Key: [aistudio.google.com/apikey](https://aistudio.google.com/apikey), model `gemini-2.0-flash`. |
| **Groq** | Free tier | Very fast, but a low daily token cap (rough for big batches). Key: [console.groq.com/keys](https://console.groq.com/keys). |
| **Ollama** | Free, local | Runs on your machine, unlimited & private. Install [Ollama](https://ollama.com), `ollama pull qwen2.5:7b`; no key needed. |
| **Custom** | — | Any OpenAI-compatible base URL (OpenRouter, Together, …). |

- Your key is stored locally in `config.json` and is only ever sent to your chosen provider.
- **Requests/min** paces batch jobs to fit free-tier limits (e.g. ~15 for Gemini free); jobs retry on rate-limit.
- Any AI failure (offline, rate-limited, no key) silently falls back to keyword matching.

### Bulk classification / fixing `_uncategorized`

For a big import that outran a free tier (papers landed in `_uncategorized`), use **Tools → Reclassify with AI** — a background job that re-runs AI over existing articles, paced to your Requests/min, then lets you review and apply the proposed moves. `_uncategorized` is reserved for papers outside your declared field(s); change your fields anytime in the Topics tab.

## Search syntax

Searches use SQLite FTS5 prefix matching. Examples:

- `imaging` — matches "imaging", "imaged"
- `IVUS PCI` — matches articles containing both terms
- `"left main"` — exact phrase

## Packs (optional)

Packs are pre-built topic taxonomies (`packs/*.yaml`) you can install from the Topics tab to seed topics instead of growing them from scratch. They coexist with fields and with custom topics; cross-pack name conflicts are suffixed with the pack slug.

## Packaging as a standalone app

Use the helper scripts (each OS builds its own artifact — PyInstaller doesn't cross-compile):

```bash
# Windows  ->  dist\EtAl.exe
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1

# macOS    ->  dist/EtAl-macos.app.zip
bash scripts/build_macos.sh
```

The Windows `.exe` needs the **Microsoft Edge WebView2 runtime** (preinstalled on Windows 11). On macOS, codesign and notarize for distribution; for personal use, the unsigned `.app` runs fine. See **[BUILD.md](BUILD.md)** for the full build-and-release checklist.

## Updating

**Tools → App update → Check for updates** compares the running version with the latest [GitHub Release](https://github.com/stefanogarzon/etal/releases) and shows its release notes. **Apply update** then:
- **Packaged macOS app:** downloads the new `.app`, clears the Gatekeeper quarantine flag, swaps the bundle, and relaunches automatically.
- **Source checkout:** runs `git pull --ff-only`.

Publishing a new version = bump `__version__` in `server.py`, build, and attach the `.app.zip` to a GitHub Release whose tag is the new version (e.g. `v0.2.0`).

## Config location

The library path and your Groq settings live in `~/.config/etal/config.json`. The library's fields and topics live in the library's own `topics.yaml`. To switch libraries, delete the config file and restart.

## Limitations & next steps

- DOI extraction depends on the publisher embedding a recognizable DOI on page 1–2. ~95% hit rate in mainstream journals; older scans need manual lookup.
- CrossRef abstracts come as JATS XML — stripped to plain text but formatting is lost.
- The shared Groq key has free-tier rate limits shared across all users; add your own key for heavy/bulk use.
- Sync relies on an external cloud service; the app itself does not sync, and SQLite shouldn't be opened on two machines at once.
