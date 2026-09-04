"""Private helper for the ``web_search`` tool.

Runs a general web search against Bocha (博查) -- a China-hosted search API
(``POST https://api.bocha.cn/v1/web-search``; register an API key at
open.bocha.cn) -- over ``aiohttp`` (already a core dependency, so no new
packages). Bocha is the domestic drop-in replacement for the Serper-based
``search`` tool: it is reachable from mainland deployments without a proxy,
indexes the Chinese web well, and returns each hit with an optional AI
``summary`` shaped for LLM consumption. The Serper ``search`` tool stays
available as an optional Google/English/vertical supplement when
``SERPER_API_KEY`` is configured.

Auth: a key read from the ``BOCHA_API_KEY`` environment variable. Following
``search.py``, the agent-root ``.env`` (``agents/<product>/.env``) is loaded
first so deployments that keep keys there keep working; a key already present
in the process environment always wins (per-deployment global, re-read on
every call so a runtime change takes effect without a restart).

Every helper returns a plain ``dict`` -- ``ok=True`` with results, or
``ok=False`` with a ``message`` -- so the thin tool layer never has to handle
exceptions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import aiohttp

# Bocha Web Search endpoint; API docs live on open.bocha.cn (飞书文档: 博查搜索API).
_SEARCH_URL = "https://api.bocha.cn/v1/web-search"
_KEY_ENV = "BOCHA_API_KEY"
# ``search.py``-style backward compat: a key dropped in the agent-root
# ``.env`` (never committed) is picked up when the process env has none.
_AGENT_DOTENV = Path(__file__).resolve().parents[1] / ".env"

DEFAULT_TIMEOUT = 20.0  # seconds for connect + read
DEFAULT_COUNT = 10
_COUNT_MAX = 50  # Bocha accepts ``count`` in [1, 50]
_FRESHNESS_CHOICES = ("noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear")

# Bocha mirrors the Bing ``webPages`` envelope
# (``{data: {webPages: {value: [...]}}}``). The keys are the documented ones;
# the alias lists tolerate minor drift so a schema change degrades to a
# missing field instead of a hard failure.
_TITLE_KEYS = ("name", "title", "headline")
_URL_KEYS = ("url", "link", "href")
_SNIPPET_KEYS = ("snippet", "description", "content")
_SUMMARY_KEYS = ("summary", "aiSummary", "answer")
_SITE_KEYS = ("siteName", "site", "domain")
_DATE_KEYS = ("datePublished", "dateLastCrawled", "publishTime", "displayTime", "date")


def dumps_result(result: dict[str, Any]) -> str:
    """Serialize a result dict to compact JSON for the tool return value."""
    return json.dumps(result, ensure_ascii=False)


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "provider": "bocha", "message": message, **extra}


def _load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` lines from *path* into ``os.environ`` if not set.

    Only fills keys the process env does not already define -- the gateway
    process env stays the authoritative, deployment-wide source.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in os.environ:
            continue
        os.environ[key] = value.strip().strip("\"'")


def _api_key() -> str:
    """Resolve ``BOCHA_API_KEY`` live: process env first, agent ``.env`` fallback."""
    _load_dotenv(_AGENT_DOTENV)
    return os.getenv(_KEY_ENV, "").strip()


def _clamp_count(count: int) -> int:
    """Coerce a requested count into Bocha's accepted [1, 50] range."""
    if count <= 0:
        return DEFAULT_COUNT
    return min(count, _COUNT_MAX)


