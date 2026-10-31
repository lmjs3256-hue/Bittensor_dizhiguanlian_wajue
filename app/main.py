"""FastAPI application for Bittensor address links."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import database_path, settings
from .db import Database
from .inputs import InputError
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


ROOT = Path(__file__).resolve().parent
CONFIG = settings()
DATABASE = Database(database_path())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    DATABASE.initialize()
    yield


app = FastAPI(title="Bittensor Wallet Link Intel", version="0.1.1", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
SERVICE = LinkService(DATABASE, CONFIG.rpc_url)


def service() -> LinkService:
    return SERVICE


class AddressInput(BaseModel):
    address: str = Field(min_length=1, max_length=64)
    category: str = Field(default="WATCH", pattern="^(SEED|WATCH|BLACKLIST|EXCHANGE|VALIDATOR)$")
    label: str = Field(default="", max_length=160)
    note: str = Field(default="", max_length=2000)


class BulkDeleteInput(BaseModel):
    ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=5000)


class NoteInput(BaseModel):
    note: str = Field(default="", max_length=2000)


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


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(ROOT / "templates" / "index.html")


@app.get("/api/health")
def health(svc: Annotated[LinkService, Depends(service)]):
    return {
        "ok": True,
        "service": "bittensor-wallet-link-intel",
        "version": "0.1.1",
        "rpc_url": svc.rpc_url,
    }


@app.get("/api/overview")
def overview(svc: Annotated[LinkService, Depends(service)]):
    return {
        "index": svc.index_summary(),
        "default_seed": svc.seed_address(),
        "rpc_url": svc.rpc_url,
        "taostats_configured": taostats_is_configured(),
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
    return {"configured": True}


@app.delete("/api/settings/taostats")
def delete_taostats_key():
    clear_taostats_api_key()
    return {"configured": False}


@app.get("/api/library")
def library(svc: Annotated[LinkService, Depends(service)]):
    return {"items": svc.library()}


@app.post("/api/library")
def create_address(payload: AddressInput, svc: Annotated[LinkService, Depends(service)]):
    if not payload.address.strip():
        raise HTTPException(422, "address must not be blank")
    return svc.import_rows([payload.model_dump()], "manual")


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
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.exception_handler(ValueError)
async def validation_error(_request: Request, exc: ValueError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})
