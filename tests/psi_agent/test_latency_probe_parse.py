"""``scripts/latency-probe/parse.py`` 的判据必须匹配**真实产出的**日志行。

脚本靠 logger 名 + 消息文本匹配 (刻意不含行号, 行号一改动就腐化)。但消息文本本身
仍会腐化, 而 ``parse.py --self-check`` 用的是**手写**样例 —— 手写样例和真实输出一起
错、或样例先写对后来代码改了, 自查都照样绿。

所以这里反过来做: 驱动真实代码路径, 经真实 ``_FORMAT`` 收行, 再喂给 ``parse.py``。
它红了说明判据与代码已经脱节, 而不是样例与代码脱节。
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from aiohttp import ClientSession, ClientTimeout, web
from loguru import logger

from psi_agent._logging import _FORMAT, install_session_patcher
from psi_agent.ai.server import handle_chat_completions

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PARSE_PY = _REPO_ROOT / "scripts" / "latency-probe" / "parse.py"


def _load_parse_module() -> Any:
    """按路径加载 —— ``scripts/`` 不是包, 不在 import path 上。"""
    spec = importlib.util.spec_from_file_location("_latency_probe_parse", _PARSE_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def probe() -> Any:
    return _load_parse_module()


class _Captured:
    """按真实 ``_FORMAT`` 收行的 sink (关掉颜色, 否则满是 ANSI)。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        install_session_patcher()
        self._id = logger.add(self.lines.append, level="DEBUG", format=_FORMAT, colorize=False)

    def close(self) -> None:
        logger.remove(self._id)

    def stripped(self) -> list[str]:
        return [line.rstrip("\n") for line in self.lines]


def test_self_check_passes(probe: Any) -> None:
    """先确认脚本自带的自查是绿的 —— 下面几条是它的补充, 不是替代。"""
    assert probe._self_check() == 0


def test_every_pattern_has_a_sample(probe: Any) -> None:
    """自查样例必须覆盖全部判据, 否则新加的判据永远没人验。"""
    # ``compaction`` 不限模块、靠文本前缀, 由 ``_self_check`` 之外的用例覆盖。
    covered = {kind for kind, _module, _pattern in probe._PATTERNS}
    missing = covered - set(probe._SAMPLES)
    assert not missing, f"这些判据没有自查样例: {sorted(missing)}"


@pytest.mark.anyio
async def test_real_ai_turn_markers_are_classified(probe: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """跑真实 handler, 让它自己吐标记, 再看 parse.py 认不认。

    手写样例认得而真实输出不认, 是这条要防的事故 —— 上一轮排查就是这么被脚本骗的。
    """

    class _Chunk:
        def model_dump_json(self) -> str:
            return '{"id":"x","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":"stop"}]}'

    class _Stream:
        def __aiter__(self) -> Any:
            self._done = False
            return self

        async def __anext__(self) -> Any:
            if getattr(self, "_done", False):
                raise StopAsyncIteration
            self._done = True
            return _Chunk()

        async def aclose(self) -> None:
            return None

    async def fake_acompletion(**kwargs: Any) -> Any:
        return _Stream()

    monkeypatch.setattr("psi_agent.ai.server.acompletion", fake_acompletion)

    app = web.Application()
    app["provider"] = "openai"
    app["model"] = "test"
    app["api_key"] = "k"
    app["base_url"] = "http://upstream"
    app.router.add_post("/chat/completions", handle_chat_completions)
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    await web.SockSite(runner, sock).start()
    await anyio.sleep(0.1)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"

    captured = _Captured()
    try:
        async with (
            ClientSession(timeout=ClientTimeout(total=5)) as http,
            http.post(
                f"{base}/chat/completions",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                    "routing": {"session_id": "feishu-ou_probe"},
                },
            ) as response,
        ):
            assert response.status == 200
            async for _ in response.content:
                pass
    finally:
        captured.close()
        await runner.cleanup()

    records = list(probe.parse_stream(captured.stripped()))
    by_kind = {r.kind: r for r in records if r.kind is not None}

    assert "ai_open" in by_kind, f"真实 open 行没被认出: {[r.raw for r in records][:5]}"
    assert "ai_close" in by_kind, f"真实 close 行没被认出: {[r.raw for r in records][:5]}"
    # 会话 id 列要能读出来 —— 认出行但读不出人, 等于回合还是归不到人身上。
    assert by_kind["ai_open"].session == "feishu-ou_probe"
    # elapsed_ms 要能读出来, 否则耗时静默变 0 (上一轮就吃过这个亏)。
    assert probe.elapsed_ms(by_kind["ai_close"]) is not None
    assert probe.outcome(by_kind["ai_close"]) == "ok"
