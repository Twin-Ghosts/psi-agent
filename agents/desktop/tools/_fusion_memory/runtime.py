from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import anyio
from anyio import to_thread

from psi_agent._appdata import resolve_appdata_root
from psi_agent.session.runtime_context import get_agent, get_workspace

from .embedding import ModelConfig, embed_texts, extract_memory_items, load_model_config
from .ingest import discover_histories, ingest_histories, parse_completed_turns, workspace_scope
from .journal import EvidenceSpan, JsonlJournal
from .retrieval import AnswerContext, EvidenceHit, build_answer_context, render_first_recall, search_evidence
from .store import IngestCheckpoint, MemoryItem, MemoryStore

logger = logging.getLogger(__name__)

_EXTRACTION_TURN_LIMIT = 8


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    enabled: bool
    workspace: str
    root: Path
    journal_path: Path
    database_path: Path
    journal_fsync: bool
    models: ModelConfig

    @classmethod
    def from_env(cls, workspace_raw: str = "", env: dict[str, str] | None = None) -> RuntimeSettings:
        values = env if env is not None else os.environ
        enabled = _parse_bool(values.get("FUSION_MEMORY_ENABLE_JOURNAL"), default=True)
        raw_workspace = workspace_raw or get_workspace() or get_agent()
        if not raw_workspace:
            raise ValueError("workspace is unavailable")
        workspace = workspace_scope(raw_workspace)
        root = Path(workspace.normalized) / ".fusion-memory"
        raw_override = values.get("FUSION_MEMORY_JOURNAL_PATH", "").strip()
        override = Path(raw_override).expanduser() if raw_override else None
        if override is None:
            journal_path = root / "evidence.jsonl"
        elif override.is_absolute():
            journal_path = override
        else:
            journal_path = root / override
        return cls(
            enabled=enabled,
            workspace=workspace.normalized,
            root=root,
            journal_path=journal_path,
            database_path=root / "memory.sqlite3",
            journal_fsync=_parse_bool(values.get("FUSION_MEMORY_JOURNAL_FSYNC"), default=True),
            models=load_model_config(values),
        )


