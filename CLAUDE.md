# Et al. — project context

*Local PDF library manager. Your papers and everyone else's.*

## Stack

- **Python 3.11+**
- **PyWebView** — native window wrapping the UI (no browser)
- **FastAPI + uvicorn** — HTTP server running in a background thread
- **SQLite + FTS5** — single-file DB inside the user's library folder, source of truth
- **pypdf** — first-page text extraction
- **CrossRef API** — DOI → metadata (no auth, no key)
- **Vanilla JS + HTML** — single-page frontend, no build step

## File map

```
app.py              # entry: starts uvicorn thread + opens PyWebView window
server.py           # FastAPI routes + all business logic (DB, ingestion, topics)
llm.py              # optional Groq layer (advisory topic suggest + summaries)
frontend/index.html # single-page UI (tabs: Inbox, Library, Topics, Tools)
fields.yaml         # catalog of selectable specialty fields (AI scope, no topics)
packs/*.yaml        # optional pre-built topic taxonomies (cardio, mastology, blank)
requirements.txt
README.md
```

Per-library `topics.yaml` holds `{fields: [...], topics: {...}}` — the declared
specialty fields and the organically-grown topic taxonomy.

## Architecture decisions (locked in)

1. **SQLite is source of truth.** `library.md` is regenerated on every mutation — never edit it by hand.
2. **Confirmation-before-save on ingestion.** Dropping a PDF stages it in a tempdir; user reviews metadata in a card and clicks "Save" to commit. Discarding removes the temp file.
3. **AI is advisory, on by default.** A Groq layer (`llm.py`) *suggests* a topic (possibly a brand-new one) and writes summaries, but never auto-commits — the user confirms every classification. It is **enabled by default for everyone** via a shared built-in key (`llm.BUILTIN_KEY`, overridable by `ETAL_GROQ_KEY`); a user can paste their own key in Tools (overrides the shared one) or turn AI off. Any failure (offline / rate-limited / disabled) falls back to the offline keyword heuristic, so the app always works. Models: classification uses `llama-3.3-70b-versatile` (semantic reasoning); summaries use the cheaper/faster `llama-3.1-8b-instant` (SUMMARY_MODEL) to ease the shared rate limit. A failed Groq call raises `LLMError(reason)`; passive paths (ingest) report `ai.error` to the UI and fall back, user actions (summarize) surface it (HTTP 429 on rate limit).
   - **SECURITY:** the shared key ships in the bundle/source — anyone with the app can read it, and free-tier limits are per-key (shared across all users). Use a dedicated, rotatable key, never a personal one. A per-user key lives in `config.json` under `llm:` and is never returned to the frontend.
4. **Topic = folder.** Renaming a topic moves the folder and updates DB rows. Deleting a topic with articles is blocked.
   - **Organic, field-guided taxonomy.** At setup the user declares their **field(s)** of practice (from `fields.yaml`, e.g. Cardiology, Oncology) — these are *context*, not predefined topics. The library starts with no topics; the AI classifies each incoming paper within the declared field(s), reusing an existing topic or proposing a new one (which becomes first-class on Save). `_uncategorized` is reserved for papers outside the declared field(s). Fields are stored in the library's `topics.yaml` and editable in the Topics tab. Packs remain an *optional* way to pre-seed topics. When no field is declared, the AI infers it from existing topics / the paper itself (legacy behavior).
5. **Duplicates by DOI.** Skipped silently (with toast notification) on save. Enforced both at app level and via UNIQUE constraint.
6. **Library path is configurable.** Stored in `~/.config/etal/config.json`. First run shows a folder picker.

## Filename convention

`{Author}-{TitleSlug}-{Journal}-{Year}.pdf`

- Author: first author surname, alpha-only
- TitleSlug: first 6 significant words (stopwords removed), CamelCase
- Journal: `short-container-title` from CrossRef, alphanumeric only
- Year: 4 digits, or `ND` if missing
- Collisions: `-2`, `-3` suffix

## API surface

