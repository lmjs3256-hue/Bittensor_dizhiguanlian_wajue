"""Read-only Bittensor transfer and subnet stake-transfer collection."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from async_substrate_interface import AsyncSubstrateInterface


RAO_PER_TAO = Decimal("1000000000")
SYSTEM_PREFIX = "5EYCAe5"


async def _query_account_balances(
    url: str,
    addresses: list[str],
    *,
    timeout_seconds: float = 12.0,
    total_timeout_seconds: float = 20.0,
) -> dict[str, Decimal | None]:
    """Read current System.Account balances without indexing extra history.

    ``total_timeout_seconds`` bounds the *whole* batch (initialize + all
    addresses). A slow/unreachable node must never hold the result hostage
    after a long multi-round crawl has finished.
    """

    import time

    deadline = time.monotonic() + max(0.5, total_timeout_seconds)
    substrate = AsyncSubstrateInterface(url)
    result: dict[str, Decimal | None] = {}
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result
        await asyncio.wait_for(substrate.initialize(), min(timeout_seconds, remaining))
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                value = await asyncio.wait_for(
                    substrate.query("System", "Account", params=[address]),
                    min(timeout_seconds, remaining),
                )
                payload = getattr(value, "value", value)
                data = payload.get("data", {}) if isinstance(payload, dict) else {}
                free = int(data.get("free", 0)) if isinstance(data, dict) else 0
                result[address] = Decimal(free) / RAO_PER_TAO
            except Exception:
                result[address] = None
        return result
    finally:
        await substrate.close()


def query_account_balances(
    url: str,
    addresses: list[str],
    *,
    timeout_seconds: float = 12.0,
    total_timeout_seconds: float = 20.0,
) -> dict[str, Decimal | None]:
    """Synchronous wrapper used by the synchronous relation API."""

    if not addresses:
        return {}
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _query_account_balances(
                url,
                addresses,
                timeout_seconds=timeout_seconds,
                total_timeout_seconds=total_timeout_seconds,
            )
        )
    # FastAPI's synchronous route is normally executed in a worker thread.
    # If a caller invokes the service inside an active loop, skip the network
    # lookup rather than nesting asyncio.run and blocking the loop.
    return {}


def _event_value(item: Any) -> dict[str, Any]:
    value = getattr(item, "value", item)
    if not isinstance(value, dict):
        return {}
    event = value.get("event", {})
    return event if isinstance(event, dict) else {}


def extract_balance_transfers(
    events: list[Any],
    *,
    block_number: int,
    block_hash: str,
    extrinsics: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract only Balances.Transfer events.

    This deliberately excludes Withdraw and Deposit, which often accompany a
    transfer and would otherwise turn one transaction into several edges.
    """

    tx_hashes: dict[int, str] = {}
    for index, extrinsic in enumerate(extrinsics or []):
        value = getattr(extrinsic, "value", extrinsic)
        if isinstance(value, dict):
            tx_hashes[index] = str(value.get("extrinsic_hash") or "")
    rows = []
    for event_index, item in enumerate(events):
        value = getattr(item, "value", item)
        event = _event_value(item)
        if event.get("module_id") != "Balances" or event.get("event_id") != "Transfer":
            continue
        attributes = event.get("attributes")
        if isinstance(attributes, dict):
            source = attributes.get("from")
            target = attributes.get("to")
            amount = attributes.get("amount")
        elif isinstance(attributes, (list, tuple)) and len(attributes) >= 3:
            first, second, amount = attributes[0], attributes[1], attributes[2]
            if isinstance(first, dict):
                source, target = first.get("from"), first.get("to")
            else:
                source, target = first, second
        else:
            continue
        if amount is None:
            continue
        if not source or not target:
            continue
        amount_rao = int(amount)
        phase = value.get("phase", {}) if isinstance(value, dict) else {}
        if isinstance(phase, str):
            # Decoded ApplyExtrinsic events expose the transaction index.
            extrinsic_index = value.get("extrinsic_idx") if isinstance(value, dict) else None
        else:
            extrinsic_index = phase.get("extrinsic_index") if isinstance(phase, dict) else None
        tx_hash = tx_hashes.get(int(extrinsic_index), "") if extrinsic_index is not None else ""
        event_key = f"{block_number}:{event_index}:{source}:{target}:{amount_rao}"
        rows.append(
            {
                "event_key": event_key,
                "tx_hash": tx_hash,
                "block_number": block_number,
                "block_hash": block_hash,
                "event_index": event_index,
                "from_address": str(source),
                "to_address": str(target),
                "amount_rao": amount_rao,
                "amount_tao": Decimal(amount_rao) / RAO_PER_TAO,
                "timestamp": "",
                "edge_type": "tao_transfer",
                "netuid": None,
                "hotkey": "",
            }
        )
    return rows


