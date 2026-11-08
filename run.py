from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path


from app import netinfo


def _ipv4_addresses() -> list[str]:
    return netinfo.ipv4_addresses()


def _default_route_address() -> str | None:
    return netinfo.default_route_address()


def _tunnel_url_file() -> Path:
    """tunnel.ps1 把 cloudflared 分配到的网址写在这个文件里。"""

    return Path(__file__).resolve().parent / "logs" / netinfo.URL_FILE_NAME


def _read_tunnel_url(wait_seconds: float = 6.0) -> str:
    return netinfo.read_tunnel_url(_tunnel_url_file(), wait_seconds)


def _cloudflared_running() -> bool:
    return netinfo.cloudflared_running()


def _is_tailscale(address: str) -> bool:
    return netinfo.is_tailscale(address)


def _print_remote_banner(port: int, tunnel: bool = False) -> None:
    """远程模式下把「怎么连、口令是多少」直接打出来。"""

    from app import access

    token, created = access.ensure_token()
    addresses = _ipv4_addresses()
    tailscale = [address for address in addresses if _is_tailscale(address)]
    # 多网卡机器上常有一堆虚拟网卡（VMware / WSL / Hyper-V）。默认出口那个
    # 才是用户真正要用的，其余折叠成一行，避免把选择成本丢给用户。
    default_route = _default_route_address()
    physical = [address for address in addresses if not _is_tailscale(address)]
    if default_route in physical:
        physical.remove(default_route)
        physical.insert(0, default_route)
    primary, others = (physical[0], physical[1:]) if physical else ("", [])

    lines = [
        "",
        "─" * 62,
        " 公网隧道已开启（客户端只当浏览器，不需要装任何软件）"
        if tunnel
        else " 远程访问已开启（其余设备只当浏览器客户端）",
        "",
        f" 访问口令   {token}" + ("   ← 本次新生成" if created else ""),
        "            （存在 data/settings.json，主机页面也能查看）",
        "",
        f" 本机       http://127.0.0.1:{port}",
    ]
    if tunnel:
        # 隧道地址由 cloudflared 现分配，tunnel.ps1 会把最终网址写进
        # logs/cloudflared.url。以前这里只印一句"见隧道程序打印的地址"，
        # 但 cloudflared 是以隐藏窗口的后台进程跑的——用户根本看不到那个
        # "打印"，于是点了启动远程也不知道该访问什么。能读到就直接印出来。
        url = _read_tunnel_url()
        if url:
            lines += [
                f" 公网入口   {url}",
                "            经隧道进来的请求同样要输上面的口令——",
                "            「本机免验证」只对真正坐在这台电脑前的浏览器成立。",
            ]
        else:
            lines += [
                " 公网入口   尚未取得（隧道还在建立，或未以隧道模式启动）",
                f"            稍后看 {_tunnel_url_file()} 文件里的 https://…trycloudflare.com",
            ]
    else:
        if tailscale:
            for address in tailscale:
                lines.append(f" Tailscale  http://{address}:{port}")
        else:
            lines.append(" Tailscale  未检测到 100.64.0.0/10 地址")
            lines.append("            装了 Tailscale 的话先执行 tailscale up 再重启本服务")
        # 这几种入口只是"能连"，公网还得靠隧道。如果恰好有隧道在跑，顺手把
        # 网址也列出来——用户分不清 --remote 和 --tunnel 时，至少不会漏看地址。
        # 注意：普通远程模式下服务不采信代理头，经隧道进来的请求会被当成外部
        # 来源而要求口令，这是预期行为，不必在这里展开解释。
        public_url = _read_tunnel_url(0)
        if public_url:
            lines.append(f" 公网入口   {public_url}")
    if primary:
        lines.append(f" 局域网     http://{primary}:{port}")
    if others:
        lines.append(f" 其他网卡   {' / '.join(f'http://{a}:{port}' for a in others)}")
        lines.append("            （虚拟网卡，通常用不到）")
    if not tailscale and not primary and not tunnel:
        lines.append(" 其他设备   未探测到非回环地址，检查网络连接")
    lines += [
        "",
        " 提醒：同一把 Taostats API key 同一时刻只能有一台机器开巡检。",
        "       其余设备只打开浏览器访问，不要再单独启动一份服务。",
        "─" * 62,
        "",
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)
    # 告诉应用层「面板已经打过了」，避免 lifespan 再重复打一遍口令。
    os.environ["WALLET_LINK_BANNER_PRINTED"] = "1"


def run_doctor() -> int:
    """``python run.py --doctor``：不启动服务，直接检查环境并打印结论。

    远端排查时最有用的两条信息是「现在这份代码是哪个版本」和「预置名单在不在
    库里」，这里一次全打出来，不用开浏览器。
    """

    from app import diagnostics
    from app.config import ROOT_DIR, database_path
    from app.db import Database
    from app.taostats import is_configured as taostats_is_configured
    from app.version_info import read_version_info

    database = Database(database_path())
    database.initialize()
    try:
        report = diagnostics.build_report(
            database,
            version=read_version_info()["version"],
            # --doctor 是新起的进程，进程版本就是当前代码的版本，两者天然一致。
            running_version=read_version_info()["version"],
            root=ROOT_DIR,
            db_path=database.path,
            taostats_configured=taostats_is_configured(),
        )
    finally:
        database.engine.dispose()
    print(diagnostics.format_report(report))
    return 0 if report["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the address-link intelligence dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--remote",
        action="store_true",
        help="开放给 Tailscale / 局域网访问（自动绑定 0.0.0.0 并要求访问口令）",
    )
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="公网隧道模式：在 --remote 基础上改看 Cloudflare 传来的真实来源（含 --remote）",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="只做环境自检并打印结果，不启动服务",
    )
    args = parser.parse_args()
    if args.doctor:
        return run_doctor()

    host = args.host
    if args.tunnel:
        # 隧道是远程模式的超集：同样要绑 0.0.0.0（局域网照样能用），
        # 额外打开「采信代理头」，否则隧道进来的请求会被当成主机本机而免口令。
        args.remote = True
        os.environ["WALLET_LINK_TRUST_PROXY"] = "1"
    if args.remote:
        # 显式指定了 --host 就尊重它，否则绑全网卡：Tailscale 地址和局域网地址
        # 都落在不同网卡上，绑 127.0.0.1 时别的设备根本连不上。
        if host == "127.0.0.1":
            host = "0.0.0.0"
        # 子进程（--reload）会继承环境变量，所以这里设置就够了。
        os.environ["WALLET_LINK_REMOTE"] = "1"
        _print_remote_banner(args.port, tunnel=args.tunnel)

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(Path(__file__).resolve().parent / "app")] if args.reload else None,
        # 必须显式关掉。uvicorn 默认 proxy_headers=True，会先按 X-Forwarded-For
        # 改写 scope["client"]——那样「谁在访问」就由 uvicorn 的 forwarded_allow_ips
        # 决定（该值可被环境变量放宽成 *），局域网设备伪造一个头就能冒充本机
        # 免口令进来。这里关掉，来源判定统一走 app/access.py 的 client_ip()：
        # 只在 TCP 对端本身是回环（隧道/反代）时才采信代理头，其余一律看真实对端。
        proxy_headers=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