| Method | Path                       | Purpose                                  |
|--------|----------------------------|------------------------------------------|
| GET    | `/api/config`              | Has the user picked a library folder?    |
| POST   | `/api/setup`               | Initialize library at given path         |
| POST   | `/api/ingest`              | Stage PDF, return DOI/metadata/suggestion|
| POST   | `/api/lookup_doi`          | Manual DOI/URL → CrossRef                |
| POST   | `/api/save`                | Confirm & commit staged PDF              |
| GET    | `/api/articles?q=&topic=`  | List/search (FTS5 prefix)                |
| GET    | `/api/article/{id}`        | Single article metadata                  |
| DELETE | `/api/article/{id}`        | Remove from DB + delete PDF              |
| GET    | `/pdf/{id}`                | Serve PDF inline (used by `<embed>`)     |
| GET    | `/api/topics`              | Topics + article counts                  |
| POST   | `/api/topics`              | New topic                                |
| PUT    | `/api/topics`              | Rename + update keywords                 |
| DELETE | `/api/topics/{name}`       | Delete (only if empty)                   |
| GET    | `/api/fields`              | Field catalog + the library's selected   |
| PUT    | `/api/library/fields`      | Update the library's declared fields     |
| GET    | `/api/llm/settings`        | AI status (never returns the key)        |
| PUT    | `/api/llm/settings`        | Enable/disable, set key + model          |
| POST   | `/api/summarize/{temp_id}` | AI summary of a staged (unsaved) PDF     |
| POST   | `/api/article/{id}/summarize` | AI summary of a saved article, stored |
| POST   | `/api/bulk/start`          | Scan a folder, start background ingest   |
| GET    | `/api/bulk/status/{id}`    | Bulk job progress + processed items      |
| POST   | `/api/bulk/import`         | Commit selected bulk items               |
| GET    | `/api/cloud_locations`     | Detected synced folders (Drive, …)       |
| POST   | `/api/inspect_folder`      | Is this folder already a library?        |
| POST   | `/api/open_library`        | Point app at an existing library         |
| POST   | `/api/app/check_updates`   | Compare version vs latest GitHub Release |
| POST   | `/api/app/update`          | Apply update (mac .app swap / git pull)  |

## Conventions

- Python: type hints throughout, `from __future__ import annotations`, no async unless needed.
- Errors: raise `HTTPException` with clear `detail` strings — frontend toasts them as-is.
- Frontend: no framework, no build. Plain `fetch()` against the same-origin API. Keep `index.html` self-contained.
- Tests: there's no test suite yet. When adding logic, smoke-test via `TestClient` (see how server.py was validated initially).

## Starter packs
The `packs/` directory contains pre-built topic taxonomies (cardio, mastology, blank).
At first run, the user picks one or more packs via the setup screen. Selected packs are
merged into the library's `topics.yaml` with per-topic `pack:` provenance.

Additional packs can be installed (preserving existing topics) or reset (wipe + reinstall)
from the Topics tab. Conflicts between packs (e.g. "Imaging" in two packs) are resolved by
suffixing with the pack slug (`Imaging_mastology`); the UI displays these as "Imaging (Pack name)".

## Bulk import

Inbox → "Bulk import a folder…" picks a folder; the server scans it for PDFs and
runs a **background job** (`/api/bulk/start` → `/api/bulk/status/{id}` polling →
`/api/bulk/import`). Each PDF gets DOI→CrossRef→AI-topic (AI **throttled**, and on
a rate-limit it falls back to keyword for the rest of the batch, flagged in the
UI). Results land in a **review table**: per-row topic dropdown + checkboxes;
within-batch and existing-library duplicates are pre-unchecked. One confirmation
imports the lot. Files are **copied** (originals untouched). New topics register
on import; `regenerate_library_md()` runs once at the end. Job state is in-memory
(`_bulk_jobs`, lock-guarded) and dropped after import.

## Library location & cross-device sync

The library folder is fully portable (DB stores relative `topic/filename`). To use
one library on several computers, keep it in a **synced folder** (Google Drive,
Dropbox, …) and point each machine at it:
- `GET /api/cloud_locations` detects synced roots (incl. Google Drive `My Drive`/
  `Meu Drive` on Windows, `~/Library/CloudStorage/GoogleDrive-*` on macOS) and the
  setup screen offers them as quick-picks.
- `POST /api/inspect_folder` tells the UI whether a chosen folder already holds a
  library; `POST /api/open_library` points the app at it **without re-initializing**
  (preserves topics/fields). `POST /api/setup` also opens-if-exists rather than wipe.
- Tools → "Open / switch library" repoints an installed app.
- **SQLite + sync caveat:** don't run the app on two machines simultaneously (or
  before sync settles) — risks DB conflicts. WAL is intentionally NOT used (its
  `-wal/-shm` sidecars desync badly). Documented in the Tools UI.

## Things explicitly NOT in v0.1

- ~~Bulk PDF import~~ (added — see above)
- Per-article notes/highlights
- Export to BibTeX / CSV
- Cloud sync
- Multi-user / sharing
- Bulk/background AI runs (AI is one-at-a-time, advisory, on the open card/article)

## When iterating

- Keep the **SQLite-as-truth + regenerate-md** invariant. Any feature that touches articles must call `regenerate_library_md()` after committing.
- Keep the **confirmation-before-save** flow. Don't add silent ingestion — bad data is harder to clean than missing data.
- Keep the **single-file frontend**. If it ever needs a build step, reconsider the whole frontend stack first.
- Before changing the filename convention, consider that existing PDFs on disk won't match — write a migration script.
