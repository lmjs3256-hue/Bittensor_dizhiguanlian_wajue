"""远程访问控制：主机本机免验证，其他设备凭口令进入。

甲方案是「一台常开电脑跑服务，其余设备只当浏览器客户端」。要做到这点，
服务必须监听 ``0.0.0.0``，但一旦监听全网卡，同一个 Wi-Fi、同一个
Tailscale 网络里的任何人都能打开页面用你的 Taostats key、改地址库、删批次。

所以远程模式加一道口令：

- 默认（只监听 127.0.0.1）整套校验完全跳过——本机访问没有意义；
- 开启远程模式（``WALLET_LINK_REMOTE=1``，由 ``run.py --remote`` 设置）后，
  只有回环地址免验证，其余来源要么带 Cookie，要么带 ``X-Access-Token`` 头；
- 口令第一次需要时自动生成，落在 ``data/settings.json``，跟备份一起走；
- 口令只以 HttpOnly Cookie 下发，页面脚本读不到，避免被 XSS 顺走。

这层不是用来对抗公网攻击的（本来就不该把 8766 暴露到公网），而是防止
Tailscale 网络里共享了节点的设备、或同一 Wi-Fi 下的其他人误入。
"""

from __future__ import annotations

import hmac
import os
import secrets
from typing import Any

from fastapi import Request

from . import settings_store


REMOTE_ENV = "WALLET_LINK_REMOTE"
COOKIE_NAME = "wl_access"
# 记住 90 天：手机浏览器每次都要手输 16 位口令的话没人愿意用。
COOKIE_MAX_AGE = 90 * 24 * 3600
# 回环地址：这些来源视为「坐在主机前」，免口令。
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "ip6-localhost", "::ffff:127.0.0.1"})
# 去掉容易看错的 I / O / 0 / 1，手机上手输不容易出错。
_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_TOKEN_LENGTH = 16


def remote_enabled() -> bool:
    """是否处于远程模式（服务监听在非回环地址上）。"""

    return os.getenv(REMOTE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def is_local(request: Request) -> bool:
    """请求是否来自主机本机。"""

    client = request.client
    if client is None:
        return False
    return client.host in LOCAL_HOSTS


def _generate() -> str:
    raw = "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LENGTH))
    # 分四段，方便念给另一台设备输入：ABCD-EFGH-IJKL-MNOP
    return "-".join(raw[index : index + 4] for index in range(0, _TOKEN_LENGTH, 4))


def _normalize(value: str | None) -> str:
    return (value or "").strip().upper().replace(" ", "")


def ensure_token() -> tuple[str, bool]:
    """返回 ``(口令, 是否本次新生成)``。"""

    token = settings_store.load_access_token()
    if token:
        return token, False
    token = _generate()
    settings_store.save_access_token(token)
    return token, True


def rotate() -> str:
    """重置口令，旧口令立即失效（所有设备都要重新输入）。"""

    token = _generate()
    settings_store.save_access_token(token)
    return token


def verify(token: str | None) -> bool:
    expected = settings_store.load_access_token()
    if not expected:
        return False
    # 常量时间比较，避免用响应时间一点点试出口令。
    return hmac.compare_digest(_normalize(token), _normalize(expected))


def is_authenticated(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and verify(cookie):
        return True
    header = request.headers.get("x-access-token")
    return bool(header) and verify(header)


def allowed(request: Request) -> bool:
    """当前请求是否可以放行。

    未开启远程模式时永远放行——只在 127.0.0.1 上监听，能连上来的只有本机。
    """

    if not remote_enabled():
        return True
    return is_local(request) or is_authenticated(request)


def hint() -> str:
    """口令线索：只给末四位，界面上用来确认「是不是这一版」。"""

    token = settings_store.load_access_token()
    return f"末四位 {token[-4:]}" if token else ""


def status(request: Request) -> dict[str, Any]:
    enabled = remote_enabled()
    local = is_local(request)
    authenticated = is_authenticated(request)
    granted = (not enabled) or local or authenticated
    payload: dict[str, Any] = {
        "remote_mode": enabled,
        "local": local,
        "authenticated": granted,
        "required": enabled and not granted,
        "client": request.client.host if request.client else "",
    }
    # 只有已经取得访问权才给口令线索；未认证的远程请求不泄漏任何信息。
    if granted:
        payload["token_hint"] = hint()
    return payload
