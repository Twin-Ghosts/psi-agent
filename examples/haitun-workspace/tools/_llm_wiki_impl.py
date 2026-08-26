"""Private helpers for the ``llm_wiki`` toolset.

Implements Karpathy's "LLM wiki" pattern: instead of re-searching raw documents
on every question, the agent incrementally compiles knowledge into a persistent,
interlinked collection of Markdown pages that live under ``<workspace>/wiki/``.
Each page is a Markdown file with a small YAML frontmatter block (title, tags,
timestamps, aliases) and a body that cross-references other pages with
``[[wikilink]]`` syntax. Over time the wiki compounds into a browsable knowledge
base the agent can read, extend, and traverse by its links.

The heavy logic lives here so the tool-discovery import of ``llm_wiki`` stays
light. File IO is async via ``anyio.Path``; frontmatter is parsed/emitted with
``pyyaml`` (both already core dependencies).
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any

import _background_process_registry as _bg
import anyio
import yaml

WIKI_DIRNAME = "wiki"
MAX_CONTENT_BYTES = 512 * 1024  # 512 KiB cap per page body
DEFAULT_SEARCH_LIMIT = 20

#: 共享 wiki 的根目录环境变量。设了它,每个人的 wiki 就是「自己的页 + 这个共享库」两处
#: 的并集,而不是只有自己那份。
#:
#: 为什么需要它:每个飞书用户的 Session 有自己的 workspace(``<root>/<open_id>/``),而
#: 这些工具都按当前 workspace 解析路径 —— 个人笔记本该如此,但**组织级**的知识(公司
#: 工作树、部门职责、共用口径)每人各存一份就毫无意义:甲写的页乙查不到,而这正是
#: 「wiki 该是全组织共享」这个诉求撞上的墙。
SHARED_WIKI_ENV = "PSI_WIKI_SHARED_DIR"
# Collapse runs of non-"word" characters into a single dash. Under Python's
# default Unicode matching, ``\w`` covers letters/digits of ANY script (CJK,
# Cyrillic, …) plus underscore — so non-Latin titles like Chinese "校训" get a
# real, distinct slug instead of all collapsing to "untitled". Underscore is
# kept so intentional names like "_schema" survive.
_SLUG_RE = re.compile(r"\W+")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]")


def dumps_result(result: dict[str, Any]) -> str:
    """Serialize a result dict to compact JSON for the tool return value."""
    return json.dumps(result, ensure_ascii=False)


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "message": message, **extra}


def _iso_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def slugify(title: str) -> str:
    """Turn a page title into a stable, filesystem-safe slug (the filename stem)."""
    slug = _SLUG_RE.sub("-", title.strip().lower()).strip("-")
    return slug or "untitled"


def wiki_dir(workspace: anyio.Path) -> anyio.Path:
    return workspace / WIKI_DIRNAME


def shared_wiki_dir() -> anyio.Path | None:
    """The org-wide wiki directory, or None when no shared library is configured.

    Configured with ``PSI_WIKI_SHARED_DIR`` (an absolute path). Returns None rather than
    guessing a location: silently inventing a shared root would split one library into two
    the first time the env var went missing, and the halves diverge without any error.
    """
    raw = (os.environ.get(SHARED_WIKI_ENV) or "").strip()
    return anyio.Path(raw) if raw else None


def _roots(workspace: anyio.Path) -> list[tuple[str, anyio.Path]]:
    """Where pages may live, in lookup order: the caller's own wiki, then the shared one.

    Personal first so somebody's own page shadows a shared page of the same name — their
    workspace is the thing they control. Both are returned even when the shared root is the
    same directory as the personal one (a deduplicating caller handles that), because
    resolving that here would hide the overlap from the callers that must report it.
    """
    roots = [("personal", wiki_dir(workspace))]
    shared = shared_wiki_dir()
    if shared is not None:
        roots.append(("shared", shared))
    return roots


def _page_path(workspace: anyio.Path, slug: str) -> anyio.Path:
    return wiki_dir(workspace) / f"{slug}.md"


async def _locate_page(workspace: anyio.Path, slug: str) -> tuple[anyio.Path, str] | None:
    """Find an existing page by slug across both roots. Returns (path, scope) or None."""
    for scope, root in _roots(workspace):
        candidate = root / f"{slug}.md"
        if await candidate.exists():
            return candidate, scope
    return None


async def _write_target(workspace: anyio.Path, slug: str) -> tuple[anyio.Path, str]:
    """Where a write for *slug* must land: on top of the existing page, else personal.

    Editing follows the page: a shared page stays shared, so somebody fixing the org's tree
    fixes it for everyone instead of forking a private copy that silently shadows it. New
    pages default to personal — promoting one to the shared library is an explicit act
    (``scope="shared"``), never a side effect of writing.
    """
    found = await _locate_page(workspace, slug)
    if found is not None:
        return found
    return _page_path(workspace, slug), "personal"


def _resolve_scope(workspace: anyio.Path, scope: str) -> tuple[anyio.Path, str] | dict[str, Any]:
    """Turn an explicit ``scope`` into a target root, or an error dict when unusable."""
    wanted = (scope or "").strip().lower()
    if wanted in {"", "auto"}:
        return anyio.Path(""), "auto"
    if wanted == "personal":
        return wiki_dir(workspace), "personal"
    if wanted == "shared":
        shared = shared_wiki_dir()
        if shared is None:
            return _error(
                f"scope='shared' needs a shared library: set {SHARED_WIKI_ENV} to an absolute "
                "path that every Session can reach. Without it a 'shared' page would just be "
                "another personal page."
            )
        return shared, "shared"
    return _error(f"scope must be 'auto', 'personal', or 'shared' — got {scope!r}.")


def extract_links(body: str) -> list[str]:
    """Return the slugs a body links to via ``[[Target]]`` / ``[[Target|label]]``."""
    seen: dict[str, None] = {}
    for match in _WIKILINK_RE.finditer(body):
        seen.setdefault(slugify(match.group(1)), None)
    return list(seen)


def _serialize_page(meta: dict[str, Any], body: str) -> str:
    """Emit a page as YAML frontmatter + Markdown body."""
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{front}---\n\n{body.strip()}\n"


def _parse_page(text: str) -> tuple[dict[str, Any], str]:
    """Split stored text into (frontmatter dict, body). Tolerant of a missing block."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            raw_front = text[4:end]
            body = text[end + 4 :].strip("\n")
            try:
                meta = yaml.safe_load(raw_front) or {}
            except yaml.YAMLError:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            return meta, body
    return {}, text.strip("\n")


