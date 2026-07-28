"""Feishu/Lark user authorization (OAuth authorization-code flow) for user_access_token.

Some Feishu APIs (e.g. document search) act on behalf of a USER and require a
user_access_token, which the bot's app credentials can't provide. These tools run
the authorization-code flow (China/feishu.cn).

The happy path is two steps and asks the user for **no copy-pasting**:
``feishu_auth_start`` returns a browser URL to approve, and — when an automatic
callback channel is available (``auto_receive=True``) — ``feishu_auth_wait``
receives the authorization code by itself and finishes the exchange. The code
comes back either through the Gateway's ``/oauth/callback`` relay (works when the
user approves on a phone) or through a one-shot ``127.0.0.1`` listener (same
machine only); see ``_oauth_receiver``. Only when neither channel is available
does the old manual path apply: the user copies ``code=...`` out of the browser
address bar and hands it to ``feishu_auth_complete``.

Tokens are cached in ``<workspace>/.psi/feishu/uat.json`` (plaintext, local dev
use; auto-refreshed later) and keyed per user via ``user_key`` (the sender's
open_id), so multiple people can authorize independently without overwriting each
other; empty ``user_key`` shares a single ``default`` slot.

Requires ``PSI_FEISHU_APP_ID`` / ``PSI_FEISHU_APP_SECRET`` and a redirect URI
registered in the app's security settings. The flow uses PKCE (S256). The OAuth
scopes are fixed to a read-only docs/drive set plus docx/wiki write inside the
tool — callers (and the LLM) cannot choose them, since an invalid scope makes
Feishu reject the authorize page (error 20043). The app must have those scopes
enabled in its Feishu console permissions.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import _feishu_impl as _f


async def feishu_auth_start(user_key: str = "") -> str:
    """Begin Feishu user authorization: return a browser URL for the user to approve.

    Send ``authorize_url`` to the user and have them approve. If the result says
    ``auto_receive=True``, do NOT ask them for any code — call ``feishu_auth_wait``
    with the same ``user_key`` and the authorization completes on its own. Only if
    ``auto_receive=False`` fall back to the manual path (user copies ``code=...``
    from the browser address bar into ``feishu_auth_complete``).

    The OAuth scopes are fixed by the tool; do NOT try to choose or pass scopes —
    an invalid scope makes Feishu reject the whole authorize page (error 20043).

    Args:
        user_key: The message sender's open_id (from the injected ``<feishu_context>``
            ``sender_open_id``), so each user's authorization is isolated. Pass the
            same value to ``feishu_auth_wait`` / ``feishu_auth_complete`` /
            ``feishu_docs_search``. Empty shares a single ``default`` slot
            (single-user / local dev).
    """
    return _f.dumps_result(await _f.auth_start_impl("", user_key))


async def feishu_auth_wait(user_key: str = "", timeout_seconds: int = 180) -> str:
    """Wait for the authorization code to arrive by itself, then finish authorizing.

    Call this right after sending the user ``authorize_url`` from
    ``feishu_auth_start`` (when it reported ``auto_receive=True``). It blocks until
    the user approves in the browser, receives the code through the callback
    channel, exchanges it for a token, and caches it — the user copies nothing.

    On ``timed_out=True`` you may simply call this again to keep waiting. On
    ``manual_required=True`` the environment has no automatic channel: fall back to
    ``feishu_auth_complete`` with a code the user copies from the address bar.

    Args:
        user_key: The same open_id passed to ``feishu_auth_start``.
        timeout_seconds: How long to wait for the user to approve (10-600, default 180).
    """
    return _f.dumps_result(await _f.auth_wait_impl(user_key, timeout_seconds))


async def feishu_auth_complete(code: str, user_key: str = "") -> str:
    """Finish Feishu user authorization manually: exchange the code for a token.

    Only needed when automatic receiving is unavailable (``auto_receive=False`` from
    ``feishu_auth_start``, or ``manual_required=True`` from ``feishu_auth_wait``).
    Call it with the ``code`` the user copied from the redirect.

    Args:
        code: The authorization code from the redirect URL, or the full redirect URL.
        user_key: The same open_id passed to ``feishu_auth_start`` — the token is
            cached under this key. Empty shares the ``default`` slot.
    """
    return _f.dumps_result(await _f.auth_complete_impl(code, user_key))
