"""Endpoint knowledge as data — parse the ``feishu-*`` skills' endpoint tables.

The point of moving endpoint knowledge out of Python is that a new endpoint should
cost a table row, not a tool. But a Markdown table read by the model is only a
*suggestion*: Feishu's worst failures are silent (a bare ``!A1`` range writes nothing
and still returns success, a mismatched Bitable column is dropped without error), and
no amount of prose stops a model from filling a wrong value.

So each skill carries two views of the same fact:

* the Markdown table — what the model reads to pick an endpoint;
* a fenced ``rules`` block — the same constraints as YAML, enforced here *before* the
  request goes out.

One file, two consumers. Drift between them is a documentation bug, not a silent data
loss, because the rules block is the one that executes.

Rule fields, all optional except ``endpoint``:

    endpoint      "GET /open-apis/contact/v3/users/:user_id" — matched by method+prefix
    token         tenant | user | tenant_then_user — the strategy this endpoint needs
    prefer_tool   name of a dedicated tool; set ``hard: true`` to refuse outright
    why           shown with prefer_tool, explains what hand-building gets wrong
    required      body/query field names that must be present
    fields        per-field: pattern / max / min / choices / default / requires
    pitfalls      free text surfaced on failure — never enforced, only explained
    paginate      true, or a mapping — follow ``page_token`` until ``has_more`` is false

``paginate`` is what lets a table row replace a hand-written tool. Feishu's paging
protocol is uniform (``page_token`` out, ``has_more`` + ``page_token`` back), so
18 of the 23 paging loops in ``_feishu_impl`` differ only in which key holds the
items and what page size they ask for. Both are declarable:

    paginate: {items: items, page_size: 100, max_pages: 50}

``items`` defaults to ``items`` (19 of 23 endpoints); ``tasks``,
``instance_code_list``, ``grouplist`` and ``memberlist`` are the ones that differ.
"""

from __future__ import annotations

import functools
import pathlib
import re
from typing import Any

import yaml

_RULES_BLOCK = re.compile(r"^```rules\s*$(.*?)^```\s*$", re.M | re.S)
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


class Rule:
    """One endpoint's enforceable contract, as loaded from a skill's rules block."""

    __slots__ = (
        "_segments",
        "endpoint",
        "fields",
        "method",
        "paginate",
        "pitfalls",
        "prefer_hard",
        "prefer_tool",
        "required",
        "source",
        "token",
        "uri",
        "why",
    )

    def __init__(self, raw: dict[str, Any], source: str = "") -> None:
        endpoint = str(raw.get("endpoint", "")).strip()
        self.endpoint = endpoint
        self.method, self.uri = _split_endpoint(endpoint)
        self._segments = [p for p in self.uri.split("/") if p]
        self.token = str(raw.get("token", "") or "").strip()
        tool = raw.get("prefer_tool")
        self.prefer_tool = str(tool).strip() if tool else ""
        self.prefer_hard = bool(raw.get("hard", False))
        self.why = str(raw.get("why", "") or "").strip()
        self.required = [str(x) for x in (raw.get("required") or [])]
        self.fields = dict(raw.get("fields") or {})
        pit = raw.get("pitfalls") or []
        self.pitfalls = [str(p) for p in (pit if isinstance(pit, list) else [pit])]
        self.source = source
        self.paginate = _paginate_spec(raw.get("paginate"))

    def matches(self, method: str, uri: str) -> bool:
        """Whether this rule governs ``method uri``.

        Matching is segment-wise, and by prefix rather than equality, because both
        properties are load-bearing:

        * ``:placeholder`` segments stand for whatever id the caller substituted, so
          the table can be written the way the endpoint is documented
          (``/users/:user_id``) and still match ``/users/ou_abc``;
        * a rule written for ``/values`` must also catch ``/values/Sheet1!A1``, which
          is the shape that silently drops data.

        Comparing whole segments — rather than raw string prefixes — is what keeps
        ``/x/batch`` from also claiming ``/x/batch_v2``.
        """
        if self.method and self.method != method:
            return False
        if not self._segments:
            return False
        parts = [p for p in uri.split("/") if p]
        if len(parts) < len(self._segments):
            return False
        return all(mine.startswith(":") or mine == theirs for mine, theirs in zip(self._segments, parts, strict=False))

    @property
    def specificity(self) -> int:
        """How strong a claim this rule makes, so the closest one wins a tie.

        Depth first, and a literal segment outranks a placeholder at the same depth.
        """
        return sum(1 if seg.startswith(":") else 2 for seg in self._segments)

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<Rule {self.endpoint!r} from {self.source}>"


#: Guards a declarative paging loop against running forever. Feishu returns
#: ``has_more`` honestly, but a table typo (wrong ``items`` key on an endpoint that
#: keeps echoing a token) must fail loudly rather than spin. 200 pages at the usual
#: page size is far past any real roster.
_MAX_PAGES = 200


def _paginate_spec(raw: Any) -> dict[str, Any] | None:
    """Normalize the ``paginate`` field: ``true`` or a mapping → a settings dict.

    Returns None when paging is off, so the send path can test it as a plain flag.
    """
    if not raw:
        return None
    spec = raw if isinstance(raw, dict) else {}
    try:
        page_size = int(spec.get("page_size", 100))
    except TypeError, ValueError:
        page_size = 100
    try:
        max_pages = min(int(spec.get("max_pages", _MAX_PAGES)), _MAX_PAGES)
    except TypeError, ValueError:
        max_pages = _MAX_PAGES
    return {
        "items": str(spec.get("items", "items")),
        "page_size": page_size,
        "max_pages": max(1, max_pages),
        "param": str(spec.get("param", "page_size")),
    }


