"""Describe an image file via vision API (image-to-text)."""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _runtime_paths as _paths
import _vision as _v


async def describe_image(image_path: str, question: str = "") -> str:
    """Return a text description or answer about an image file.

    Pass an **absolute image path** and an optional **question** (what to extract).
    Do **not** use ``read`` on PNG/JPEG for vision — this tool reads bytes and
    calls the vision API inside the helper.

    Calls **MiniMax-M3** vision via ``POST /v1/chat/completions`` (BYOK).
    Returns ``ok: false`` with a clear ``message`` when credentials are unset
    or the upstream API fails.

    Env: workspace ``.env.multimodal`` (auto-loaded).
    ``MINIMAX_API_KEY`` (or ``VISION_API_KEY``), ``MINIMAX_API_HOST`` (default CN host),
    ``VISION_MODEL`` (default ``MiniMax-M3``).

    Args:
        image_path: Absolute path to png/jpg/jpeg/gif/webp (max 20 MB).
        question: What to ask about the image; empty = default describe prompt.

    Returns:
        JSON with ok, text, backend, message, image_path.
    """
    # Does its own IO outside _runtime_paths, so read authority is checked explicitly
    # (another user's image must not be fed to the vision model).
    try:
        await _paths.resolve_user_path(image_path)
    except _paths.PrivateSpaceDeniedError as e:
        return _v.dumps_result(_v.VisionResult(ok=False, text="", backend="", message=str(e), image_path=image_path))
    result = await _v.describe_image_impl(image_path=image_path, question=question)
    return _v.dumps_result(result)
