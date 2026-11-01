"""Read-only Bittensor Balances.Transfer collection."""

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
) -> dict[str, Decimal | None]:
    """Read current System.Account balances without indexing extra history."""

    substrate = AsyncSubstrateInterface(url)
    result: dict[str, Decimal | None] = {}
    try:
        await asyncio.wait_for(substrate.initialize(), timeout_seconds)
        for address in addresses:
            try:
                value = await asyncio.wait_for(
                    substrate.query("System", "Account", params=[address]),
                    timeout_seconds,
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
            )
        )
    # FastAPI's synchronous route is normally executed in a worker thread.
    # If a caller invokes the service inside an active loop, skip the network
    # lookup rather than nesting asyncio.run and blocking the loop.
    return {}


async def _query_wallet_values(
    url: str,
    addresses: list[str],
    *,
    timeout_seconds: float = 20.0,
) -> dict[str, dict[str, Decimal | str | None]]:
    """Read available TAO plus coldkey stake valued in current TAO.

    ``StakeInfoRuntimeApi.get_stake_info_for_coldkey`` returns subnet-specific
    Alpha positions. Alpha units are not comparable across subnets, so each
    position is converted with ``SwapRuntimeApi.current_alpha_price`` before
    the values are added. A failed stake lookup never gets presented as a
    complete zero-stake wallet.
    """

    substrate = AsyncSubstrateInterface(url)
    result: dict[str, dict[str, Decimal | str | None]] = {
        address: {
            "available_tao": None,
            "staked_tao": None,
            "total_value_tao": None,
            "balance_status": "unavailable",
        }
        for address in addresses
    }
    try:
        await asyncio.wait_for(substrate.initialize(), timeout_seconds)
        for address in addresses:
            available: Decimal | None = None
            stake_ok = False
            try:
                value = await asyncio.wait_for(
                    substrate.query("System", "Account", params=[address]),
                    timeout_seconds,
                )
                payload = getattr(value, "value", value)
                data = payload.get("data", {}) if isinstance(payload, dict) else {}
                free = int(data.get("free", 0)) if isinstance(data, dict) else 0
                available = Decimal(free) / RAO_PER_TAO
            except Exception:
                pass

            staked: Decimal | None = None
            try:
                raw_stakes = await asyncio.wait_for(
                    substrate.runtime_call(
                        api="StakeInfoRuntimeApi",
                        method="get_stake_info_for_coldkey",
                        params=[address],
                    ),
                    timeout_seconds,
                )
                stake_payload = getattr(raw_stakes, "value", raw_stakes)
                if isinstance(stake_payload, dict):
                    stake_payload = stake_payload.get(
                        "stakes", stake_payload.get("data", [])
                    )
                if stake_payload is None:
                    stake_payload = []
                if not isinstance(stake_payload, (list, tuple)):
                    raise TypeError("stake runtime result is not a list")

                positions: list[tuple[int, int]] = []
                for item in stake_payload:
                    decoded = getattr(item, "value", item)
                    if not isinstance(decoded, dict):
                        continue
                    netuid = int(decoded.get("netuid", 0))
                    stake_rao = int(decoded.get("stake", 0))
                    if stake_rao > 0:
                        positions.append((netuid, stake_rao))

                prices: dict[int, int] = {0: int(RAO_PER_TAO)}
                for netuid, _stake_rao in positions:
                    if netuid in prices:
                        continue
                    raw_price = await asyncio.wait_for(
                        substrate.runtime_call(
                            api="SwapRuntimeApi",
                            method="current_alpha_price",
                            params=[netuid],
                        ),
                        timeout_seconds,
                    )
                    price_payload = getattr(raw_price, "value", raw_price)
                    prices[netuid] = int(price_payload)

                staked = sum(
                    (
                        Decimal(stake_rao)
                        * Decimal(prices[netuid])
                        / (RAO_PER_TAO * RAO_PER_TAO)
                        for netuid, stake_rao in positions
                    ),
                    Decimal("0"),
                )
                stake_ok = True
            except Exception:
                # Keep the available component when the runtime API is not
                # exposed by an older node or a public endpoint is transient.
                pass

            current = result[address]
            current["available_tao"] = available
            current["staked_tao"] = staked
            current["balance_status"] = (
                "complete" if available is not None and stake_ok else
                "partial" if available is not None else "unavailable"
            )
            if available is not None and staked is not None:
                current["total_value_tao"] = available + staked
    finally:
        await substrate.close()
    return result


def query_wallet_values(
    url: str,
    addresses: list[str],
    *,
    timeout_seconds: float = 20.0,
) -> dict[str, dict[str, Decimal | str | None]]:
    """Synchronous wrapper for current wallet value queries."""

    if not addresses:
        return {}
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _query_wallet_values(
                url,
                addresses,
                timeout_seconds=timeout_seconds,
            )
        )
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