def _pick(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    """First non-empty string value among *keys*; ``""`` when none present."""
    for key in keys:
        value = item.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _row(item: Any) -> dict[str, str] | None:
    """Shape one raw hit into the compact dict the tool returns.

    ``None`` for items that carry neither a title nor a URL -- such entries
    are unusable evidence and would only pad the result.
    """
    if not isinstance(item, dict):
        return None
    title = _pick(item, _TITLE_KEYS)
    url = _pick(item, _URL_KEYS)
    if not title and not url:
        return None
    return {
        "title": title or url,
        "url": url,
        "site": _pick(item, _SITE_KEYS),
        "date": _pick(item, _DATE_KEYS),
        "snippet": _pick(item, _SNIPPET_KEYS),
        "summary": _pick(item, _SUMMARY_KEYS),
    }


def _extract_items(payload: dict[str, Any]) -> tuple[list[dict[str, str]], int | None]:
    """Pull the web results out of a Bocha response payload.

    The documented shape nests the list under ``data.webPages.value`` with a
    ``totalEstimatedMatches`` estimate; a flat ``results``/``items`` list is
    accepted too. Returns ``(rows, total_estimated)``.
    """
    root = payload.get("data")
    if not isinstance(root, dict):
        root = payload
    pages = root.get("webPages")
    items: Any = None
    total: Any = None
    if isinstance(pages, dict):
        items = pages.get("value")
        if not isinstance(items, list):
            items = pages.get("items")
        total = pages.get("totalEstimatedMatches")
    if not isinstance(items, list):
        items = root.get("results")
    if not isinstance(items, list):
        items = root.get("items")
    if not isinstance(items, list):
        items = []
    rows = [row for item in items if isinstance(item, dict) and (row := _row(item)) is not None]
    if isinstance(total, bool):
        total = None
    return rows, (int(total) if isinstance(total, (int, float)) else None)


def _describe_api_error(status: int, payload: Any, text: str) -> str:
    """Turn a non-2xx Bocha response into a short, actionable message."""
    detail = ""
    if isinstance(payload, dict):
        msg = payload.get("msg")
        if isinstance(msg, str) and msg.strip():
            detail = msg.strip()
        elif payload.get("message"):
            detail = str(payload["message"])
        elif payload.get("detail"):
            detail = str(payload["detail"])
    if not detail and text and text.strip():
        detail = text.strip()[:300]
    if status in (401, 403):
        return f"Bocha authentication failed (HTTP {status}): check {_KEY_ENV}. {detail}".strip()
    if status == 429:
        return f"Bocha rate limit exceeded (HTTP 429): try again later. {detail}".strip()
    return f"Bocha API error (HTTP {status}). {detail}".strip()


async def _http_post(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout_s: float,
) -> tuple[int, Any, str]:
    """POST JSON to *url*; return ``(status, parsed_json_or_None, raw_text)``.

    Transport failures (timeout / connection error) are reported as
    ``status == 0`` with the failure description in *raw_text*, so callers
    only ever handle the returned tuple.
    """
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout, headers=headers) as session,
            session.post(url, json=body) as response,
        ):
            status = response.status
            text = await response.text()
    except TimeoutError:
        return 0, None, f"timed out after {timeout_s:.0f}s"
    except aiohttp.ClientError as exc:
        return 0, None, f"{type(exc).__name__}: {exc}"
    try:
        return status, json.loads(text), text
    except json.JSONDecodeError, UnicodeDecodeError:
        return status, None, text


async def web_search_impl(
    query: str,
    count: int = DEFAULT_COUNT,
    freshness: str = "noLimit",
    summary: bool = True,
    site: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run a web search against Bocha and return a result dict.

    See the ``web_search`` tool docstring for the parameter contract. Returns
    ``{"ok": True, "provider": "bocha", "query", "count", "total_estimated",
    "results": [...]}`` on success, or ``{"ok": False, "message"}`` on any
    failure (missing key, bad argument, auth, rate limit, network error).
    """
    query = (query or "").strip()
    if not query:
        return _error("`query` must not be empty.")

    key = _api_key()
    if not key:
        return _error(
            f"Web search (Bocha) is not configured. Set the {_KEY_ENV} environment variable "
            "to a Bocha API key (register at https://open.bocha.cn; new accounts get a free "
            "trial package). For Google/English results when SERPER_API_KEY is set instead, "
            "use the `serper_google_search` tool."
        )

    if freshness not in _FRESHNESS_CHOICES:
        return _error(f"`freshness` must be one of {', '.join(_FRESHNESS_CHOICES)}, got {freshness!r}.")

    body: dict[str, Any] = {
        "query": query,
        "count": _clamp_count(count),
        "freshness": freshness,
    }
    if summary:
        body["summary"] = True
    site = (site or "").strip() if site is not None else ""
    if site:
        # Accept "https://zhihu.com/a/b" and normalize to the bare host the
        # API expects; never send a scheme or path.
        if "://" in site:
            site = site.split("://", 1)[1]
        site = site.split("/", 1)[0]
        if site:
            body["site"] = site

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    status, payload, text = await _http_post(_SEARCH_URL, headers, body, timeout_s)
    if status == 0:
        return _error(f"Request failed: {text}")
    if status < 200 or status >= 300:
        return _error(_describe_api_error(status, payload, text), status=status)
    if not isinstance(payload, dict):
        return _error("Bocha returned an unexpected (non-JSON) response.")

    code = payload.get("code")
    if code not in (None, 200):
        msg = payload.get("msg")
        if not isinstance(msg, str) or not msg.strip():
            msg = f"Bocha business error (code {code})."
        return _error(str(msg).strip(), code=code)

    rows, total_estimated = _extract_items(payload)
    return {
        "ok": True,
        "provider": "bocha",
        "query": query,
        "count": len(rows),
        "total_estimated": total_estimated,
        "results": rows,
    }
