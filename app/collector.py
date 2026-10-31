"""Read-only Bittensor Balances.Transfer collection."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from async_substrate_interface import AsyncSubstrateInterface


RAO_PER_TAO = Decimal("1000000000")
SYSTEM_PREFIX = "5EYCAe5"


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
            }
        )
    return rows


class RPCCollector:
    def __init__(self, url: str, *, timeout_seconds: float = 25.0) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds

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
            await asyncio.wait_for(substrate.initialize(), self.timeout_seconds)
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
                rows = extract_balance_transfers(
                    events,
                    block_number=block_number,
                    block_hash=block_hash,
                    extrinsics=block.get("extrinsics", []),
                )
                await on_batch(rows, block_number, block_hash)
                scanned += 1
                transfers += len(rows)
            return {
                "head_block": head,
                "start_block": start if scanned else 0,
                "end_block": end if scanned else 0,
                "scanned_blocks": scanned,
                "transfer_count": transfers,
                "seconds": round(time.monotonic() - started, 2),
            }
        finally:
            await substrate.close()
