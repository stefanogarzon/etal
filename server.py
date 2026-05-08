"""
Et al. backend — FastAPI server.

Responsibilities:
- First-run configuration (library folder path)
- PDF ingestion: extract DOI → CrossRef lookup → topic suggestion → save
- SQLite + FTS5 storage
- Search & filter
- Topic CRUD (with folder rename on the filesystem)
- Auto-regeneration of library.md after every mutation
- Serving PDFs to the embedded webview
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import httpx
import yaml
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pypdf import PdfReader

# ---------------------------------------------------------------------------
# Paths & config
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).parent
FRONTEND_DIR = APP_DIR / "frontend"
DEFAULT_TOPICS_FILE = APP_DIR / "topics.yaml"

CONFIG_DIR = Path.home() / ".config" / "etal"
CONFIG_FILE = CONFIG_DIR / "config.json"

DOI_REGEX = re.compile(r"10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+", re.IGNORECASE)
CROSSREF_URL = "https://api.crossref.org/works/{doi}"

logger = logging.getLogger("etal")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Set by app.py once the webview window exists
_window_ref: Any = None


def set_window_ref(win: Any) -> None:
    global _window_ref
    _window_ref = win


def load_config() -> dict | None:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return None


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def library_root() -> Path:
    cfg = load_config()
    if not cfg:
        raise HTTPException(status_code=400, detail="Library not configured")
    return Path(cfg["library_path"])


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doi TEXT UNIQUE,
    author TEXT NOT NULL,
    title TEXT NOT NULL,
    journal TEXT,
    year INTEGER,
    topic TEXT NOT NULL,
    filename TEXT NOT NULL,
    abstract TEXT,
    tags TEXT,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title, abstract, author, tags,
    content='articles', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, abstract, author, tags)
    VALUES (new.id, new.title, new.abstract, new.author, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, abstract, author, tags)
    VALUES ('delete', old.id, old.title, old.abstract, old.author, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, abstract, author, tags)
    VALUES ('delete', old.id, old.title, old.abstract, old.author, old.tags);
    INSERT INTO articles_fts(rowid, title, abstract, author, tags)
    VALUES (new.id, new.title, new.abstract, new.author, new.tags);
END;
"""


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(library_root() / "library.db")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_library(path: Path) -> None:
    """Create folder structure, DB, and seed topics.yaml on first run."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "_uncategorized").mkdir(exist_ok=True)
    (path / "_inbox").mkdir(exist_ok=True)  # quarantine for failed ingestion

    # Seed topics.yaml from default template if missing
    topics_file = path / "topics.yaml"
    if not topics_file.exists():
        shutil.copy(DEFAULT_TOPICS_FILE, topics_file)

    # Create folders for each topic
    for topic in load_topics(path).keys():
        (path / topic).mkdir(exist_ok=True)

    # Init DB
    conn = sqlite3.connect(path / "library.db")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    # Seed library.md
    (path / "library.md").write_text("# Et al.\n\n_Empty._\n")


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

def topics_path(root: Path | None = None) -> Path:
    return (root or library_root()) / "topics.yaml"


def load_topics(root: Path | None = None) -> dict[str, dict]:
    p = topics_path(root)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return data.get("topics", {})


def save_topics(topics: dict[str, dict]) -> None:
    topics_path().write_text(yaml.safe_dump({"topics": topics}, sort_keys=True))


def suggest_topic(text: str) -> str:
    """Score topics by keyword hits; return best match or '_uncategorized'."""
    text_lc = text.lower()
    scores: dict[str, int] = {}
    for topic, meta in load_topics().items():
        score = 0
        for kw in meta.get("keywords", []):
            if kw.lower() in text_lc:
                score += 1
        if score > 0:
            scores[topic] = score
    if not scores:
        return "_uncategorized"
    return max(scores, key=lambda k: scores[k])


# ---------------------------------------------------------------------------
# PDF & metadata
# ---------------------------------------------------------------------------

def extract_first_pages_text(pdf_path: Path, n: int = 2) -> str:
    try:
        reader = PdfReader(str(pdf_path))
        pages = reader.pages[:n]
        return "\n".join(p.extract_text() or "" for p in pages)
    except Exception as e:
        logger.warning("PDF extraction failed: %s", e)
        return ""


def find_doi(text: str) -> str | None:
    m = DOI_REGEX.search(text)
    if not m:
        return None
    # Strip trailing punctuation that often gets glued to the DOI
    doi = m.group(0).rstrip(".,;)")
    return doi


def crossref_lookup(doi: str) -> dict | None:
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(
                CROSSREF_URL.format(doi=doi),
                headers={"User-Agent": "EtAl/0.1 (local app)"},
            )
            if r.status_code != 200:
                return None
            msg = r.json().get("message", {})
            return parse_crossref(msg, doi)
    except Exception as e:
        logger.warning("CrossRef lookup failed for %s: %s", doi, e)
        return None


def parse_crossref(msg: dict, doi: str) -> dict:
    title = (msg.get("title") or [""])[0]
    journal = (msg.get("container-title") or [""])[0]
    journal_short = (msg.get("short-container-title") or [journal])[0]
    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0]:
            year = parts[0][0]
            break
    authors = msg.get("author") or []
    first_author = authors[0].get("family", "Unknown") if authors else "Unknown"
    abstract = msg.get("abstract", "")
    # CrossRef abstracts contain JATS XML — strip tags crudely
    abstract = re.sub(r"<[^>]+>", "", abstract).strip()
    return {
        "doi": doi,
        "author": first_author,
        "title": title,
        "journal": journal_short or journal,
        "year": year,
        "abstract": abstract,
    }


def slugify(s: str, max_words: int = 6) -> str:
    """CamelCase-ish slug: take first N significant words, drop punctuation."""
    words = re.findall(r"[A-Za-z0-9]+", s)
    stop = {"a", "an", "the", "of", "in", "on", "for", "to", "and", "or", "with", "by"}
    significant = [w for w in words if w.lower() not in stop][:max_words]
    return "".join(w.capitalize() for w in significant) or "Untitled"


def make_filename(meta: dict) -> str:
    author = re.sub(r"[^A-Za-z]", "", meta.get("author") or "Unknown") or "Unknown"
    title = slugify(meta.get("title") or "Untitled")
    journal = re.sub(r"[^A-Za-z0-9]", "", meta.get("journal") or "Journal") or "Journal"
    year = meta.get("year") or "ND"
    return f"{author}-{title}-{journal}-{year}.pdf"


def unique_path(folder: Path, filename: str) -> Path:
    target = folder / filename
    if not target.exists():
        return target
    stem, ext = target.stem, target.suffix
    i = 2
    while True:
        candidate = folder / f"{stem}-{i}{ext}"
        if not candidate.exists():
            return candidate
        i += 1


# ---------------------------------------------------------------------------
# library.md generator
# ---------------------------------------------------------------------------

def regenerate_library_md() -> None:
    root = library_root()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM articles ORDER BY topic, year DESC, author"
        ).fetchall()

    lines = ["# Et al.", "", "_Your papers and everyone else's._", ""]
    lines.append(f"_Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_  ")
    lines.append(f"_Total: {len(rows)} articles_")
    lines.append("")

    by_topic: dict[str, list] = {}
    for r in rows:
        by_topic.setdefault(r["topic"], []).append(r)

    for topic in sorted(by_topic.keys()):
        items = by_topic[topic]
        lines.append(f"## {topic} ({len(items)})")
        lines.append("")
        for r in items:
            rel = f"{r['topic']}/{r['filename']}"
            entry = (
                f"- [{r['author']} — {r['title']} — *{r['journal']}* — {r['year']}]"
                f"({rel})"
            )
            if r["doi"]:
                entry += f"  ·  [doi](https://doi.org/{r['doi']})"
            lines.append(entry)
        lines.append("")

    (root / "library.md").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Et al.")


# ----- Models -----

class SetupRequest(BaseModel):
    library_path: str


class SaveRequest(BaseModel):
    temp_id: str  # filename in tempdir from /api/ingest
    doi: str | None = None
    author: str
    title: str
    journal: str | None = None
    year: int | None = None
    topic: str
    abstract: str | None = None
    tags: str | None = None


class TopicRequest(BaseModel):
    name: str
    keywords: list[str] = []


class TopicRename(BaseModel):
    old_name: str
    new_name: str
    keywords: list[str] = []


# ----- Routes: setup -----

@app.get("/api/config")
def get_config() -> dict:
    cfg = load_config()
    return {"configured": cfg is not None, "config": cfg}


@app.post("/api/setup")
def post_setup(req: SetupRequest) -> dict:
    path = Path(req.library_path).expanduser().resolve()
    init_library(path)
    save_config({"library_path": str(path)})
    return {"ok": True, "library_path": str(path)}


# ----- Routes: ingestion -----

# Tempdir for staged uploads awaiting confirmation
STAGING = Path(tempfile.gettempdir()) / "etal_staging"
STAGING.mkdir(exist_ok=True)


@app.post("/api/ingest")
async def post_ingest(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDFs are accepted")

    # Stage the file
    temp_id = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}.pdf"
    temp_path = STAGING / temp_id
    content = await file.read()
    temp_path.write_bytes(content)

    # Extract text and try DOI
    text = extract_first_pages_text(temp_path)
    doi = find_doi(text)

    meta: dict[str, Any] = {
        "doi": None, "author": "", "title": "", "journal": "",
        "year": None, "abstract": "",
    }
    source = "manual"

    if doi:
        cr = crossref_lookup(doi)
        if cr:
            meta = cr
            source = "crossref"

    # Check duplicate
    duplicate = False
    if meta.get("doi"):
        with db() as conn:
            row = conn.execute(
                "SELECT id FROM articles WHERE doi = ?", (meta["doi"],)
            ).fetchone()
            duplicate = row is not None

    suggested_topic = suggest_topic(
        f"{meta.get('title','')} {meta.get('abstract','')} {text[:1500]}"
    )

    return {
        "temp_id": temp_id,
        "original_filename": file.filename,
        "source": source,
        "doi_found_in_pdf": doi,
        "metadata": meta,
        "suggested_topic": suggested_topic,
        "duplicate": duplicate,
        "topics": list(load_topics().keys()) + ["_uncategorized"],
    }


@app.post("/api/lookup_doi")
def post_lookup_doi(payload: dict) -> dict:
    """Manual DOI/URL lookup (when extraction failed)."""
    raw = (payload.get("query") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty query")

    # Try to extract DOI from URL or raw string
    doi = find_doi(raw) or raw if raw.startswith("10.") else find_doi(raw)
    if not doi:
        return {"ok": False, "reason": "No DOI pattern found"}

    cr = crossref_lookup(doi)
    if not cr:
        return {"ok": False, "reason": "DOI not found in CrossRef"}

    duplicate = False
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM articles WHERE doi = ?", (cr["doi"],)
        ).fetchone()
        duplicate = row is not None

    suggested = suggest_topic(f"{cr.get('title','')} {cr.get('abstract','')}")
    return {"ok": True, "metadata": cr, "duplicate": duplicate,
            "suggested_topic": suggested}


@app.post("/api/save")
def post_save(req: SaveRequest) -> dict:
    temp_path = STAGING / req.temp_id
    if not temp_path.exists():
        raise HTTPException(status_code=404, detail="Staged file expired")

    # Skip-on-duplicate (also enforced by UNIQUE constraint)
    if req.doi:
        with db() as conn:
            row = conn.execute(
                "SELECT id, topic, filename FROM articles WHERE doi = ?", (req.doi,)
            ).fetchone()
            if row:
                logger.info("Skip duplicate DOI %s", req.doi)
                temp_path.unlink(missing_ok=True)
                return {"ok": False, "skipped": True,
                        "reason": "Already in library", "existing": dict(row)}

    root = library_root()
    topic = req.topic
    topic_dir = root / topic
    topic_dir.mkdir(exist_ok=True)

    meta = {
        "author": req.author or "Unknown",
        "title": req.title or "Untitled",
        "journal": req.journal or "Journal",
        "year": req.year,
    }
    filename = make_filename(meta)
    final_path = unique_path(topic_dir, filename)
    shutil.move(str(temp_path), str(final_path))

    with db() as conn:
        cur = conn.execute(
            """INSERT INTO articles
               (doi, author, title, journal, year, topic, filename, abstract, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (req.doi, req.author, req.title, req.journal, req.year, topic,
             final_path.name, req.abstract, req.tags),
        )
        new_id = cur.lastrowid

    regenerate_library_md()
    return {"ok": True, "id": new_id, "filename": final_path.name, "topic": topic}