def extract_stake_transfers(
    events: list[Any],
    *,
    block_number: int,
    block_hash: str,
    extrinsics: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Extract SubtensorModule.StakeTransferred edges between two coldkeys.

    Events probed on the finney endpoint on 2026-11-04 expose positional
    attributes: (origin_coldkey, destination_coldkey, hotkey, origin_netuid,
    destination_netuid, amount). ``amount`` is a TAO value in RAO, not an alpha
    quantity, so it shares the unit used by Balances.Transfer. Dict-shaped
    attributes are accepted as a decoder-compatible fallback. Records whose two
    coldkeys match are skipped because they do not link different wallets.
    """

    tx_hashes: dict[int, str] = {}
    for index, extrinsic in enumerate(extrinsics or []):
        value = getattr(extrinsic, "value", extrinsic)
        if isinstance(value, dict):
            tx_hashes[index] = str(value.get("extrinsic_hash") or "")
    rows: list[dict[str, Any]] = []
    for event_index, item in enumerate(events):
        value = getattr(item, "value", item)
        event = _event_value(item)
        if (
            event.get("module_id") != "SubtensorModule"
            or event.get("event_id") != "StakeTransferred"
        ):
            continue
        attributes = event.get("attributes")
        if isinstance(attributes, dict):
            source = attributes.get("origin_coldkey") or attributes.get("from")
            target = attributes.get("destination_coldkey") or attributes.get("to")
            hotkey = attributes.get("hotkey") or ""
            netuid = attributes.get("origin_netuid")
            amount = attributes.get("amount")
        elif isinstance(attributes, (list, tuple)) and len(attributes) >= 6:
            source, target, hotkey = attributes[0], attributes[1], attributes[2]
            netuid = attributes[3]
            amount = attributes[5]
        else:
            continue
        if not source or not target or amount is None:
            continue
        source, target = str(source), str(target)
        if source == target:
            continue
        try:
            amount_rao = int(amount)
            origin_netuid = int(netuid) if netuid is not None else None
        except (TypeError, ValueError):
            continue
        phase = value.get("phase", {}) if isinstance(value, dict) else {}
        if isinstance(phase, str):
            extrinsic_index = value.get("extrinsic_idx") if isinstance(value, dict) else None
        else:
            extrinsic_index = phase.get("extrinsic_index") if isinstance(phase, dict) else None
        tx_hash = tx_hashes.get(int(extrinsic_index), "") if extrinsic_index is not None else ""
        rows.append(
            {
                "event_key": f"stake:{block_number}:{event_index}:{source}:{target}:{amount_rao}",
                "tx_hash": tx_hash,
                "block_number": block_number,
                "block_hash": block_hash,
                "event_index": event_index,
                "from_address": source,
                "to_address": target,
                "amount_rao": amount_rao,
                "amount_tao": Decimal(amount_rao) / RAO_PER_TAO,
                "timestamp": "",
                "edge_type": "stake_transfer",
                "netuid": origin_netuid,
                "hotkey": str(hotkey),
            }
        )
    return rows


#: 单块读取（get_block / get_events）的超时。预热之后这两种请求都很快。
DEFAULT_TIMEOUT_SECONDS = 25.0

#: 连接 + 初始化 runtime 元数据的超时。
#:
#: `init_runtime()` 需要从链上拉取 runtime WASM/metadata，实测冷启动要
#: 25~72 秒（2026-11 观察）。它和「读一个块」完全不是一个量级的操作，
#: 用 25 秒去卡它会让**每一次扫描都必然超时**（此前的 bug：报错信息还是
#: 空的 `TimeoutError: `，界面上只显示 `RPC scan failed:`）。
#: 这里单独给它一个宽超时，预热完成后单块读取仍然只有 1~6 秒。
DEFAULT_WARMUP_TIMEOUT_SECONDS = 240.0


class RPCCollector:
    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        warmup_timeout_seconds: float = DEFAULT_WARMUP_TIMEOUT_SECONDS,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.warmup_timeout_seconds = warmup_timeout_seconds

    async def collect_range(
        self,
        *,
        start: int,
        end: int,
        on_batch: Any,
        stop_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        substrate = AsyncSubstrateInterface(self.url)
        head = 0
        scanned = 0
        transfers = 0
        started = time.monotonic()
        try:
            # 预热阶段：连接 + 拉取 runtime 元数据。这一步在冷启动时可能
            # 要几十秒，必须单独放宽超时，否则读第一块就会撞上 25 秒上限。
            await asyncio.wait_for(substrate.initialize(), self.warmup_timeout_seconds)
            await asyncio.wait_for(substrate.init_runtime(), self.warmup_timeout_seconds)
            warmup_seconds = round(time.monotonic() - started, 2)
            head = int(await substrate.get_block_number())
            end = min(end, head)
            start = max(1, min(start, end + 1))
            for block_number in range(start, end + 1):
                if stop_event is not None and stop_event.is_set():
                    break
                block = await asyncio.wait_for(
                    substrate.get_block(block_number=block_number),
                    self.timeout_seconds,
                )
                if block is None:
                    raise RuntimeError(f"block {block_number} is unavailable")
                block_hash = str(block["header"]["hash"])
                events = await asyncio.wait_for(
                    substrate.get_events(block_hash=block_hash),
                    self.timeout_seconds,
                )
                event_args = {
                    "events": events,
                    "block_number": block_number,
                    "block_hash": block_hash,
                    "extrinsics": block.get("extrinsics", []),
                }
                rows = extract_balance_transfers(**event_args) + extract_stake_transfers(**event_args)
                await on_batch(rows, block_number, block_hash)
                scanned += 1
                transfers += len(rows)
            return {
                "head_block": head,
                "start_block": start if scanned else 0,
                "end_block": end if scanned else 0,
                "scanned_blocks": scanned,
                "transfer_count": transfers,
                "warmup_seconds": warmup_seconds,
                "seconds": round(time.monotonic() - started, 2),
            }
        finally:
            await substrate.close()