async def _atomic_write(path: anyio.Path, text: str) -> None:
    await path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp"
    await tmp.write_text(text, encoding="utf-8")
    if await path.exists():
        await path.unlink()
    await tmp.rename(path)


def _normalize_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        parts = re.split(r"[,\s]+", tags.strip())
        return [p for p in parts if p]
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    return []


async def _read_page(path: anyio.Path) -> tuple[dict[str, Any], str] | None:
    if not await path.exists():
        return None
    try:
        text = await path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_page(text)


async def wiki_write_impl(
    title: str,
    content: str,
    *,
    tags: Any = None,
    aliases: Any = None,
    overwrite: bool = True,
    scope: str = "auto",
    workspace_raw: str = "",
) -> dict[str, Any]:
    """Create or update a wiki page. Returns the saved page's metadata + links.

    ``scope`` decides which library the page lands in: ``auto`` (default) edits an existing
    page where it already lives and creates new ones as personal; ``shared`` puts it in the
    org-wide library everyone reads; ``personal`` forces the caller's own workspace.
    """
    if not title or not isinstance(title, str) or not title.strip():
        return _error("A non-empty page title is required.")
    if not isinstance(content, str):
        return _error("content must be a string.")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        return _error(f"content exceeds the {MAX_CONTENT_BYTES // 1024} KiB per-page limit.")

    workspace = _bg.resolve_workspace(workspace_raw)
    slug = slugify(title)
    resolved = _resolve_scope(workspace, scope)
    if isinstance(resolved, dict):
        return resolved
    root, wanted_scope = resolved
    if wanted_scope == "auto":
        path, page_scope = await _write_target(workspace, slug)
    else:
        path, page_scope = root / f"{slug}.md", wanted_scope

    existing = await _read_page(path)
    if existing is not None and not overwrite:
        return _error(
            f"Page {slug!r} already exists; pass overwrite=true to replace it.",
            slug=slug,
        )

    now = _iso_now()
    created = now
    if existing is not None:
        prev_meta, _ = existing
        created = str(prev_meta.get("created", now)) or now

    meta: dict[str, Any] = {
        "title": title.strip(),
        "slug": slug,
        "tags": _normalize_tags(tags),
        "aliases": _normalize_tags(aliases),
        "created": created,
        "updated": now,
    }
    links = extract_links(content)
    if links:
        meta["links"] = links

    try:
        await _atomic_write(path, _serialize_page(meta, content))
    except OSError as exc:
        return _error(f"Failed to write page: {exc}", slug=slug)

    return {
        "ok": True,
        "slug": slug,
        "path": str(path),
        "scope": page_scope,
        "created": existing is None,
        "title": meta["title"],
        "tags": meta["tags"],
        "links": links,
        "workspace": str(workspace),
    }