# ----- Routes: query -----

@app.get("/api/articles")
def get_articles(q: str | None = None, topic: str | None = None) -> list[dict]:
    sql = "SELECT a.* FROM articles a"
    params: list[Any] = []
    where: list[str] = []

    if q:
        # FTS5 query — escape quotes
        fts_q = q.replace('"', '""')
        sql = (
            "SELECT a.* FROM articles a "
            "JOIN articles_fts f ON f.rowid = a.id "
            "WHERE articles_fts MATCH ?"
        )
        params.append(f'"{fts_q}"*')
    if topic:
        sql += " AND " if "WHERE" in sql else " WHERE "
        sql += "a.topic = ?"
        params.append(topic)

    sql += " ORDER BY a.added_at DESC LIMIT 500"

    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/article/{article_id}")
def get_article(article_id: int) -> dict:
    with db() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    return dict(row)


@app.get("/pdf/{article_id}")
def get_pdf(article_id: int) -> FileResponse:
    with db() as conn:
        row = conn.execute("SELECT topic, filename FROM articles WHERE id = ?",
                           (article_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    path = library_root() / row["topic"] / row["filename"]
    if not path.exists():
        raise HTTPException(404, "PDF missing on disk")
    return FileResponse(path, media_type="application/pdf")


# ----- Routes: topics CRUD -----

@app.get("/api/topics")
def get_topics() -> dict:
    topics = load_topics()
    counts: dict[str, int] = {}
    with db() as conn:
        rows = conn.execute(
            "SELECT topic, COUNT(*) c FROM articles GROUP BY topic"
        ).fetchall()
        for r in rows:
            counts[r["topic"]] = r["c"]
    return {"topics": topics, "counts": counts}


@app.post("/api/topics")
def post_topic(req: TopicRequest) -> dict:
    topics = load_topics()
    if req.name in topics:
        raise HTTPException(400, "Topic already exists")
    topics[req.name] = {"keywords": req.keywords}
    save_topics(topics)
    (library_root() / req.name).mkdir(exist_ok=True)
    return {"ok": True}


@app.put("/api/topics")
def put_topic(req: TopicRename) -> dict:
    topics = load_topics()
    if req.old_name not in topics:
        raise HTTPException(404, "Topic not found")

    if req.new_name != req.old_name:
        if req.new_name in topics:
            raise HTTPException(400, "Target name already exists")
        # Rename folder
        old_dir = library_root() / req.old_name
        new_dir = library_root() / req.new_name
        if old_dir.exists():
            old_dir.rename(new_dir)
        # Update DB
        with db() as conn:
            conn.execute("UPDATE articles SET topic = ? WHERE topic = ?",
                         (req.new_name, req.old_name))
        # Update topics.yaml
        topics[req.new_name] = {"keywords": req.keywords}
        del topics[req.old_name]
    else:
        topics[req.old_name]["keywords"] = req.keywords

    save_topics(topics)
    regenerate_library_md()
    return {"ok": True}


@app.delete("/api/topics/{name}")
def delete_topic(name: str) -> dict:
    topics = load_topics()
    if name not in topics:
        raise HTTPException(404, "Topic not found")
    with db() as conn:
        cnt = conn.execute("SELECT COUNT(*) c FROM articles WHERE topic = ?",
                           (name,)).fetchone()["c"]
    if cnt > 0:
        raise HTTPException(400, f"Topic has {cnt} articles — move them first")
    del topics[name]
    save_topics(topics)
    folder = library_root() / name
    if folder.exists() and not any(folder.iterdir()):
        folder.rmdir()
    return {"ok": True}


# ----- Routes: delete article -----

@app.delete("/api/article/{article_id}")
def delete_article(article_id: int) -> dict:
    with db() as conn:
        row = conn.execute("SELECT topic, filename FROM articles WHERE id = ?",
                           (article_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    pdf = library_root() / row["topic"] / row["filename"]
    pdf.unlink(missing_ok=True)
    regenerate_library_md()
    return {"ok": True}


# ----- Static frontend -----

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((FRONTEND_DIR / "index.html").read_text())


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
