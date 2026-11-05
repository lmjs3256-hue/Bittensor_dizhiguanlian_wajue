"""FastAPI application for Bittensor address links."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import license as license_mod
from . import settings_store
from .config import ROOT_DIR, database_path, settings
from .db import Database
from .inputs import InputError
from .jobs import RelationJobManager
from .validation import is_valid_ss58
from .service import LinkService, ScanBusyError
from .taostats import (
    TaostatsAuthError,
    TaostatsRateLimitError,
    TaostatsUnavailableError,
    clear_api_key as clear_taostats_api_key,
    configure_api_key as configure_taostats_api_key,
    is_configured as taostats_is_configured,
)
from .version_info import read_version_info


ROOT = Path(__file__).resolve().parent
CONFIG = settings()
DATABASE = Database(database_path())
VERSION_INFO = read_version_info()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    DATABASE.initialize()
    # 上次保存过 Taostats key 就自动装回内存，避免每次重启都要重填。
    if not taostats_is_configured():
        saved_key = settings_store.load_api_key()
        if saved_key:
            configure_taostats_api_key(saved_key)
    scheduler = asyncio.create_task(_job_scheduler())
    try:
        yield
    finally:
        scheduler.cancel()
        try:
            await scheduler
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关闭时不再关心
            pass


app = FastAPI(
    title="Bittensor Wallet Link Intel",
    version=VERSION_INFO["version"],
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
SERVICE = LinkService(DATABASE, CONFIG.rpc_url)
JOBS = RelationJobManager()

# 勾了「使用 Taostats 历史索引」但没有 key 时直接报错，不再静默退回本地索引：
# 以前那种回退会返回 0 个关联地址，用户只看到一张空表，以为是工具坏了。
TAOSTATS_KEY_MISSING = (
    "未配置 Taostats API key：请先在页面「Taostats API key」里粘贴并点「保存 API key」，再发起查询。"
    "如果只想查本地索引，请取消勾选「使用 Taostats 历史索引」。"
)

# 未激活时也要能打开的路径：首页显示激活页，健康检查和授权接口用于自检/激活。
PUBLIC_PATHS = {
    "/",
    "/api/health",
    "/api/version",
    "/api/license",
    "/api/license/activate",
}


@app.middleware("http")
async def license_guard(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if not license_mod.is_licensed():
        return JSONResponse(
            status_code=403,
            content={
                "detail": "工具未激活或授权已失效，请在首页粘贴管理员提供的授权码",
                "code": "unlicensed",
                "machine": license_mod.machine_code(),
            },
        )
    return await call_next(request)



async def _job_scheduler() -> None:
    """定时推进到期的分轮作业。

    每轮最多花掉 5 次 Taostats 请求，因此放在线程里执行，避免阻塞事件循环；
    同一时刻只推进一轮，和本机 5 次/分钟的限速保持一致。
    """

    while True:
        await asyncio.sleep(2)
        try:
            for job in JOBS.due():
                await asyncio.to_thread(JOBS.run_round, job)
            JOBS.cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 定时器不因单次失败退出
            continue


def service() -> LinkService:
    return SERVICE


class AddressInput(BaseModel):
    address: str = Field(min_length=1, max_length=64)
    # 分类是用户自定义的（大V / 群友 / 其他 + 自建），不再限定枚举。
    category: str = Field(default="其他", max_length=32)
    label: str = Field(default="", max_length=160)
    note: str = Field(default="", max_length=2000)


class BulkDeleteInput(BaseModel):
    ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=5000)


class NoteInput(BaseModel):
    note: str = Field(default="", max_length=2000)


class CategoriesInput(BaseModel):
    categories: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(
        default_factory=list, max_length=10
    )


class ExchangeConfirmationInput(BaseModel):
    confirmed: bool = True
    rejected: bool = False
    decision: str = Field(default="", pattern="^(|confirm|reject|reset)$")


class ExchangeAddressConfirmationInput(BaseModel):
    address: str = Field(min_length=1, max_length=64)
    label: str = Field(default="人工确认交易所", max_length=160)
    decision: str = Field(default="confirm", pattern="^(confirm|reject|reset)$")


class AddressNoteInput(BaseModel):
    address: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=2000)


class RelationInput(BaseModel):
    seed: str = Field(min_length=1, max_length=64)
    depth: int = Field(default=3, ge=1, le=3)
    min_edge_tao: str = Field(default="1")
    min_total_tao: str = Field(default="1")
    include_incoming: bool = True
    include_outgoing: bool = True
    data_source: str = Field(default="local", pattern="^(local|taostats)$")
    filter_exchanges: bool = False
    filter_validators: bool = False
    filter_systems: bool = False
    # 手动「移除关联」的地址：不进结果，也不再作为中转跳点（其线索整体剪除）。
    removed_addresses: list[str] = Field(default_factory=list, max_length=100)


class RelationJobInput(BaseModel):
    seed: str = Field(min_length=1, max_length=64)
    depth: int = Field(default=3, ge=1, le=3)
    min_edge_tao: str = Field(default="1")
    min_total_tao: str = Field(default="1")
    include_incoming: bool = True
    include_outgoing: bool = True
    filter_exchanges: bool = False
    filter_validators: bool = False
    filter_systems: bool = False
    removed_addresses: list[str] = Field(default_factory=list, max_length=100)
    round_interval_seconds: int | None = Field(default=None, ge=10, le=600)
    max_rounds: int | None = Field(default=None, ge=1, le=100)


class RelationSyncInput(BaseModel):
    seed: str = Field(default="", max_length=64)
    addresses: list[str] = Field(default_factory=list, max_length=5000)
    include_seed: bool = True
    category: str = Field(default="其他", max_length=32)
    label: str = Field(default="", max_length=160)
    note: str = Field(default="", max_length=2000)
    # 只同步「已经命中地址库的关联地址 + 发起地址」，用于把发起地址补进库。
    only_library_matched: bool = False


class HistoryDeleteInput(BaseModel):
    ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=5000)


class AddressBookInput(BaseModel):
    """新建地址库：给一次查询结果起个库名，把可导入地址批量入库。"""

    name: str = Field(min_length=1, max_length=60)
    seed: str = Field(default="", max_length=64)
    addresses: list[str] = Field(default_factory=list, max_length=5000)
    include_seed: bool = True
    category: str = Field(default="其他", max_length=32)
    note: str = Field(default="", max_length=2000)


class CategoryInput(BaseModel):
    name: str = Field(min_length=1, max_length=32)


class LicenseActivateInput(BaseModel):
    token: str = Field(min_length=10, max_length=4000)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(ROOT / "templates" / "index.html")


@app.get("/api/health")
def health(svc: Annotated[LinkService, Depends(service)]):
    status = license_mod.status()
    return {
        "ok": True,
        "service": "bittensor-wallet-link-intel",
        "version": VERSION_INFO["version"],
        "rpc_url": svc.rpc_url,
        "licensed": status["licensed"],
        "team": status["team"],
        # pid/root 用来区分“同一个服务”还是“另一个副本占着同一个端口”：
        # 把整个文件夹拷到别的磁盘再启动时，端口若被原副本占用，启动脚本据此提示换端口。
        "pid": os.getpid(),
        "root": str(ROOT_DIR),
    }


@app.get("/api/license")
def license_status():
    """当前授权状态；未激活时也开放，用于在激活页显示本机机器码。"""

    return license_mod.status()


@app.post("/api/license/activate")
def license_activate(payload: LicenseActivateInput):
    try:
        row = license_mod.save_token(payload.token)
    except license_mod.LicenseError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"ok": True, "team": row.team, "status": license_mod.status()}


@app.get("/api/version")
def version_info():
    """Return the version and structured release notes shown in the UI."""

    return read_version_info()


@app.get("/api/overview")
def overview(svc: Annotated[LinkService, Depends(service)]):
    return {
        "index": svc.index_summary(),
        "default_seed": svc.seed_address(),
        "rpc_url": svc.rpc_url,
        "taostats_configured": taostats_is_configured(),
        "taostats_key_storage": settings_store.storage_kind(),
        "api_usage": svc.api_usage_summary(),
    }


@app.post("/api/settings/taostats")
async def save_taostats_key(request: Request):
    raw = await request.body()
    if len(raw) > 4096:
        raise HTTPException(413, "API key 请求内容过大")
    try:
        import json

        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "API key 请求格式无效") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "API key 请求格式无效")
    candidate = payload.get("api_key")
    if not isinstance(candidate, str) or not candidate.strip() or len(candidate) > 1024:
        raise HTTPException(422, "API key 不能为空且不得超过 1024 个字符")
    key = candidate.strip()
    configure_taostats_api_key(key)
    # 同时也存到本机（DPAPI 加密），重启服务后自动装回内存。
    storage = settings_store.save_api_key(key)
    return {"configured": True, "storage": storage}


@app.delete("/api/settings/taostats")
def delete_taostats_key():
    clear_taostats_api_key()
    settings_store.clear_api_key()
    return {"configured": False, "storage": ""}


@app.get("/api/library")
def library(svc: Annotated[LinkService, Depends(service)]):
    return {"items": svc.library()}


@app.get("/api/library/export")
def export_library(
    svc: Annotated[LinkService, Depends(service)],
    format: str = Query(default="json", pattern="^(csv|json)$"),
):
    content, media_type = svc.export_library(format)
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="bittensor-address-library.{format}"'
            )
        },
    )


@app.post("/api/library")
def create_address(payload: AddressInput, svc: Annotated[LinkService, Depends(service)]):
    if not payload.address.strip():
        raise HTTPException(422, "address must not be blank")
    return svc.import_rows([payload.model_dump()], "manual")


@app.get("/api/library/books")
def list_address_books(svc: Annotated[LinkService, Depends(service)]):
    """自建地址库清单（按库名归组，含地址数与分类统计）。"""

    return {"items": svc.address_books()}


@app.get("/api/library/categories")
def list_categories(svc: Annotated[LinkService, Depends(service)]):
    """地址库分类清单：大V / 群友 / 其他 + 用户自建，含各分类地址数。"""

    return {"items": svc.categories()}


@app.post("/api/library/categories")
def create_category(payload: CategoryInput, svc: Annotated[LinkService, Depends(service)]):
    try:
        return svc.add_category(payload.name)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/library/categories/{name}")
def delete_category(name: str, svc: Annotated[LinkService, Depends(service)]):
    try:
        return svc.delete_category(name)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/library/books")
def create_address_book(
    payload: AddressBookInput, svc: Annotated[LinkService, Depends(service)]
):
    """新建地址库并导入：关联地址 + 发起地址，排除交易所和验证者。"""

    try:
        return svc.create_address_book(
            name=payload.name,
            seed=payload.seed.strip(),
            addresses=payload.addresses,
            include_seed=payload.include_seed,
            category=payload.category,
            note=payload.note,
        )
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/library/books/{name}/export")
def export_address_book(
    name: str,
    svc: Annotated[LinkService, Depends(service)],
    format: str = Query(default="txt", pattern="^(txt|csv)$"),
):
    try:
        content, media_type, filename = svc.export_address_book(name, format)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@app.delete("/api/library/books/{name}")
def dissolve_address_book(name: str, svc: Annotated[LinkService, Depends(service)]):
    """解散地址库：只清除库名标记，地址继续留在已知地址库中。"""

    try:
        return svc.dissolve_address_book(name)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/library/{row_id}")
def delete_address(row_id: int, svc: Annotated[LinkService, Depends(service)]):
    if not svc.remove_address(row_id):
        raise HTTPException(404, "address not found")
    return {"ok": True}


@app.patch("/api/library/{row_id}")
def update_address_note(
    row_id: int,
    payload: NoteInput,
    svc: Annotated[LinkService, Depends(service)],
):
    try:
        return svc.update_library_note(row_id, payload.note)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.patch("/api/library/{row_id}/categories")
def update_address_categories(
    row_id: int,
    payload: CategoriesInput,
    svc: Annotated[LinkService, Depends(service)],
):
    """人工修改地址分类，一个地址可同时属于多个分类。"""

    try:
        return svc.update_library_categories(row_id, payload.categories)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/library/{row_id}/confirm-exchange")
def confirm_exchange(
    row_id: int,
    payload: ExchangeConfirmationInput,
    svc: Annotated[LinkService, Depends(service)],
):
    try:
        decision = payload.decision or (
            "reject" if payload.rejected else ("confirm" if payload.confirmed else "reset")
        )
        return svc.decide_exchange(row_id, decision)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/library/confirm-exchange-address")
def confirm_exchange_address(
    payload: ExchangeAddressConfirmationInput,
    svc: Annotated[LinkService, Depends(service)],
):
    try:
        return svc.decide_exchange_address(payload.address, payload.decision, payload.label)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/library")
def delete_addresses(
    payload: BulkDeleteInput,
    svc: Annotated[LinkService, Depends(service)],
):
    return {"deleted": svc.remove_addresses(payload.ids)}


@app.post("/api/library/import")
async def import_library(
    request: Request,
    svc: Annotated[LinkService, Depends(service)],
):
    raw = await request.body()
    if len(raw) > 2_000_000:
        raise HTTPException(413, "upload exceeds 2 MB")
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        boundary = content_type.split("boundary=", 1)
        if len(boundary) != 2:
            raise HTTPException(422, "multipart boundary is missing")
        try:
            import email

            message = email.message_from_bytes(
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw
            )
            part = next(
                (
                    item
                    for item in message.walk()
                    if item.get_filename() is not None
                ),
                None,
            )
            if part is None:
                raise HTTPException(422, "no uploaded file")
            filename = part.get_filename() or "upload.txt"
            payload = part.get_payload(decode=True) or b""
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(422, f"invalid upload: {exc}") from exc
    else:
        filename = request.query_params.get("filename", "upload.txt")
        payload = raw
    try:
        return svc.import_upload(filename, payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, InputError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/library/import-exchanges")
def import_exchange_addresses(svc: Annotated[LinkService, Depends(service)]):
    try:
        return svc.sync_exchange_addresses()
    except TaostatsAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except TaostatsRateLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    except TaostatsUnavailableError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/notes")
def save_address_note(
    payload: AddressNoteInput,
    svc: Annotated[LinkService, Depends(service)],
):
    try:
        return svc.save_note(payload.address, payload.note)
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/scan")
def scan(
    svc: Annotated[LinkService, Depends(service)],
    blocks: int = Query(default=2, ge=1, le=16),
    advance: bool = Query(default=False),
):
    try:
        return svc.scan(blocks=blocks, advance=advance)
    except ScanBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"RPC scan failed: {exc}") from exc


@app.post("/api/relations")
def relations(payload: RelationInput, svc: Annotated[LinkService, Depends(service)]):
    if not payload.seed.strip():
        raise HTTPException(422, "seed must not be blank")
    if payload.data_source == "taostats" and not taostats_is_configured():
        raise HTTPException(400, TAOSTATS_KEY_MISSING)
    try:
        return svc.relations(
            seed=payload.seed.strip(),
            depth=payload.depth,
            min_edge_tao=payload.min_edge_tao,
            min_total_tao=payload.min_total_tao,
            include_incoming=payload.include_incoming,
            include_outgoing=payload.include_outgoing,
            data_source=payload.data_source,
            filter_exchanges=payload.filter_exchanges,
            filter_validators=payload.filter_validators,
            filter_systems=payload.filter_systems,
            removed_addresses=[value.strip() for value in payload.removed_addresses if value.strip()],
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/relations/jobs")
def start_relation_job(
    payload: RelationJobInput, svc: Annotated[LinkService, Depends(service)]
):
    """创建分轮查询作业：每分钟推进一轮，直到所有待查地址查完。"""

    if not payload.seed.strip():
        raise HTTPException(422, "seed must not be blank")
    if not taostats_is_configured():
        raise HTTPException(400, TAOSTATS_KEY_MISSING)
    try:
        job = JOBS.create(
            svc,
            seed=payload.seed.strip(),
            depth=payload.depth,
            min_edge_tao=payload.min_edge_tao,
            min_total_tao=payload.min_total_tao,
            include_incoming=payload.include_incoming,
            include_outgoing=payload.include_outgoing,
            filter_exchanges=payload.filter_exchanges,
            filter_validators=payload.filter_validators,
            filter_systems=payload.filter_systems,
            removed_addresses=[value.strip() for value in payload.removed_addresses if value.strip()],
            round_interval=payload.round_interval_seconds,
            max_rounds=payload.max_rounds,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JOBS.payload(job)


@app.get("/api/relations/jobs/{job_id}")
def get_relation_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return JOBS.payload(job)


@app.post("/api/relations/jobs/{job_id}/cancel")
def cancel_relation_job(job_id: str):
    job = JOBS.cancel(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return JOBS.payload(job)


@app.get("/api/relations/history")
def relation_history(
    svc: Annotated[LinkService, Depends(service)],
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """查询记录列表：每次关联查询（含中途停止）都会留下一条。"""

    return svc.relation_history(limit=limit, offset=offset)


@app.get("/api/relations/history/{archive_id}")
def relation_history_detail(
    archive_id: int, svc: Annotated[LinkService, Depends(service)]
):
    try:
        return svc.relation_history_detail(archive_id)
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/relations/history/{archive_id}/export")
def export_relation_history(
    archive_id: int,
    svc: Annotated[LinkService, Depends(service)],
    format: str = Query(default="txt", pattern="^(txt|json)$"),
    depth: int | None = Query(default=None, ge=1, le=3),
):
    try:
        content, media_type, filename = svc.export_relation_history(
            archive_id, format, depth=depth
        )
    except InputError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/relations/history/{archive_id}")
def delete_relation_history(
    archive_id: int, svc: Annotated[LinkService, Depends(service)]
):
    deleted = svc.delete_relation_history([archive_id])
    if not deleted:
        raise HTTPException(404, "history record not found")
    return {"deleted": deleted}


@app.delete("/api/relations/history")
def delete_relation_history_batch(
    payload: HistoryDeleteInput, svc: Annotated[LinkService, Depends(service)]
):
    return {"deleted": svc.delete_relation_history(payload.ids)}


@app.post("/api/relations/history/clear")
def clear_relation_history(svc: Annotated[LinkService, Depends(service)]):
    return {"deleted": svc.clear_relation_history()}


@app.post("/api/relations/sync-library")
def sync_relation_library(
    payload: RelationSyncInput, svc: Annotated[LinkService, Depends(service)]
):
    """把关联地址和发起地址去重写入地址库，已存在的地址不覆盖。"""

    try:
        return svc.sync_relation_addresses(
            seed=payload.seed.strip(),
            addresses=payload.addresses,
            include_seed=payload.include_seed,
            category=payload.category,
            label=payload.label,
            note=payload.note,
            only_library_matched=payload.only_library_matched,
        )
    except InputError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.exception_handler(ValueError)
async def validation_error(_request: Request, exc: ValueError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})
