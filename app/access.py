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

第二种入口是公网隧道（``--tunnel``，cloudflared 把外网请求转到本机 8766）。
隧道带来的麻烦是：所有请求的对端都是 127.0.0.1，看起来像「坐在主机前」。
所以隧道模式额外打开 :func:`trust_proxy`，改看 Cloudflare 写进来的真实来源；
未开隧道时一律不看请求头，避免局域网设备伪造。

这层不是用来对抗公网攻击的（本来就不该把 8766 裸奔到公网），而是防止
同一 Wi-Fi 下的其他人、或隧道 URL 被转发出去后陌生人误入。
"""

from __future__ import annotations

import hmac
import os
import secrets
from typing import Any

from fastapi import Request

from . import settings_store


REMOTE_ENV = "WALLET_LINK_REMOTE"
# 公网隧道模式（cloudflared）：连接从本机回环进来，真实来源在请求头里。
# 这个开关只在 --tunnel 启动时打开，默认关闭——否则任何能直连 0.0.0.0 的
# 局域网设备都能靠伪造一个请求头把自己说成「本机」。
TRUST_PROXY_ENV = "WALLET_LINK_TRUST_PROXY"
COOKIE_NAME = "wl_access"
# 记住 90 天：手机浏览器每次都要手输 16 位口令的话没人愿意用。
COOKIE_MAX_AGE = 90 * 24 * 3600
# 回环地址：这些来源视为「坐在主机前」，免口令。
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "ip6-localhost", "::ffff:127.0.0.1"})
# 隧道 / 反代在这几个头里写真实来源，按可信度排序。
# CF-Connecting-IP 由 Cloudflare 边缘写入并覆盖客户端自带的值；
# X-Forwarded-For 是逗号链，第一段才是最初的客户端。
FORWARD_HEADERS = ("cf-connecting-ip", "x-real-ip", "x-forwarded-for")
# 去掉容易看错的 I / O / 0 / 1，手机上手输不容易出错。
_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_TOKEN_LENGTH = 16


def remote_enabled() -> bool:
    """是否处于远程模式（服务监听在非回环地址上）。"""

    return os.getenv(REMOTE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def trust_proxy() -> bool:
    """是否显式声明「本服务在隧道/反代后面」（``--tunnel``）。

    来源判定不再依赖这个开关（见 :func:`client_ip`：回环 + 转发头即视为
    经代理进来）。保留它只用于状态展示，让界面能说清"这台机器挂着公网
    入口"，以及兼容既有部署脚本。
    """

    return os.getenv(TRUST_PROXY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _peer(request: Request) -> str:
    """TCP 连接的对端地址，也就是「谁真的连上了我」。"""

    client = request.client
    return client.host if client else ""


def _forwarded_candidate(request: Request) -> str:
    """请求头里写的「真实来源」，没有则空串。"""

    for name in FORWARD_HEADERS:
        value = (request.headers.get(name) or "").strip()
        if not value:
            continue
        candidate = value.split(",")[0].strip()
        if candidate:
            return candidate
    return ""


def client_ip(request: Request) -> str:
    """判断访问者身份时应该用的地址。

    走隧道时所有请求的对端都是 127.0.0.1——如果直接拿它做判断，公网上的
    任何人都会被当成「坐在主机前」，免口令进站，还能读到口令本身。所以
    这种连接要看 Cloudflare 写进来的真实来源。

    判据分两层，缺一不可：

    1. **对端必须是回环**。隧道（cloudflared）和本机反向代理都是从本机
       发起的回环连接；局域网设备直连 0.0.0.0 时对端是 192.168.x.x，
       它带任何头都不改变结论——否则任何人都能发一个头冒充本机。
    2. **必须带转发头**。v0.8.42 修的就是这一层：以前只在 ``--tunnel``
       启动时才看头，于是用 ``--remote`` 起服务、后面又挂了 cloudflared
       隧道时，所有隧道请求都被当成主机本机——免口令、能读访问口令、能改
       key 和额度（实测远程直接 ``GET /api/access/token`` 就拿到了口令）。

    本机浏览器直接访问 127.0.0.1:8766 不会带 ``CF-Connecting-IP`` 这类头，
    所以「回环 + 带转发头」这个组合足以区分"坐在主机前"和"经隧道进来"，
    不再依赖用户有没有记得加 ``--tunnel``。
    """

    peer = _peer(request)
    if peer not in LOCAL_HOSTS:
        return peer
    candidate = _forwarded_candidate(request)
    if not candidate:
        return peer
    # 反代把来源写成回环（本机上的其他反代、或系统代理加了头）时，
    # 仍然按本机处理——不然坐在主机前的人反而被要求输口令。
    if candidate in LOCAL_HOSTS:
        return peer
    return candidate


def is_local(request: Request) -> bool:
    """请求是否来自主机本机。"""

    return client_ip(request) in LOCAL_HOSTS


def is_host(request: Request) -> bool:
    """本次请求是否「等同于坐在主机前」。

    分两种情形，合并成一个判断是为了让调用方不必各写一遍：

    - **未开远程模式**：服务只监听 127.0.0.1，能连上来的一定出自本机
      （含本机反代、测试客户端），一律算主机。
    - **远程模式**：只有真实来源在回环上才算主机。

    管理类接口（key 池、访问口令）与「主机专属字段」都用它，避免出现
    「一个接口用 remote_enabled、另一个只用 is_local」这种口径漂移。
    """

    if not remote_enabled():
        return True
    return is_local(request)


def via_tunnel(request: Request) -> bool:
    """本次请求是否经隧道/反代转进来（对端是回环，但真实来源在外网/局域网）。"""

    return _peer(request) in LOCAL_HOSTS and not is_local(request)


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
        # 隧道模式：前端可以据此把「远程访问」角标说得更准确。
        "trust_proxy": trust_proxy(),
        "via_tunnel": via_tunnel(request),
        "local": local,
        "authenticated": granted,
        "required": enabled and not granted,
        # 展示真实来源：走隧道时对端永远是 127.0.0.1，直接显示会误导。
        "client": client_ip(request),
    }
    # 只有已经取得访问权才给口令线索；未认证的远程请求不泄漏任何信息。
    if granted:
        payload["token_hint"] = hint()
    return payload
