"""UIPrefs —— SPA 的 UI 偏好 (问卷是否已填、界面语言等) 的本机落盘。

**为什么不放 ``localStorage``。** 安装包拉起 Gateway 时不带 ``--listen``
(见 ``inno-setup/haitun.c``), 于是 ``__init__`` 回落 ``_random_port()`` ——
每次启动都是新端口。浏览器的 ``localStorage`` 按 origin (scheme+host+**port**)
分桶, 所以上次运行写的标记下次读不到: 用户填过问卷、关掉弹窗, 重启客户端
5 分钟后照样再弹, 历史标记还会在一堆孤儿 origin 下越攒越多。

**为什么不放 ``state/latest.json``。** 那份快照是 5 个固定 key 的 manager
状态 (``ais/routers/sessions/titles/summaries``), 且每次启动另写一份带时间戳
的副本。UI 偏好既不是 manager 快照, 也不值得留版本历史。

**为什么不像 ``_auth_store`` 那样加密。** 这里存的是「问卷填过了」这类布尔,
不是凭证; 上钥匙串只会把平台相关代码扩散到第二个文件。

语言与标记按**机器**存, 不按登录用户: 认证在本项目是旁挂且可整套关掉的
(``PSI_AUTH_ENDPOINT=""``), 绑 user_id 会让纯本地模式下的问卷标记无处落脚。
代价是同机换账号登录不会重新弹问卷 —— 对「别再骚扰这台机器的使用者」这个
诉求来说是对的取舍。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
from loguru import logger

from psi_agent._appdata import appdata_ui_prefs_path, resolve_appdata_root
from psi_agent.i18n import DEFAULT_LANGUAGE, normalize_language

# 已知标记名。收敛成白名单, 避免 SPA 拿这个接口当通用 KV 乱写。
_SURVEY_DONE = "survey_done"
_LANGUAGE = "language"
_LANGUAGE_UPDATED_AT = "language_updated_at"
_INSTALL_LANGUAGE_SEEN = "install_language_seen"


@dataclass
class UIPrefs:
    """Persist one-shot SPA UI flags as a flat JSON object under AppData."""

    _path: anyio.Path = field(default_factory=lambda: anyio.Path("ui-prefs.json"))

    @classmethod
    async def from_appdata(cls, appdata_root: str = "") -> UIPrefs:
        """Build a prefs store rooted at *appdata_root* (empty → resolve)."""
        root = appdata_root.strip() or await resolve_appdata_root()
        return cls(_path=appdata_ui_prefs_path(root))

    async def _read(self) -> dict[str, Any]:
        """Whole-file read. Any failure degrades to empty prefs, never raises.

        A corrupt or unreadable prefs file must not break the SPA: worst case the
        user sees the survey popup one more time.
        """
        try:
            raw = await self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.debug(f"UI prefs {self._path} not found, starting fresh")
            return {}
        except OSError as e:
            logger.warning(f"Failed to read UI prefs {self._path}: {e!r}")
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"UI prefs {self._path} is corrupt, starting fresh")
            return {}
        if not isinstance(data, dict):
            logger.warning(f"UI prefs {self._path} is not an object, starting fresh")
            return {}
        return data

    async def _write(self, data: dict[str, Any]) -> None:
        await self._path.parent.mkdir(parents=True, exist_ok=True)
        await self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def survey_done(self) -> bool:
        """True once the user has dismissed the survey popup on this machine."""
        return (await self._read()).get(_SURVEY_DONE) is True

    async def set_survey_done(self, done: bool = True) -> None:
        """Persist the survey flag. Idempotent — repeated calls rewrite the same value."""
        data = await self._read()
        data[_SURVEY_DONE] = bool(done)
        await self._write(data)
        logger.info(f"UI prefs: {_SURVEY_DONE}={done} → {self._path}")

    async def language(self) -> str:
        """User-chosen UI language, or ``""`` when the user never chose one.

        Returning empty (rather than a default) lets callers fall back to the
        installer-written language, CLI flag, or ``zh-CN`` in priority order.
        """
        raw = (await self._read()).get(_LANGUAGE)
        if not isinstance(raw, str) or not raw.strip():
            return ""
        return normalize_language(raw)

    async def set_language(self, language: str) -> str:
        """Persist the UI language with a timestamp. Returns the stored code."""
        normalized = normalize_language(language)
        data = await self._read()
        data[_LANGUAGE] = normalized
        data[_LANGUAGE_UPDATED_AT] = datetime.now(UTC).isoformat()
        await self._write(data)
        logger.info(f"UI prefs: {_LANGUAGE}={normalized} → {self._path}")
        return normalized or DEFAULT_LANGUAGE

    async def language_updated_at(self) -> str:
        """ISO-8601 timestamp of the last in-app language change (empty if never)."""
        raw = (await self._read()).get(_LANGUAGE_UPDATED_AT)
        return raw if isinstance(raw, str) else ""

    async def install_language_seen(self) -> str:
        """Last installer language this Gateway has already applied (empty if never)."""
        raw = (await self._read()).get(_INSTALL_LANGUAGE_SEEN)
        return raw if isinstance(raw, str) else ""

    async def set_install_language_seen(self, language: str) -> None:
        """Record the installer language currently applied, so a same-language
        update does not override the user's in-app choice."""
        data = await self._read()
        data[_INSTALL_LANGUAGE_SEEN] = normalize_language(language)
        await self._write(data)
        logger.info(f"UI prefs: {_INSTALL_LANGUAGE_SEEN}={language} → {self._path}")
