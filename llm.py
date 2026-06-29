"""llm.py — optional AI layer (Groq, OpenAI-compatible chat API).

Stays *advisory*: it suggests a topic and writes plain-language summaries that
the user reviews and confirms. It never commits anything by itself, so the
"confirmation-before-save" invariant is preserved.

Degrades gracefully — if the feature is disabled, has no API key, or the call
fails (offline, rate-limited, bad key), every entry point returns ``None`` and
the caller falls back to the existing keyword heuristics in ``server.py``.

No new dependency: Groq speaks the OpenAI chat-completions protocol, and the
project already ships ``httpx``.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import httpx

logger = logging.getLogger("etal.llm")

# Default provider is Groq, but any OpenAI-compatible endpoint works (Groq,
# Google Gemini's OpenAI layer, a local Ollama, OpenRouter, …). The base URL is
# configurable in Tools; we append "/chat/completions".
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"


class LLMError(Exception):
    """Raised by _chat on a failed Groq call. ``reason`` is one of
    'rate_limited' | 'auth' | 'http' | 'network'."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(detail or reason)


def _http_reason(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    return "http"

# --- Shared default key (AI on out-of-the-box for everyone) ------------------
# The key is NEVER committed to source (this repo is public). It is resolved at
# runtime from, in order:
#   1. the ETAL_GROQ_KEY env var (source runs / CI),
#   2. a `groq_key.txt` file next to the app or inside the PyInstaller bundle —
#      git-ignored; the maintainer drops it in before building so the packaged
#      app ships with a shared, dedicated, rotatable key,
#   3. empty -> AI is opt-in (the user pastes their own key in Tools).
# A per-user key set in Tools always overrides this default.
def _load_builtin_key() -> str:
    env = os.environ.get("ETAL_GROQ_KEY", "").strip()
    if env:
        return env
    bases: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        bases.append(Path(meipass))
    bases.append(Path(__file__).resolve().parent)
    for base in bases:
        try:
            f = base / "groq_key.txt"
            if f.exists():
                return f.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


BUILTIN_KEY = _load_builtin_key()
# AI is enabled by default; a user can still turn it off in Tools.
BUILTIN_ENABLED = True


def llm_enabled(cfg: dict | None) -> bool:
    """True when AI is usable: enabled (default on) AND a key is available
    (the user's own, or the shared built-in)."""
    return _llm_cfg(cfg) is not None


def _llm_cfg(cfg: dict | None) -> dict | None:
    llm = dict((cfg or {}).get("llm") or {})
    # 'enabled' defaults to BUILTIN_ENABLED when the user never set it.
    if not llm.get("enabled", BUILTIN_ENABLED):
        return None
    key = (llm.get("api_key") or BUILTIN_KEY or "").strip()
    if not key:
        return None
    llm["api_key"] = key
    llm["model"] = llm.get("model") or DEFAULT_MODEL
    llm["base_url"] = (llm.get("base_url") or DEFAULT_BASE_URL).strip()
    return llm


def _chat(
    llm: dict,
    messages: list[dict],
    *,
    json_mode: bool = False,
    max_tokens: int = 512,
    model: str | None = None,
) -> str:
    """One blocking chat-completions round-trip. Returns the assistant text on
    success, or raises ``LLMError`` (with a reason) on any failure. Callers
    decide whether to swallow it (passive paths) or surface it (user actions)."""
    body: dict = {
        "model": model or llm.get("model") or DEFAULT_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    url = (llm.get("base_url") or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"
    try:
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {llm['api_key']}",
                    # Cloudflare in front of api.groq.com rejects the default
                    # python-httpx UA with HTTP 403 (error 1010). Send a real one.
                    "User-Agent": "EtAl/0.1 (local app)",
                },
            )
    except Exception as e:  # network / timeout
        logger.warning("LLM call failed (%s): %s", url, e)
        raise LLMError("network", str(e)) from e
    if r.status_code != 200:
        logger.warning("LLM %s: %s", r.status_code, r.text[:300])
        raise LLMError(_http_reason(r.status_code), f"HTTP {r.status_code}")
    try:
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:  # malformed response
        logger.warning("Groq bad response: %s", e)
        raise LLMError("http", "malformed response") from e


def suggest_topic_llm(
    cfg: dict | None,
    title: str,
    abstract: str,
    body: str,
    topics: dict[str, dict],
    fields: list[str] | None = None,
) -> dict | None:
    """Ask the model to classify the paper into the library's organic taxonomy.

    ``fields`` are the user-declared specialty areas (e.g. ['Cardiology',
    'Oncology']) that bound the classification scope. The model reuses an
    existing topic when one fits, otherwise PROPOSES a new topic within those
    field(s) — so the taxonomy grows from the actual collection. ``_uncategorized``
    is reserved for papers outside the declared field(s). Returns::

        {"topic": str, "is_new": bool, "confidence": float, "reason": str}

    or ``None`` when the feature is off / the call fails / the output is
    unusable. The caller sanitizes new names before they touch the filesystem.
    """
    llm = _llm_cfg(cfg)
    if not llm:
        return None

    catalogue = "\n".join(
        f"- {name}: {', '.join((meta.get('keywords') or [])[:8])}"
        for name, meta in topics.items()
    ) or "(no topics yet — this is a new library)"

    if fields:
        field_ctx = (
            "This library collects papers in the following medical field(s): "
            f"{', '.join(fields)}.\n"
            "Scope every decision to these field(s).\n\n"
        )
        scope_rule = (
            "- Use '_uncategorized' ONLY for papers that fall outside the "
            f"declared field(s) ({', '.join(fields)}) entirely.\n"
        )
    else:
        field_ctx = ""
        scope_rule = (
            "- If no field is declared, infer the BROAD field from the existing "
            "topics (or, if there are none, from the paper itself) and treat "
            "that whole field as in-scope.\n"
            "- Use '_uncategorized' ONLY for papers outside that field entirely.\n"
        )

    user = (
        field_ctx
        + f"Existing topics (name: example keywords):\n{catalogue}\n\n"
        f"Article title: {title}\n\n"
        f"Abstract / first-page text:\n{(abstract or body or '')[:3000]}\n\n"
        "Pick the SINGLE best topic for this article.\n"
        "- If an existing topic clearly fits, reuse it (is_new=false).\n"
        "- Otherwise, if the paper is within the library's field(s), PROPOSE a "
        "new topic that captures its subject (is_new=true). Strongly prefer "
        "proposing a sensible new topic over '_uncategorized' — the taxonomy "
        "grows with the collection. Keep new names short and TitleCase (no "
        "spaces), e.g. 'Electrophysiology', 'HeartFailure', 'LungCancer', "
        "'Neuroimaging', 'BreastCancer'.\n"
        + scope_rule
        + "\n"
        'Respond as JSON: {"topic": "<name>", "is_new": <true|false>, '
        '"confidence": <0-1>, "reason": "<one short phrase>"}'
    )
    try:
        out = _chat(
            llm,
            [
                {
                    "role": "system",
                    "content": (
                        "You classify clinical research papers into a GROWING "
                        "topic taxonomy. Prefer creating a precise new topic "
                        "over leaving an in-field paper uncategorized. You "
                        "return strict JSON and nothing else."
                    ),
                },
                {"role": "user", "content": user},
            ],
            json_mode=True,
            max_tokens=200,
        )
    except LLMError as e:
        # Passive path: never fail ingestion. Report the reason so the UI can
        # warn (e.g. rate-limited) while the caller falls back to keywords.
        return {"error": e.reason}
    if not out:
        return None
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Groq topic output not JSON: %s", out[:200])
        return None

    chosen = (data.get("topic") or "").strip()
    if not chosen:
        return None
    is_new = bool(data.get("is_new")) and chosen not in topics and chosen != "_uncategorized"
    # If the model flagged a name that actually exists, treat it as existing.
    if chosen in topics or chosen == "_uncategorized":
        is_new = False
    result = {
        "topic": chosen,
        "is_new": is_new,
        "confidence": data.get("confidence"),
        "reason": (data.get("reason") or "").strip(),
    }
    logger.info("LLM topic: %s (new=%s) — %s", chosen, is_new, result["reason"])
    return result


def summarize_text(cfg: dict | None, title: str, text: str) -> str | None:
    """Write a faithful 2-3 sentence plain-language summary using the configured
    model. Returns ``None`` when AI is off or there's nothing to summarize;
    raises ``LLMError`` on a failed call (user-initiated action, so the caller
    surfaces the reason)."""
    llm = _llm_cfg(cfg)
    if not llm or not (text or "").strip():
        return None
    return _chat(
        llm,
        [
            {
                "role": "system",
                "content": (
                    "You write concise, faithful 2-3 sentence plain-language "
                    "summaries of biomedical papers. Summarize ONLY what the "
                    "provided text explicitly states — never infer, add, or "
                    "guess results, outcomes, or conclusions that are not "
                    "present in the text. If the text does not report a result, "
                    "do not state one. No invented numbers, no citations, no "
                    "preamble — just the summary."
                ),
            },
            {
                "role": "user",
                "content": f"Title: {title}\n\nText:\n{text[:6000]}\n\n"
                "Write a 2-3 sentence summary.",
            },
        ],
        max_tokens=300,
    )
