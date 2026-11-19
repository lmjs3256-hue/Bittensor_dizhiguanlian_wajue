"""Event-monitor facade over the shared, priority-aware chain gateway."""

from __future__ import annotations

from typing import Any

from ..shared_chain import PRIORITY_EVENT, get_shared_chain


class AsyncSubstrateAdapter:
    """Synchronous event-side facade using the shared high-priority connection."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._gateway = get_shared_chain(url)

    def rpc_request(self, method: str, params: list[Any] | None = None) -> Any:
        return self._gateway.rpc_request_sync(
            method,
            params or [],
            priority=PRIORITY_EVENT,
        )

    def init_runtime(
        self,
        block_hash: str | None = None,
        block_id: int | None = None,
        init: bool = False,
    ) -> Any:
        return self._gateway.init_runtime_sync(
            block_hash=block_hash,
            block_id=block_id,
            init=init,
            priority=PRIORITY_EVENT,
        )

    def query(
        self,
        module: str,
        storage_function: str,
        params: list[Any] | None = None,
        block_hash: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._gateway.query_sync(
            module,
            storage_function,
            params=params,
            block_hash=block_hash,
            priority=PRIORITY_EVENT,
            **kwargs,
        )

    def query_map(
        self,
        module: str,
        storage_function: str,
        params: list[Any] | None = None,
        block_hash: str | None = None,
        **kwargs: Any,
    ) -> list[tuple[Any, Any]]:
        return self._gateway.query_map_sync(
            module,
            storage_function,
            params=params,
            block_hash=block_hash,
            priority=PRIORITY_EVENT,
            **kwargs,
        )

    def get_block(self, block_number: int) -> Any:
        return self._gateway.get_block_sync(
            block_number,
            priority=PRIORITY_EVENT,
        )

    def get_events(self, block_hash: str | None = None) -> Any:
        if not block_hash:
            raise ValueError("block_hash is required for shared get_events")
        return self._gateway.get_events_sync(
            block_hash,
            priority=PRIORITY_EVENT,
        )

    def close(self) -> None:
        # The shared gateway is owned by the main application lifespan and is
        # intentionally not closed when an individual collector thread exits.
        return None
