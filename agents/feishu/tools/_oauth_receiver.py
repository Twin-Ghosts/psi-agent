"""OAuth 回调自动接收 —— 让授权码自己回来, 免用户手工复制。

授权码流程的痛点不在「点同意」, 而在**同意之后**: 第三方只把 ``code`` 拼在
``redirect_uri`` 上跳一次浏览器, 若没人监听那个地址, 用户就得自己看地址栏、把 code
粘回给 agent。本模块提供两条自动接收通道, 由 :func:`plan_receiver` 按环境自动选:

- ``gateway``: 回调打到 Gateway 的 ``/oauth/callback`` (见 ``psi_agent.gateway._oauth_manager``),
  工具侧用同一个 ``state`` 去 ``/oauth/code`` 取件。**浏览器和 agent 不必同机** ——
  手机上点授权也能自动回流, 是飞书多用户部署唯一可行的一条。需要一个用户浏览器可达
  的回调基址 (``PSI_OAUTH_CALLBACK_BASE``, 公网域名或内网地址)。
- ``loopback``: 在 ``127.0.0.1`` 上临时起一个一次性 HTTP 监听 (RFC 8252 的标准做法,
  gh / gcloud / aws sso 同款)。只在**浏览器和 agent 同机**时成立, 适合本机开发。

两条都不可用时回落到原来的手工贴码 —— 行为不变, 只是不再是唯一选择。

无论走哪条, ``redirect_uri`` 都必须先登记到应用后台的重定向 URL 列表, 否则第三方
在跳转前就会拒绝。
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import anyio
import anyio.abc

# 回调基址: 用户浏览器能打开的 Gateway 地址 (如 https://haitun.example.com)。
_CALLBACK_BASE_ENV = "PSI_OAUTH_CALLBACK_BASE"
# 本机回环监听端口。必须固定 —— redirect_uri 要能提前登记到应用后台。
_LOOPBACK_PORT_ENV = "PSI_OAUTH_LOOPBACK_PORT"
_DEFAULT_LOOPBACK_PORT = 17860
_CALLBACK_PATH = "/oauth/callback"

# 本进程**自己**正在等待回调的端口。plan_receiver 的「端口空不空」判定必须认得它:
# 自己的 watcher 占着 17860 时回调照样能接到; 若误判成「被占 → manual」, 一次本可
# 免复制的授权会被静默降级成手工贴码 —— env_check 也会对着一台活着的监听报 manual,
# 把用户往「复制 code」上引 (线上实际发生过)。
_SELF_LISTENING: set[int] = set()

_DONE_HTML = (
    "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>{title}</title><style>"
    "html,body{{width:100%;height:100%;margin:0;padding:0;}}"
    "body{{display:grid;place-items:center;"
    "font-family:system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
    "background:linear-gradient(160deg,#f4f7fd,#e8eefb);}}"
    ".card{{background:#fff;border-radius:20px;box-shadow:0 14px 44px rgba(38,72,150,.14);"
    "padding:40px 52px;max-width:400px;width:100%;box-sizing:border-box;text-align:center;}}"
    ".icon{{font-size:52px;line-height:1;margin-bottom:12px;}}"
    "h1{{font-size:21px;margin:0 0 10px;color:#1c2b4a;}}"
    "p{{margin:0 0 26px;color:#5a6b8c;font-size:14.5px;line-height:1.75;}}"
    ".btn{{display:inline-block;padding:9px 26px;border:1px solid #d3daea;border-radius:999px;"
    "background:#fff;color:#5a6b8c;font-size:14px;cursor:pointer;text-decoration:none;}}"
    ".btn:hover{{background:#f2f5fc;color:#1c2b4a;}}"
    ".btn.primary{{background:#3370ff;border-color:#3370ff;color:#fff;}}"
    ".btn.primary:hover{{background:#275fe0;color:#fff;}}"
    "#hint{{display:none;margin-top:14px;color:#9aa7bd;font-size:12.5px;}}"
    "</style></head><body><div class='card'>"
    "<div class='icon'>{icon}</div><h1>{title}</h1><p>{note}</p>"
    "<button class='btn' onclick='closePage()'>✕ 关闭页面</button>"
    "{feishu_btn}"
    "<p id='hint'>浏览器未允许自动关闭, 请手动关闭本标签页后回到飞书。</p>"
    "</div>"
    "<script>function closePage(){{try{{window.close();}}catch(e){{}}"
    "setTimeout(function(){{document.getElementById('hint').style.display='block';}},400);}}</script>"
    "</body></html>"
)
_OK_TITLE = "授权成功"
_OK_ICON = "✅"
_OK_NOTE = "授权已完成, 现在可以回到飞书继续对话了。"
_FAIL_TITLE = "授权未完成"
_FAIL_ICON = "⚠️"
_FAIL_NOTE = "可以回到对话里重新发起授权。"


def callback_base() -> str:
    """Gateway 回调基址 (无尾斜杠); 未配置返回空串。"""
    return os.environ.get(_CALLBACK_BASE_ENV, "").strip().rstrip("/")


def loopback_port() -> int:
    """本机回环监听端口 (环境变量非法时用默认值)。"""
    raw = os.environ.get(_LOOPBACK_PORT_ENV, "").strip()
    if raw.isdigit() and 1 <= int(raw) <= 65535:
        return int(raw)
    return _DEFAULT_LOOPBACK_PORT


def is_private_callback(redirect_uri: str = "") -> bool:
    """回调地址是不是只有内网/本机才打得到。

    ``gateway`` 通道成立的前提是**用户的浏览器**能打开这个地址, 而配成内网 IP 时这个
    前提只对在内网的人成立: 外网用户点完「同意授权」, 浏览器跳不过去, 回调永远到不了
    取件箱 —— 而工具这边只看见「配了通道」, 便一直承诺「不用复制」, 两头落空。

    判到私网并不改变通道选择 (内网用户照样自动回流), 只是让调用方知道要额外备一条
    后路: 让用户把地址栏那整条 URL 贴回来, ``_extract_code`` 能直接从里面取 code。
    """
    host = (urlsplit(redirect_uri or callback_base()).hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        # 域名一律当作公网可达: 内网 DNS 名无从判断, 而误判成私网会让每次授权都
        # 多挂一段没必要的手工提示。
        return False


def gateway_redirect_uri() -> str:
    """Gateway 通道的 ``redirect_uri``; 未配置基址返回空串。"""
    base = callback_base()
    return f"{base}{_CALLBACK_PATH}" if base else ""


def loopback_redirect_uri() -> str:
    """回环通道的 ``redirect_uri`` (固定端口, 便于提前登记)。"""
    return f"http://127.0.0.1:{loopback_port()}{_CALLBACK_PATH}"


def _port_is_free(port: int) -> bool:
    """端口能否绑定。占用即视为回环通道不可用 (别抢别人的端口)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, contextlib.suppress(OSError):
        s.bind(("127.0.0.1", port))
        return True
    return False


