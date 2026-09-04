"""web_search tool — general web search via Bocha (博查), the domestic default.

This is the primary general web search for the workspace. It queries the
China-hosted Bocha Web Search API (``POST https://api.bocha.cn/v1/web-search``,
docs on open.bocha.cn) over ``aiohttp`` (already a core dependency, no extra
packages): no proxy needed from mainland deployments, strong Chinese-web
coverage, and each hit carries an optional AI ``summary`` shaped for the model.

It is the domestic replacement for the Serper-backed ``search`` tool: prefer
``web_search`` for everyday searches (Chinese or global content). The
``search`` group (``serper_google_search`` / ``serper_call``) remains
available for Google-only/English queries and the vertical searches (images,
maps, scholar, patents, news, ...) when ``SERPER_API_KEY`` is configured.

Auth: set ``BOCHA_API_KEY`` in the environment (or the agent-root ``.env``)
to a Bocha API key; register at https://open.bocha.cn (new accounts get a
free trial package). The heavy logic lives in ``_web_search_impl`` so the
import stays light on the tool-discovery path.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _web_search_impl as _ws


async def web_search(
    query: str,
    count: int = 10,
    freshness: str = "noLimit",
    summary: bool = True,
    site: str | None = None,
) -> str:
    """Search the web (Bocha / 博查) and return compact, citable results.

    Use this as the **default** web search for any question that needs current
    or external information -- news, facts, people, products, documents,
    prices. Bocha is a China-hosted search index, so it works without a proxy
    from mainland deployments and covers the Chinese web well. Each hit is
    returned with its title, URL, site, date and snippet, plus an AI-generated
    ``summary`` when requested.

    For Google-only / English-heavy queries or vertical searches (images,
    maps, scholar, patents, news, shopping), use the ``serper_*`` tools
    instead when ``SERPER_API_KEY`` is configured.

    Args:
        query: The search query -- a natural-language phrase or keywords
            (e.g. "2024 年阿里巴巴 ESG 报告要点").
        count: How many results to return (default 10, max 50).
        freshness: Time range filter -- "noLimit" (default), "oneDay",
            "oneWeek", "oneMonth" or "oneYear".
        summary: Whether to request an AI summary for each result (default
            True). Slightly slower, but the summaries are often directly
            usable as answer material.
        site: Restrict results to one site's domain, e.g. "zhihu.com" or
            "https://36kr.com". Omit for an unrestricted search.

    Returns:
        JSON with ok=true, the echoed query, count, total_estimated (when the
        API reports one) and a ``results`` list of {title, url, site, date,
        snippet, summary}; or ok=false with a ``message`` on failure (missing
        key, invalid argument, rate limit, network error).
    """
    result = await _ws.web_search_impl(
        query=query,
        count=count,
        freshness=freshness,
        summary=summary,
        site=site,
    )
    return _ws.dumps_result(result)
