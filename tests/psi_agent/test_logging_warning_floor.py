"""定向 DEBUG 的文件 sink 不得吃掉**别的**模块的 WARNING/ERROR。

起因是一次排查翻案: 生产 14.5 万行 DEBUG 日志里 ``FeishuManager`` **零命中**, 于是
「adopt 了哪个 session、workspace 对不对」在线上完全查不到。原因不是那两行没打, 而是
文件 sink 的 filter 写成 ``{"": False, **白名单}`` —— ``""`` 是 loguru 的**根**规则,
``False`` 对未列模块**整段关掉**, 不是只关 DEBUG。实测(改动前):

    白名单 = psi_agent.ai.server
    psi_agent.gateway.feishu._feishu_manager 的 WARNING → 文件里没有

所以「新增一条 WARNING 就能在线上看见」这个前提在改动前是**假的**: 一旦运维开了定向
DEBUG 去查别的事, 这个文件就是那批日志的全部, 而它对未列模块一个字都不收。

改法: 根规则从 ``False`` 改成 ``"WARNING"`` —— 未列模块的 DEBUG/INFO 照旧不进(量的
约束不变, 这是 ``PSI_DEBUG_MODULES`` 存在的理由), 但 WARNING 起的必须留。判据就是下面
两条: 一条钉「未列模块的 WARNING 进得去」, 一条钉「未列模块的 DEBUG/INFO 仍进不去」。
两条互为对照, 少任何一条都会让「把根规则改成 'DEBUG'」这种过度放开的写法蒙混过关。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from loguru import logger

import psi_agent._logging as _logging
from psi_agent._logging import setup_logging

# 一个**不在**白名单里的模块名。刻意用真实存在的那个 —— 它正是线上查不到的受害者。
_UNLISTED = "psi_agent.gateway.feishu._feishu_manager"
_LISTED = "psi_agent.ai.server"


@pytest.fixture(autouse=True)
def _reset_logging_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("PSI_DEBUG_MODULES", raising=False)
    monkeypatch.delenv("PSI_DEBUG_LOG_PATH", raising=False)
    monkeypatch.delenv("PSI_APPDATA", raising=False)
    _logging._handler_id = None
    _logging._file_handler_id = None
    yield
    logger.remove()
    _logging._handler_id = None
    _logging._file_handler_id = None


def _read_debug_log(root: Path) -> str:
    """拼接 *root* 下的按 PID 命名的 debug 日志(名字含运行期 PID, 只能 glob)。"""
    files = sorted((root / "logs").glob("psi-debug-*.log"))
    assert files, f"no debug log written under {root / 'logs'}"
    return "\n".join(f.read_text(encoding="utf-8") for f in files)


def _emit_as(module: str) -> Any:
    """拿一个把 ``record["name"]`` 改写成 *module* 的 logger。

    与 ``test_logging.py`` 同一手法: 一个测试模块借此冒充任意模块, 免得为了测 filter
    去真的 import 那些模块(它们各自带一堆依赖与副作用)。
    """
    return logger.patch(lambda record: record.update(name=module))


def test_unlisted_module_warning_reaches_the_debug_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未列模块的 WARNING/ERROR **必须**落进文件 sink。

    这是「新增的 WARNING 在线上真的看得见」的唯一判据。根规则是 ``False`` 时本条红。
    """
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.setenv("PSI_DEBUG_MODULES", _LISTED)

    setup_logging(verbose=False)
    assert _logging._file_handler_id is not None
    _emit_as(_UNLISTED).warning("unlisted-module-warning")
    _emit_as(_UNLISTED).error("unlisted-module-error")
    # ``enqueue=True`` 把记录交给 worker; remove() 会 flush 并 join。
    logger.remove()

    text = _read_debug_log(tmp_path)
    assert "unlisted-module-warning" in text, "未列模块的 WARNING 被 filter 吃掉了 —— 线上仍然查不到"
    assert "unlisted-module-error" in text, "未列模块的 ERROR 被 filter 吃掉了"


def test_unlisted_module_debug_and_info_still_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未列模块的 DEBUG/INFO 仍然进不去 —— 放开 WARNING 不等于放开全部。

    与上一条互为对照: 只有上一条时, 把根规则直接改成 ``"DEBUG"`` 也能绿, 而那会让文件
    sink 收下全进程的 DEBUG, 正是 ``PSI_DEBUG_MODULES`` 白名单要避免的量。
    """
    monkeypatch.setenv("PSI_APPDATA", str(tmp_path))
    monkeypatch.setenv("PSI_DEBUG_MODULES", _LISTED)

    setup_logging(verbose=False)
    _emit_as(_UNLISTED).debug("unlisted-module-debug")
    _emit_as(_UNLISTED).info("unlisted-module-info")
    # 白名单模块照旧收 DEBUG —— 顺带钉住放开根规则没把白名单本身弄坏。
    _emit_as(_LISTED).debug("listed-module-debug")
    logger.remove()

    text = _read_debug_log(tmp_path)
    assert "unlisted-module-debug" not in text, "未列模块的 DEBUG 混进来了, 白名单形同废除"
    assert "unlisted-module-info" not in text, "未列模块的 INFO 混进来了"
    assert "listed-module-debug" in text, "白名单模块的 DEBUG 反而没了"
