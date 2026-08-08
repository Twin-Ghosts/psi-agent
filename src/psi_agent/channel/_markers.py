"""File-transfer marker protocol between Channel and Session.

Pure (transport-free) encode/decode for the ``[RECV:/path]`` (input) and
``[SEND:/path]`` (output) markers exchanged over the message content. Kept
separate from ``ChannelCore`` so the wire protocol has a single authoritative
definition and can be unit-tested without any HTTP/SSE machinery.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from loguru import logger

from psi_agent.channel._types import FileChunk, InputChunk, TextChunk

RECV_MARKER = "[RECV:{path}]"
# Matches the space-padded variant ``[ SEND:path ]`` some models emit, and an
# empty ``[SEND:]`` -- the latter is filtered by ``iter_send_paths`` rather than
# by the pattern, so both the Channel decoder and the Gateway projection share
# one rule instead of each encoding it in its own regex.
SEND_RE = re.compile(r"\[\s*SEND\s*:\s*([^\]]*?)\s*\]", re.IGNORECASE)


def iter_send_paths(text: str) -> Iterator[tuple[str, int]]:
    """Yield ``(path, match_end)`` for each ``[SEND:…]`` carrying a real path.

    Empty / whitespace-only paths are skipped: a bare ``[SEND:]`` is a model
    slip, not a transfer request.  Forwarding one would make the Channel attempt
    an upload with an empty source path (neither ``_send_file`` implementation
    guards against it), and would make the Gateway projection emit a blank entry.

    ``match_end`` is the offset just past the marker, so a streaming caller can
    advance its scan pointer without re-deriving the match.
    """
    for match in SEND_RE.finditer(text):
        path = match.group(1).strip()
        if path:
            yield path, match.end()


def encode_input(chunks: list[InputChunk]) -> str:
    """Encode input chunks into a single user-message string.

    ``FileChunk`` becomes a ``[RECV:/path]`` marker (the Session reads the file);
    ``TextChunk`` contributes its text verbatim. Other chunk kinds are ignored.
    """
    parts: list[str] = []
    for chunk in chunks:
        if isinstance(chunk, FileChunk):
            marker = RECV_MARKER.format(path=chunk.path)
            logger.debug(f"FileChunk → {marker}")
            parts.append(marker)
        elif isinstance(chunk, TextChunk):
            parts.append(chunk.text)
    return "\n".join(parts)


class SendMarkerScanner:
    """Incrementally scans streamed output content for ``[SEND:/path]`` markers.

    Stateful across ``feed()`` calls so a marker split over multiple SSE deltas
    is still detected; each distinct path yields a ``FileChunk`` only once.
    """

    def __init__(self) -> None:
        self._full = ""
        self._scan_ptr = 0
        self._emitted: set[str] = set()

    def feed(self, text: str) -> list[FileChunk]:
        """Append a new content fragment, return newly-detected ``FileChunk``s."""
        out: list[FileChunk] = []
        self._full += text
        base = self._scan_ptr
        new = self._full[base:]
        for path, match_end in iter_send_paths(new):
            if path not in self._emitted:
                logger.debug(f"[SEND] detected → FileChunk({path})")
                out.append(FileChunk(path))
                self._emitted.add(path)
            self._scan_ptr = base + match_end
        return out