async def wiki_read_impl(title_or_slug: str, *, workspace_raw: str = "") -> dict[str, Any]:
    """Read one page's full Markdown (frontmatter + body) plus its parsed metadata."""
    if not title_or_slug or not title_or_slug.strip():
        return _error("A page title or slug is required.")
    workspace = _bg.resolve_workspace(workspace_raw)
    slug = slugify(title_or_slug)
    found = await _locate_page(workspace, slug)
    if found is None:
        return _error(f"No wiki page named {slug!r}.", slug=slug)
    path, page_scope = found
    page = await _read_page(path)
    if page is None:
        return _error(f"No wiki page named {slug!r}.", slug=slug)
    meta, body = page
    return {
        "ok": True,
        "slug": slug,
        "path": str(path),
        "scope": page_scope,
        "title": str(meta.get("title", slug)),
        "tags": _normalize_tags(meta.get("tags")),
        "aliases": _normalize_tags(meta.get("aliases")),
        "created": meta.get("created"),
        "updated": meta.get("updated"),
        "links": extract_links(body),
        "content": body,
    }


async def _iter_pages(workspace: anyio.Path) -> list[tuple[str, dict[str, Any], str]]:
    """Load every page as (slug, meta, body), sorted by slug — personal plus shared.

    One slug yields one page: the personal root wins, so a private page shadows a shared one
    of the same name rather than both showing up. Reading the same directory twice (when the
    shared root *is* the personal one) also collapses here, which is why the dedup lives in
    the iterator every reader shares instead of in each caller.
    """
    pages: dict[str, tuple[dict[str, Any], str]] = {}
    seen_dirs: set[str] = set()
    for scope, root in _roots(workspace):
        key = str(root)
        if key in seen_dirs or not await root.exists():
            continue
        seen_dirs.add(key)
        async for entry in root.glob("*.md"):
            page = await _read_page(entry)
            if page is None:
                continue
            meta, body = page
            slug = str(meta.get("slug") or entry.stem)
            # scope 只在这里注入,不落盘 —— 一页的归属是「它在哪个目录」这个事实,
            # 写进 frontmatter 就会在页面被搬动后变成谎话。
            pages.setdefault(slug, ({**meta, "_scope": scope}, body))
    return [(slug, meta, body) for slug, (meta, body) in sorted(pages.items())]


def _page_summary(slug: str, meta: dict[str, Any], body: str) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": str(meta.get("title", slug)),
        "tags": _normalize_tags(meta.get("tags")),
        "updated": meta.get("updated"),
        # 哪些页是全组织共享的、哪些只有自己看得到 —— 不报出来就分不清。
        "scope": str(meta.get("_scope", "personal")),
        "links": extract_links(body),
    }


async def wiki_list_impl(*, tag: str = "", workspace_raw: str = "") -> dict[str, Any]:
    """List every page (slug/title/tags/updated/links), optionally filtered by tag."""
    workspace = _bg.resolve_workspace(workspace_raw)
    tag_filter = tag.strip().lower()
    pages = await _iter_pages(workspace)
    out: list[dict[str, Any]] = []
    for slug, meta, body in pages:
        summary = _page_summary(slug, meta, body)
        if tag_filter and tag_filter not in [t.lower() for t in summary["tags"]]:
            continue
        out.append(summary)
    return {
        "ok": True,
        "workspace": str(workspace),
        "count": len(out),
        "pages": out,
    }


