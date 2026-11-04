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
    parser.add_argument("--team", required=True, help="团队名称，会显示在页面和导出文件的水印里")
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
    return parser.parse_args()


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

    machines = []
    for item in args.machine:
        machines.extend(part for part in str(item).replace("，", ",").split(",") if part.strip())
    machines = [item.strip() if item.strip() == "*" else normalize_machine(item) for item in machines]
    machines = [item for item in machines if item]
    if not machines:
        raise SystemExit("请用 --machine 至少指定一个机器码（或 * 表示不绑定）")
    if len(machines) > 50:
        raise SystemExit("一个授权码最多绑定 50 台机器，请拆成多个授权")

    if args.permanent:
        expires_text = ""
    elif args.expires:
        try:
            expires_text = date.fromisoformat(args.expires).isoformat()
        except ValueError:
            raise SystemExit("--expires 要写成 YYYY-MM-DD")
    else:
        expires_text = (date.today() + timedelta(days=max(1, args.days))).isoformat()

    payload = {
        "v": 1,
        "team": args.team.strip(),
        "machines": machines,
        "expires": expires_text,
        "issued": date.today().isoformat(),
        "note": args.note.strip(),
    }
    token = build_token(payload)

    # 自检：签完立刻验一遍机器无关的部分，避免发出坏码
    verify_token(token, check_machine=False)

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