def _port_usable(port: int) -> bool:
    """回环通道可用 = 端口空闲, 或正被**本进程自己的 watcher** 守着。

    自己的监听占着 17860 时回调照样能接到 —— 那正是「等授权中」的正常形态, 不算被占;
    只有**别人**占着 (``_port_is_free`` 失败且不在 ``_SELF_LISTENING``) 才算不可用。
    """
    return _port_is_free(port) or port in _SELF_LISTENING


def mark_self_listening(port: int) -> None:
    """记录本进程正在 127.0.0.1:*port* 上等回调 (供 ``plan_receiver`` 识别)。"""
    _SELF_LISTENING.add(port)


def unmark_self_listening(port: int) -> None:
    """撤销 :func:`mark_self_listening` 的记录。"""
    _SELF_LISTENING.discard(port)


@dataclass
class ReceiverPlan:
    """本次授权用哪条自动接收通道, 以及配套的 ``redirect_uri``。"""

    mode: str  # "gateway" | "loopback" | "manual"
    redirect_uri: str

    @property
    def automatic(self) -> bool:
        return self.mode != "manual"


def plan_receiver(explicit_redirect: str = "") -> ReceiverPlan:
    """按环境选自动接收通道: gateway → loopback → manual。

    ``explicit_redirect`` (来自 ``PSI_FEISHU_REDIRECT_URI``) 一旦设置就尊重它 ——
    那是用户在应用后台登记过的地址; 若它正好是本机回环且端口可用 (空闲, 或由本进程
    自己的 watcher 守着), 仍可自动接收, 否则只能手工贴码。
    """
    if explicit_redirect:
        host = (urlsplit(explicit_redirect).hostname or "").lower()
        port = urlsplit(explicit_redirect).port
        if host in ("127.0.0.1", "localhost") and port and _port_usable(port):
            return ReceiverPlan(mode="loopback", redirect_uri=explicit_redirect)
        return ReceiverPlan(mode="manual", redirect_uri=explicit_redirect)
    gw = gateway_redirect_uri()
    if gw:
        return ReceiverPlan(mode="gateway", redirect_uri=gw)
    if _port_usable(loopback_port()):
        return ReceiverPlan(mode="loopback", redirect_uri=loopback_redirect_uri())
    return ReceiverPlan(mode="manual", redirect_uri="http://localhost/")