def _split_endpoint(endpoint: str) -> tuple[str, str]:
    """``"GET /open-apis/x"`` → ``("GET", "/open-apis/x")``; a bare path keeps method empty."""
    parts = endpoint.split(None, 1)
    if len(parts) == 2 and parts[0].upper() in _METHODS:
        return parts[0].upper(), parts[1].strip()
    return "", endpoint.strip()


def parse_rules(text: str, source: str = "") -> list[Rule]:
    """Every rule in the fenced ``rules`` blocks of one Markdown document.

    A malformed block is skipped rather than raising: a typo in one skill must not
    take down every other endpoint's validation. The block is YAML — either a list of
    rule mappings or a single mapping.
    """
    rules: list[Rule] = []
    for match in _RULES_BLOCK.finditer(text):
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            continue
        if isinstance(loaded, dict):
            loaded = [loaded]
        if not isinstance(loaded, list):
            continue
        for item in loaded:
            if isinstance(item, dict) and str(item.get("endpoint", "")).strip():
                rules.append(Rule(item, source=source))
    return rules


def load_rules(skills_dir: str | pathlib.Path) -> list[Rule]:
    """All rules from ``<skills_dir>/*/SKILL.md``, most specific URI first."""
    root = pathlib.Path(skills_dir)
    found: list[Rule] = []
    if not root.is_dir():
        return found
    for skill in sorted(root.glob("*/SKILL.md")):
        try:
            text = skill.read_text(encoding="utf-8")
        except OSError:
            continue
        if "```rules" not in text:
            continue
        found.extend(parse_rules(text, source=skill.parent.name))
    found.sort(key=lambda r: -r.specificity)
    return found


@functools.lru_cache(maxsize=8)
def _cached(skills_dir: str) -> tuple[Rule, ...]:
    return tuple(load_rules(skills_dir))


def rules_for(skills_dir: str | pathlib.Path, method: str, uri: str) -> Rule | None:
    """The most specific rule governing ``method uri``, or None."""
    for rule in _cached(str(skills_dir)):
        if rule.matches((method or "").upper(), uri or ""):
            return rule
    return None


def reset_cache() -> None:
    """Drop the parsed-rules cache — for tests and for skill hot-reload."""
    _cached.cache_clear()


def _present(name: str, body: dict[str, Any], query: dict[str, Any], paths: dict[str, Any]) -> tuple[bool, Any]:
    """Look a field up across all three argument buckets.

    A rule names a field once; whether it rides in the body, the query string, or a
    path placeholder is the endpoint's business, not the rule author's.
    """
    for bucket in (body, query, paths):
        if name in bucket:
            return True, bucket[name]
    return False, None


def _check_field(name: str, spec: Any, value: Any) -> str | None:
    """One field against its constraints; returns the violation text or None.

    Values arrive as whatever JSON produced, so numeric bounds coerce and give up
    quietly when the value isn't a number — a type mismatch is Feishu's error to
    report, with its own message. What must not pass silently is a value that *looks*
    valid and loses data.
    """
    if not isinstance(spec, dict):
        return None
    if (pattern := spec.get("pattern")) and isinstance(value, str) and not re.search(str(pattern), value):
        return spec.get("on_fail") or f"{name}={value!r} 不符合要求的格式 ({pattern})"
    if (choices := spec.get("choices")) and value not in choices:
        return spec.get("on_fail") or f"{name}={value!r} 不在允许取值 {list(choices)} 内"
    for bound, cmp, label in (("max", lambda a, b: a > b, "上限"), ("min", lambda a, b: a < b, "下限")):
        limit = spec.get(bound)
        if limit is None:
            continue
        try:
            if cmp(float(value), float(limit)):
                return spec.get("on_fail") or f"{name}={value!r} 超出{label} {limit}"
        except TypeError, ValueError:
            pass
    if (length := spec.get("max_items")) is not None and isinstance(value, (list, tuple)):
        try:
            if len(value) > int(length):
                return spec.get("on_fail") or f"{name} 有 {len(value)} 项, 超出上限 {length}"
        except TypeError, ValueError:
            pass
    return None


def validate(
    rule: Rule | None,
    body: dict[str, Any],
    query: dict[str, Any],
    paths: dict[str, Any],
) -> list[str]:
    """Every way this call violates ``rule``. Empty list means send it.

    All violations are collected rather than short-circuiting: a caller who got two
    fields wrong should learn both in one round trip, not discover the second only
    after fixing the first.
    """
    if rule is None:
        return []
    problems: list[str] = []
    for name in rule.required:
        found, _ = _present(name, body, query, paths)
        if not found:
            problems.append(f"缺少必填字段 {name}")
    for name, spec in rule.fields.items():
        found, value = _present(name, body, query, paths)
        if not found:
            continue
        if isinstance(spec, dict) and (need := spec.get("requires")):
            for other in need if isinstance(need, list) else [need]:
                if not _present(str(other), body, query, paths)[0]:
                    problems.append(f"给了 {name} 就必须同时给 {other}")
        if (violation := _check_field(name, spec, value)) is not None:
            problems.append(violation)
    return problems


def defaults_for(rule: Rule | None) -> dict[str, dict[str, Any]]:
    """Field defaults declared by ``rule``, split by which bucket they belong to.

    Only ``query`` and ``body`` get defaults; a missing path placeholder is already a
    hard error upstream and guessing one would send the request somewhere else.
    """
    out: dict[str, dict[str, Any]] = {"query": {}, "body": {}}
    if rule is None:
        return out
    for name, spec in rule.fields.items():
        if isinstance(spec, dict) and "default" in spec:
            bucket = str(spec.get("in", "query")).strip()
            out.setdefault(bucket if bucket in out else "query", {})[name] = spec["default"]
    return out
