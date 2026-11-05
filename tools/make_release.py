"""打外发包：把别人能用起来的文件压成一个 zip。

为什么单独写个脚本
------------------
直接右键"压缩为 ZIP"整个文件夹会失败：服务在跑的时候 `data\\wallet_link.db`
被 SQLite 进程占着，压缩工具会报"文件正由另一进程使用"。而且整目录里有
`.venv`（90 MB，换台电脑就失效）、`data`（自己的查询记录，不该外发）、
`license.key`（本机授权）这些东西，本来也不该打进包里。

用法：
    .venv\\Scripts\\python.exe tools\\make_release.py                # 给使用者的外发包
    .venv\\Scripts\\python.exe tools\\make_release.py --with-admin   # 连管理员工具和开发文档一起打

输出在项目根目录：`Bittensor地址关联挖掘-<版本>-外发包.zip`。
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 运行环境、本地数据、版本库、缓存：一律不进包
EXCLUDE_DIRS = {
    ".venv",
    "data",
    "logs",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "授权码",
    ".release",
    ".probe",
    ".workbuddy",
}
EXCLUDE_FILES = {"license.key"}

# 只有管理员/开发者才需要的内容，默认不打
ADMIN_ITEMS = {"tools", "docs", "tests", "授权管理.md", "签发授权.cmd"}

FIRST_RUN = """Bittensor 地址关联挖掘 · v{version}
================================

这是一个可以直接使用的分发包：不含任何本地数据库，也不含授权文件。

第一次使用
----------
1. 把整个文件夹解压到硬盘上（例如 D:\\wallet-link），不要在压缩包里直接双击运行；
2. 双击「安装.cmd」，等它跑完（第一次会装运行环境，约 1-3 分钟，全程不需要联网）；
3. 双击桌面上生成的「Bittensor 地址关联挖掘」快捷方式，或文件夹里的「启动.cmd」；
4. 第一次启动会显示这台电脑的 16 位机器码，把它发给管理员换授权码，粘贴后点「激活」。

日常
----
双击快捷方式启动，用完双击「停止.cmd」。详细说明见「使用说明.html」（双击即可离线阅读）。

说明
----
- 本包不含 data 目录：你查过的地址、地址库都只存在本机自己的数据库里，不会随包外流；
- license.key 需要每台电脑单独激活，拷贝别人的授权文件到本机不会生效；
- 同一台电脑上再放一份副本做测试时，第二份会自动换端口启动，不用重新安装或重新激活。
"""


def build(with_admin: bool, out: Path | None = None) -> Path:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    folder = f"Bittensor地址关联挖掘-{version}"
    suffix = "完整包" if with_admin else "外发包"
    target = out or (ROOT / f"{folder}-{suffix}.zip")

    with tempfile.TemporaryDirectory(prefix="wallet-link-release-") as temp:
        stage = Path(temp) / folder
        stage.mkdir()
        for item in sorted(ROOT.iterdir()):
            name = item.name
            if name in EXCLUDE_DIRS or name in EXCLUDE_FILES or name.endswith(".zip"):
                continue
            if name.startswith(".pytest_tmp") or name.startswith(".probe"):
                continue
            if not with_admin and name in ADMIN_ITEMS:
                continue
            if item.is_dir():
                shutil.copytree(
                    item,
                    stage / name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            else:
                shutil.copy2(item, stage / name)

        (stage / "先读我.txt").write_text(
            FIRST_RUN.format(version=version), encoding="utf-8-sig", newline="\r\n"
        )

        if target.exists():
            target.unlink()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage.parent).as_posix())

        count = sum(1 for path in stage.rglob("*") if path.is_file())

    size = target.stat().st_size / 1024 / 1024
    print(f"已生成：{target}")
    print(f"文件数：{count}　压缩后：{size:.1f} MB")
    print("已排除：.venv、data、logs、.git、license.key、缓存" + ("" if with_admin else "、tools、docs、tests、授权管理.md、签发授权.cmd"))
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="打外发包（不含本地数据与授权文件）")
    parser.add_argument("--with-admin", action="store_true", help="连管理员工具和开发文档一起打包")
    parser.add_argument("--out", help="输出 zip 路径，默认放项目根目录")
    args = parser.parse_args()
    build(args.with_admin, Path(args.out).resolve() if args.out else None)


if __name__ == "__main__":
    main()
