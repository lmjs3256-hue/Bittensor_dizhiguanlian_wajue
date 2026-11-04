"""签发离线授权码（管理员在自己电脑上运行，不要随安装包发给用户）。

用法示例
--------
    # 1. 看某台电脑的机器码：在对方电脑上打开工具，未激活页面会直接显示
    #    也可以运行: .venv\\Scripts\\python.exe -m app.license

    # 2. 给一个团队 + 一台机器签 1 年
    python tools\\issue_license.py --team "XX 研究组" --machine ABCD-EFGH-JKLM-NPQR --days 365

    # 3. 多台机器用同一个授权码（最多 50 台），或长期有效
    python tools\\issue_license.py --team "XX 研究组" --machine AAAA-BBBB-CCCC-DDDD --machine EEEE-FFFF-GGGG-HHHH --permanent

    # 4. 直接写成本机的 license.key（管理员自己用）
    python tools\\issue_license.py --team "XX 研究组" --machine <本机机器码> --days 365 --write

私钥默认放在仓库外的 ..\\..\\.license-keys\\wallet_link_private.pem，
也可以用环境变量 WALLET_LINK_PRIVATE_KEY 指定。私钥丢失后无法再签发新授权码
（只能换密钥对并给所有人重发），请单独备份。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.license import (  # noqa: E402
    LICENSE_FILENAME,
    LicenseError,
    machine_code,
    normalize_machine,
    verify_token,
)


def private_key_path() -> Path:
    override = os.getenv("WALLET_LINK_PRIVATE_KEY")
    if override:
        return Path(override)
    return ROOT_DIR.parent / ".license-keys" / "wallet_link_private.pem"


def load_private_key(create: bool = False):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path = private_key_path()
    if not path.exists():
        if not create:
            raise SystemExit(
                f"找不到私钥：{path}\n"
                "请把管理员私钥放到这个位置，或设置环境变量 WALLET_LINK_PRIVATE_KEY。\n"
                "第一次使用可以加 --init-key 生成一对新密钥（此时旧授权码全部失效）。"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        from cryptography.hazmat.primitives import serialization as s

        raw = key.public_key().public_bytes(
            encoding=s.Encoding.Raw, format=s.PublicFormat.Raw
        )
        print(f"已生成新私钥：{path}")
        print("请把 app/license.py 里的 PUBLIC_KEY_B64 改成：")
        print(base64.b64encode(raw).decode())
        return key
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_token(payload: dict) -> str:
    body = b64(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    signature = load_private_key().sign(f"WL1.{body}".encode("ascii"))
    return f"WL1.{body}.{b64(signature)}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="签发 Wallet Link 离线授权码")
    parser.add_argument("--team", default="", help="团队名称，会显示在页面和导出文件的水印里")
    parser.add_argument(
        "--machine",
        action="append",
        default=[],
        help="允许运行的机器码，可重复传；填 * 表示不绑定机器（不推荐）",
    )
    parser.add_argument("--days", type=int, default=365, help="有效天数，默认 365")
    parser.add_argument("--expires", default="", help="到期日 YYYY-MM-DD，优先于 --days")
    parser.add_argument("--permanent", action="store_true", help="长期有效（不写到期日）")
    parser.add_argument("--note", default="", help="备注，只存在授权码里，页面不显示")
    parser.add_argument("--out", default="", help="把授权码写到指定文件")
    parser.add_argument("--write", action="store_true", help="直接写成本机 license.key")
    parser.add_argument("--verify", default="", help="只校验一个已有授权码")
    parser.add_argument("--print-machine", action="store_true", help="打印本机机器码后退出")
    parser.add_argument(
        "--init-key", action="store_true", help="私钥不存在时生成一对新密钥（旧授权码会失效）"
    )
    parser.add_argument(
        "--interactive", "-i", action="store_true", help="一步步问答式签发（双击 签发授权.cmd 用的就是这个）"
    )
    return parser.parse_args()


def format_machine(code: str) -> str:
    """把 16 位机器码排成 XXXX-XXXX-XXXX-XXXX，方便核对。"""

    compact = normalize_machine(code)
    if len(compact) != 16:
        return code
    return "-".join(compact[index : index + 4] for index in range(0, 16, 4))


def issue(team: str, machines: list[str], expires_text: str, note: str = "") -> tuple[dict, str]:
    """签发一个授权码，并立刻自检签名。"""

    if not team.strip():
        raise SystemExit("团队名称不能为空")
    if not machines:
        raise SystemExit("至少需要一个机器码")
    if len(machines) > 50:
        raise SystemExit("一个授权码最多绑定 50 台机器，请拆成多个授权")

    payload = {
        "v": 1,
        "team": team.strip(),
        "machines": machines,
        "expires": expires_text,
        "issued": date.today().isoformat(),
        "note": note.strip(),
    }
    token = build_token(payload)
    verify_token(token, check_machine=False)  # 避免把坏码发出去
    return payload, token


def copy_to_clipboard(text: str) -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001 - 没装 tk 时退回系统 clip
        pass
    try:
        import subprocess

        subprocess.run("clip", input=text.encode("ascii"), check=True, shell=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def ask(prompt: str, default: str = "") -> str:
    try:
        raw = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        raise SystemExit(1)
    return raw.strip() or default


def interactive() -> None:
    """双击 签发授权.cmd 后走的一步步问答流程。"""

    print("=" * 60)
    print("  Wallet Link 授权码签发")
    print("=" * 60)
    print("对方打开工具时，未激活的页面上会显示 16 位机器码，")
    print("形如 KNT5-YFER-N3PA-5W7Z，让他点“复制”后发给你。")
    print()

    machines: list[str] = []
    while True:
        raw = ask(f"① 粘贴机器码（第 {len(machines) + 1} 台，直接回车结束）: ")
        if not raw:
            break
        for part in raw.replace("，", ",").replace(" ", ",").split(","):
            part = part.strip()
            if not part:
                continue
            code = "*" if part == "*" else normalize_machine(part)
            if code != "*" and len(code) != 16:
                print(f"   ！{part} 不是 16 位机器码，已跳过（请让对方复制激活页上的完整机器码）。")
                continue
            if code in machines:
                print(f"   = {code} 已经加过了")
                continue
            machines.append(code)
            print(f"   + 已记录 {format_machine(code)}")

    if not machines:
        print("没有输入机器码，已退出。")
        return

    team = ask("② 团队名称（会显示在对方页面右下角的水印里）[内部团队]: ", "内部团队")

    while True:
        raw = ask("③ 有效期天数（直接回车 = 365 天，输入 0 = 长期有效）[365]: ", "365")
        try:
            days = int(raw)
        except ValueError:
            print("   请输入数字，例如 180。")
            continue
        if days < 0:
            print("   天数不能是负数。")
            continue
        break
    if days == 0:
        expires_text = ""
    else:
        expires_text = (date.today() + timedelta(days=max(1, days))).isoformat()

    print()
    print("-" * 60)
    print(f"团队　：{team}")
    print(f"机器　：{', '.join(format_machine(item) for item in machines)}（共 {len(machines)} 台）")
    print(f"到期　：{expires_text or '长期有效'}")
    print("-" * 60)
    if ask("确认签发？(Y/n): ", "y").lower() not in {"y", "yes"}:
        print("已取消，没有生成授权码。")
        return

    payload, token = issue(team, machines, expires_text)

    print()
    print("授权码（整行复制发给对方，粘贴时不要带多余空行）：")
    print()
    print(token)
    print()

    copied = copy_to_clipboard(token)
    print("已复制到剪贴板。" if copied else "（没能自动复制，请手动选中上面那行复制。）")

    out_dir = ROOT_DIR / "授权码"
    safe_team = "".join(ch for ch in team if ch not in '\\/:*?"<>|').strip() or "license"
    out_path = out_dir / f"{date.today().isoformat()}-{safe_team}.txt"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            "\n".join(
                [
                    f"团队：{payload['team']}",
                    f"机器码：{', '.join(format_machine(item) for item in machines)}",
                    f"到期：{expires_text or '长期有效'}",
                    f"签发日：{date.today().isoformat()}",
                    "",
                    "授权码：",
                    token,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(f"同时备份到了：{out_path}")
    except OSError as exc:
        print(f"（备份文件没写成：{exc}，不影响使用）")

    print()
    print("对方怎么激活：打开工具页面 → 粘贴到“粘贴授权码”框 → 点“激活”。")
    print("授权码和机器绑定，只能在这台电脑上用；换电脑要重新签发。")
    print()
    input("按回车键关闭这个窗口…")


def main() -> None:
    args = parse_args()

    if args.print_machine:
        print(machine_code())
        return

    if args.init_key:
        load_private_key(create=True)
        return

    if args.verify:
        try:
            row = verify_token(args.verify)
        except LicenseError as exc:
            raise SystemExit(f"校验失败：{exc}")
        print(f"校验通过：团队={row.team} 到期={row.expires or '长期'} 机器={','.join(row.machines)}")
        return

    if args.interactive:
        interactive()
        return

    if not args.team.strip():
        raise SystemExit("请用 --team 指定团队名称（或直接运行 签发授权.cmd 走问答模式）")

    machines = []
    for item in args.machine:
        machines.extend(part for part in str(item).replace("，", ",").split(",") if part.strip())
    machines = [item.strip() if item.strip() == "*" else normalize_machine(item) for item in machines]
    machines = [item for item in machines if item]
    if not machines:
        raise SystemExit("请用 --machine 至少指定一个机器码（或 * 表示不绑定）")

    if args.permanent:
        expires_text = ""
    elif args.expires:
        try:
            expires_text = date.fromisoformat(args.expires).isoformat()
        except ValueError:
            raise SystemExit("--expires 要写成 YYYY-MM-DD")
    else:
        expires_text = (date.today() + timedelta(days=max(1, args.days))).isoformat()

    payload, token = issue(args.team.strip(), machines, expires_text, args.note.strip())

    print("授权码（复制整行发出去）：")
    print(token)
    print()
    print(f"团队：{payload['team']}")
    print(f"机器：{', '.join(machines)}")
    print(f"到期：{expires_text or '长期有效'}")
    if args.note:
        print(f"备注：{payload['note']}")

    if args.out:
        out = Path(args.out)
        out.write_text(token + "\n", encoding="utf-8")
        print(f"已写入：{out}")

    if args.write:
        from app.license import save_token

        row = save_token(token)
        print(f"已激活本机：{ROOT_DIR / LICENSE_FILENAME}（团队 {row.team}）")


if __name__ == "__main__":
    main()