def _parse_request_target(request_line: str) -> dict[str, str]:
    """从 HTTP 请求行里取出 query 参数 (只关心 code / state / error)。"""
    parts = request_line.split(" ")
    if len(parts) < 2:
        return {}
    qs = parse_qs(urlsplit(parts[1]).query)
    return {k: v[0] for k, v in qs.items() if v}


def _chat_from_state(state: str) -> str:
    """从 ``<random>.oc_xxx`` 形态的 state 里取回 chat_id (授权发起时拼入的尾巴)。"""
    return state.split(".", 1)[1] if "." in state else ""


def _feishu_chat_btn(chat_id: str) -> str:
    """「回到飞书对话」按钮: applink 深链直接打开该会话, 让用户授权完就回到聊天。"""
    if not chat_id:
        return ""
    href = f"https://applink.feishu.cn/client/chat/open?chatId={quote(chat_id, safe='')}"
    return f"<p style='margin:12px 0 0'><a class='btn primary' href='{href}'>回到飞书对话</a></p>"


async def _serve_one_callback(port: int, expected_state: str, result: dict[str, str]) -> None:
    """接一次回调就收工: 校验 ``state``, 记下 code/error, 回一张成功页。

    不匹配的 ``state`` 一律回 400 且**不写** ``result`` —— 别的进程或恶意页面打过来
    的回调不能顶替真正的授权结果, 监听继续等真回调。
    """
    done = anyio.Event()

    async def _handle(stream: anyio.abc.SocketStream) -> None:
        async with stream:
            raw = b""
            with contextlib.suppress(Exception):
                while b"\r\n\r\n" not in raw and len(raw) < 8192:
                    chunk = await stream.receive(4096)
                    if not chunk:
                        break
                    raw += chunk
            query = _parse_request_target(raw.split(b"\r\n", 1)[0].decode("latin-1"))
            state = query.get("state", "")
            feishu_btn = _feishu_chat_btn(_chat_from_state(expected_state)) if state == expected_state else ""
            if not state or state != expected_state:
                body = _DONE_HTML.format(
                    title=_FAIL_TITLE, icon=_FAIL_ICON, feishu_btn="", note="state 不匹配, 请重新发起授权."
                )
                status = "400 Bad Request"
            else:
                code = query.get("code", "")
                error = query.get("error", "") or query.get("error_description", "")
                if code:
                    result["code"] = code
                    body, status = (
                        _DONE_HTML.format(title=_OK_TITLE, icon=_OK_ICON, feishu_btn=feishu_btn, note=_OK_NOTE),
                        "200 OK",
                    )
                else:
                    result["error"] = error or "callback carried neither code nor error"
                    body = _DONE_HTML.format(title=_FAIL_TITLE, icon=_FAIL_ICON, feishu_btn=feishu_btn, note=_FAIL_NOTE)
                    status = "400 Bad Request"
            payload = body.encode("utf-8")
            head = (
                f"HTTP/1.1 {status}\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("latin-1")
            with contextlib.suppress(Exception):
                await stream.send(head + payload)
            # 回完页面再收工, 否则浏览器可能拿不到成功页。
            if result:
                done.set()

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=port)
    mark_self_listening(port)
    try:
        async with listener, anyio.create_task_group() as tg:
            tg.start_soon(listener.serve, _handle, tg)
            await done.wait()
            tg.cancel_scope.cancel()
    finally:
        unmark_self_listening(port)


async def wait_loopback(port: int, expected_state: str, timeout_seconds: float) -> dict[str, str]:
    """起一次性回环监听等回调; 超时返回空 dict。"""
    result: dict[str, str] = {}
    with anyio.move_on_after(timeout_seconds), contextlib.suppress(OSError):
        await _serve_one_callback(port, expected_state, result)
    return result


async def poll_gateway(state: str, timeout_seconds: float, interval: float = 1.0) -> dict[str, str]:
    """轮询 Gateway 的 ``/oauth/code`` 直到取到 code/error 或超时。"""
    base = callback_base()
    if not base:
        return {}
    import httpx  # noqa: PLC0415

    result: dict[str, str] = {}
    with anyio.move_on_after(timeout_seconds):
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                with contextlib.suppress(Exception):
                    resp = await client.get(f"{base}/oauth/code", params={"state": state})
                    if resp.status_code == 200:
                        data: Any = resp.json()
                        if isinstance(data, dict) and (data.get("code") or data.get("error")):
                            result = {k: str(v) for k, v in data.items() if k in ("code", "error")}
                            break
                await anyio.sleep(interval)
    return result
