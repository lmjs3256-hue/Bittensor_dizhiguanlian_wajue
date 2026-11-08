"""本机网络地址探测：给启动面板和页面顶部共用。

抽成独立模块的原因：这套逻辑原来只写在 ``run.py``（启动脚本）里，页面上要显示
"远程访问地址"就得再抄一份——两处各写一遍必然走样。现在只有这一份实现。
"""

from __future__ import annotations

import socket
import time

# 隧道网址文件（tunnel.ps1 写入）。放在这里是为了让「读网址」也只有一处。
URL_FILE_NAME = "cloudflared.url"


def default_route_address() -> str | None:
    """默认出口地址：多网卡时用它挑「最该给用户看」的那个。

    UDP connect 只填路由表，不发包，所以离线也能用。
    """

    for probe in ("8.8.8.8", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 80))
                return sock.getsockname()[0]
        except OSError:
            continue
    return None


def is_tailscale(address: str) -> bool:
    """Tailscale 的 IPv4 固定落在 100.64.0.0/10（CGNAT 段）。"""

    parts = address.split(".")
    if len(parts) != 4 or parts[0] != "100":
        return False
    try:
        return 64 <= int(parts[1]) <= 127
    except ValueError:
        return False


def ipv4_addresses() -> list[str]:
    """列出本机对外可达的 IPv4（排除回环）。

    两条路子一起用：``getaddrinfo(hostname)`` 能拿到所有网卡，但某些 Windows
    环境下只返回一条；UDP connect 不需要真的发包，可以问出默认出口地址。
    """

    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found:
                found.append(address)
    except OSError:
        pass
    route = default_route_address()
    if route and route not in found:
        found.append(route)
    return [address for address in found if not address.startswith("127.")]


def lan_addresses() -> tuple[str, list[str]]:
    """返回 (首选局域网地址, 其余网卡地址)。

    多网卡机器上常有一堆虚拟网卡（VMware / WSL / Hyper-V），默认出口那个才是
    用户真正要用的，其余交给调用方折叠，避免把选择成本丢给用户。
    """

    addresses = [address for address in ipv4_addresses() if not is_tailscale(address)]
    route = default_route_address()
    if route in addresses:
        addresses.remove(route)
        addresses.insert(0, route)
    if not addresses:
        return "", []
    return addresses[0], addresses[1:]


def tailscale_addresses() -> list[str]:
    """本机所有 Tailscale IPv4 地址。"""

    return [address for address in ipv4_addresses() if is_tailscale(address)]


def cloudflared_running() -> bool:
    """当前系统里是否有 cloudflared 进程（判断隧道网址是否还有效）。"""

    import os
    import subprocess

    try:
        if os.name == "nt":
            # tasklist 在所有 Windows 版本都有，比依赖 psutil 稳妥。
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                errors="ignore",
            ).stdout
            return "cloudflared.exe" in out.lower()
        out = subprocess.run(["pgrep", "-x", "cloudflared"], capture_output=True, timeout=5)
        return out.returncode == 0
    except Exception:  # noqa: BLE001 - 探测失败就当没有，宁可少显示也不给错地址
        return False


def read_tunnel_url(url_file, wait_seconds: float = 6.0) -> str:
    """读当前隧道网址；没有或不是合法地址时返回空串。

    ``wait_seconds``：文件还没写出来时最多等多久。服务刚起、隧道还在和
    Cloudflare 握手时值得等一会儿；只做"顺便看一眼"就传 0 立即返回。

    只认文件内容不够：隧道进程退出后文件还留着，会让人对着一个死网址折腾。
    所以先确认 cloudflared 进程还在，再返回网址。
    """

    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        url = ""
        try:
            text = url_file.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            text = ""
        if text.startswith("https://") and ".trycloudflare.com" in text:
            url = text.split()[0]
        if url and cloudflared_running():
            return url
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.5)