def _snippet(body: str, needle: str, width: int = 160) -> str:
    idx = body.lower().find(needle.lower())
    if idx < 0:
        return body[:width].strip()
    start = max(0, idx - width // 2)
    end = min(len(body), idx + len(needle) + width // 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return f"{prefix}{body[start:end].strip()}{suffix}"


async def wiki_search_impl(
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    workspace_raw: str = "",
) -> dict[str, Any]:
    """Full-text search across page titles, tags, aliases, and bodies."""
    if not query or not query.strip():
        return _error("A non-empty search query is required.")
    if limit <= 0:
        limit = DEFAULT_SEARCH_LIMIT
    workspace = _bg.resolve_workspace(workspace_raw)
    needle = query.strip().lower()
    matches: list[dict[str, Any]] = []
    for slug, meta, body in await _iter_pages(workspace):
        title = str(meta.get("title", slug))
        tags = _normalize_tags(meta.get("tags"))
        aliases = _normalize_tags(meta.get("aliases"))
        # Weight title/tag hits above body hits so the best pages sort first.
        score = 0
        if needle in title.lower():
            score += 10
        if any(needle in t.lower() for t in tags + aliases):
            score += 5
        body_hits = body.lower().count(needle)
        score += body_hits
        if score == 0:
            continue
        matches.append(
            {
                "slug": slug,
                "title": title,
                "tags": tags,
                "score": score,
                "scope": str(meta.get("_scope", "personal")),
                "snippet": _snippet(body, query),
            }
        )
    matches.sort(key=lambda m: (-m["score"], m["slug"]))
    return {
        "ok": True,
        "workspace": str(workspace),
        "query": query,
        "count": len(matches),
        "results": matches[:limit],
    }


async def wiki_links_impl(title_or_slug: str, *, workspace_raw: str = "") -> dict[str, Any]:
    """Report a page's outgoing links, back-links, and broken (missing-target) links."""
    if not title_or_slug or not title_or_slug.strip():
        return _error("A page title or slug is required.")
    workspace = _bg.resolve_workspace(workspace_raw)
    target = slugify(title_or_slug)
    pages = await _iter_pages(workspace)
    known = {slug for slug, _, _ in pages}
    if target not in known:
        return _error(f"No wiki page named {target!r}.", slug=target)

    outgoing: list[str] = []
    backlinks: list[str] = []
    for slug, _, body in pages:
        links = extract_links(body)
        if slug == target:
            outgoing = links
        elif target in links:
            backlinks.append(slug)
    broken = [link for link in outgoing if link not in known]
    return {
        "ok": True,
        "workspace": str(workspace),
        "slug": target,
        "outgoing": outgoing,
        "backlinks": sorted(backlinks),
        "broken": broken,
    }


async def wiki_delete_impl(title_or_slug: str, *, workspace_raw: str = "") -> dict[str, Any]:
    """Delete a page. Reports which other pages had links pointing at it (now broken)."""
    if not title_or_slug or not title_or_slug.strip():
        return _error("A page title or slug is required.")
    workspace = _bg.resolve_workspace(workspace_raw)
    slug = slugify(title_or_slug)
    found = await _locate_page(workspace, slug)
    if found is None:
        return _error(f"No wiki page named {slug!r}.", slug=slug)
    path, page_scope = found

    orphaned: list[str] = []
    for other_slug, _, body in await _iter_pages(workspace):
        if other_slug != slug and slug in extract_links(body):
            orphaned.append(other_slug)
    try:
        await path.unlink()
    except OSError as exc:
        return _error(f"Failed to delete page: {exc}", slug=slug)
    return {
        "ok": True,
        "workspace": str(workspace),
        "slug": slug,
        # 删掉的是共享页时明确说出来:那一下影响的是所有人,而不只是调用者自己。
        "scope": page_scope,
        "deleted": True,
        "orphaned_backlinks": sorted(orphaned),
    }
