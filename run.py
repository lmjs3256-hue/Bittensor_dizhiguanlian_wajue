from __future__ import annotations

import argparse
import sys
from pathlib import Path


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
        "--doctor",
        action="store_true",
        help="只做环境自检并打印结果，不启动服务",
    )
    args = parser.parse_args()
    if args.doctor:
        return run_doctor()

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(Path(__file__).resolve().parent / "app")] if args.reload else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