class MemoryRuntime:
    def __init__(self, settings: RuntimeSettings, store: MemoryStore | None) -> None:
        self.settings = settings
        self.workspace_id = workspace_scope(settings.workspace).workspace_id
        self.store = store
        self.models = settings.models
        self.lock = anyio.Lock()
        self.consumed_sessions: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self.settings.enabled and self.store is not None

    def _warn(self, operation: str, exc: Exception) -> None:
        logger.warning("Fusion Memory %s degraded after %s", operation, type(exc).__name__)

    async def ingest_current_session(self, session_id: str) -> dict[str, object]:
        if not self.enabled:
            return {"ok": False, "disabled": True}
        store = self.store
        assert store is not None
        async with self.lock:
            try:
                appdata = Path(await resolve_appdata_root())
                scope = workspace_scope(self.settings.workspace)
                sources = await discover_histories(scope, session_id, appdata)
                report = await to_thread.run_sync(ingest_histories, store, scope, sources)
                source_batches: list[tuple[IngestCheckpoint, list[list[EvidenceSpan]], list[list[EvidenceSpan]]]] = []
                for source in sources:
                    checkpoint = await to_thread.run_sync(
                        store.read_checkpoint, self.workspace_id, str(source.path.resolve())
                    )
                    if checkpoint is None:
                        continue
                    all_spans_by_turn: dict[str, list[EvidenceSpan]] = {}
                    for span in await to_thread.run_sync(parse_completed_turns, scope, source):
                        all_spans_by_turn.setdefault(span.turn_id, []).append(span)
                    missing_card_ids = await to_thread.run_sync(
                        store.missing_turn_cards, self.workspace_id, all_spans_by_turn
                    )
                    card_batch = [all_spans_by_turn[turn_id] for turn_id in missing_card_ids[:_EXTRACTION_TURN_LIMIT]]
                    extraction_spans_by_turn: dict[str, list[EvidenceSpan]] = {}
                    for span in await to_thread.run_sync(
                        parse_completed_turns, scope, source, checkpoint.extraction_line + 1
                    ):
                        extraction_spans_by_turn.setdefault(span.turn_id, []).append(span)
                    extraction_batch = list(extraction_spans_by_turn.values())[:_EXTRACTION_TURN_LIMIT]
                    if extraction_batch or card_batch:
                        source_batches.append((checkpoint, extraction_batch, card_batch))
                for checkpoint, extraction_batch, card_batch in source_batches:
                    extraction_line = checkpoint.extraction_line
                    if self.models.llm is not None:
                        for spans in extraction_batch:
                            try:
                                drafts = await extract_memory_items(self.models, spans)
                                items = [
                                    MemoryItem(
                                        item_id=hashlib.sha256(
                                            f"{draft.kind}|{'|'.join(draft.source_span_ids)}|{draft.text}".encode()
                                        ).hexdigest(),
                                        workspace_id=self.workspace_id,
                                        kind=draft.kind,
                                        text=draft.text,
                                        confidence=draft.confidence,
                                        salience=draft.salience,
                                        source_span_ids=draft.source_span_ids,
                                        model=self.models.llm.model,
                                    )
                                    for draft in drafts
                                ]
                                if items:
                                    await to_thread.run_sync(store.upsert_memory_items, self.workspace_id, items)
                            except Exception as exc:
                                self._warn("extraction", exc)
                                break
                            assistants = [span for span in spans if span.speaker == "assistant"]
                            if not assistants:
                                break
                            extraction_line = max(span.line_no for span in assistants)
                    if extraction_line > checkpoint.extraction_line:
                        await to_thread.run_sync(
                            store.write_checkpoint,
                            replace(checkpoint, extraction_line=extraction_line, updated_at=""),
                        )
                    for raw_spans in card_batch:
                        spans = sorted(raw_spans, key=lambda span: span.line_no)
                        users = [span for span in spans if span.speaker == "user"]
                        assistants = [span for span in spans if span.speaker == "assistant"]
                        if not users or not assistants:
                            break
                        try:
                            await to_thread.run_sync(
                                store.upsert_turn_card,
                                self.workspace_id,
                                spans[0].turn_id,
                                users[0].content,
                                assistants[-1].content,
                                [span.span_id for span in spans],
                            )
                        except Exception as exc:
                            self._warn("card", exc)
                            break
                if self.models.embedding.api_key:
                    try:
                        pending = await to_thread.run_sync(store.pending_embeddings, self.workspace_id, 32)
                        for offset in range(0, len(pending), self.models.embedding.batch_size):
                            batch = pending[offset : offset + self.models.embedding.batch_size]
                            vectors = await embed_texts(self.models, [item[2] for item in batch])
                            mapping = {(item[0], item[1]): vector for item, vector in zip(batch, vectors, strict=True)}
                            await to_thread.run_sync(
                                store.write_embeddings,
                                self.workspace_id,
                                self.models.embedding.model,
                                mapping,
                            )
                    except Exception as exc:
                        self._warn("embedding", exc)
                return {"ok": True, **asdict(report)}
            except Exception as exc:
                self._warn("ingest", exc)
                return {"ok": False, "error": type(exc).__name__}

    async def search(self, query: str, limit: int = 8) -> list[EvidenceHit]:
        if not self.enabled:
            return []
        store = self.store
        assert store is not None
        async with self.lock:
            try:
                return await search_evidence(store, self.models, query, self.workspace_id, limit)
            except Exception as exc:
                self._warn("search", exc)
                return []

    async def answer_context(self, query: str, limit: int = 12, max_chars: int = 6000) -> AnswerContext:
        if not self.enabled:
            return AnswerContext(query, (), "")
        store = self.store
        assert store is not None
        async with self.lock:
            try:
                return await build_answer_context(store, self.models, query, self.workspace_id, limit, max_chars)
            except Exception as exc:
                self._warn("answer_context", exc)
                return AnswerContext(query, (), "")

    async def promote(self, source_span_ids: list[str], kind: str, salience: float) -> MemoryItem | None:
        if not self.enabled:
            return None
        store = self.store
        assert store is not None
        async with self.lock:
            try:
                return await to_thread.run_sync(store.promote, self.workspace_id, source_span_ids, kind, salience)
            except Exception as exc:
                self._warn("promote", exc)
                return None

    async def first_turn_recall(self, session_id: str, query: str, limit: int = 8) -> str:
        if not self.enabled or not query.strip():
            return ""
        async with self.lock:
            if session_id in self.consumed_sessions:
                return ""
            self.consumed_sessions.add(session_id)
        await self.ingest_current_session(session_id)
        hits = await self.search(query, limit)
        return render_first_recall(hits)

    async def close(self) -> None:
        if self.store is not None:
            await to_thread.run_sync(self.store.close)


_cache_lock = threading.RLock()
_runtimes: dict[str, MemoryRuntime] = {}


async def get_runtime(workspace_raw: str = "") -> MemoryRuntime:
    settings = RuntimeSettings.from_env(workspace_raw)
    return await to_thread.run_sync(_get_or_create_runtime, settings)


def _get_or_create_runtime(settings: RuntimeSettings) -> MemoryRuntime:
    with _cache_lock:
        existing = _runtimes.get(settings.workspace)
        if existing is not None:
            return existing
        runtime = _create_runtime(settings)
        _runtimes[settings.workspace] = runtime
        return runtime


def _create_runtime(settings: RuntimeSettings) -> MemoryRuntime:
    if not settings.enabled:
        return MemoryRuntime(settings, None)
    settings.root.mkdir(parents=True, exist_ok=True)
    gitignore = settings.root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    journal = JsonlJournal(settings.journal_path, fsync=settings.journal_fsync)
    store = MemoryStore(settings.database_path, journal, workspace_scope(settings.workspace).workspace_id).open()
    return MemoryRuntime(settings, store)


async def reset_runtime_cache_for_tests() -> None:
    with _cache_lock:
        values = list(_runtimes.values())
        _runtimes.clear()
    for runtime in values:
        await runtime.close()
