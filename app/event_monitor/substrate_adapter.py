"""Synchronous facade over the project's async Substrate client.

The wallet tool already depends on ``async-substrate-interface`` (cyscale).
The event collector historically used the sync package, which installs the
conflicting ``scalecodec`` namespace.  This adapter keeps the collector's
blocking call shape while using one asynchronous client and one dedicated
event loop per adapter instance.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any


class AsyncSubstrateAdapter:
    def __init__(self, url: str, *, connect_timeout: float = 240.0) -> None:
        self.url = url
        self.connect_timeout = connect_timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="event-monitor-substrate-loop",
            daemon=True,
        )
        self._thread.start()
        self._client: Any = None
        self._closed = False
        self._init_lock = threading.Lock()
        self._run(self._connect(), timeout=connect_timeout)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, awaitable: Any, *, timeout: float | None = None) -> Any:
        if self._closed:
            raise RuntimeError("Substrate adapter is closed")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        return future.result(timeout)

    async def _connect(self) -> None:
        from async_substrate_interface import AsyncSubstrateInterface

        self._client = AsyncSubstrateInterface(self.url)
        await self._client.initialize()

    def _ensure_client(self) -> Any:
        if self._closed:
            raise RuntimeError("Substrate adapter is closed")
        if self._client is None:
            with self._init_lock:
                if self._client is None:
                    self._run(self._connect(), timeout=self.connect_timeout)
        return self._client

    def rpc_request(self, method: str, params: list[Any] | None = None) -> Any:
        client = self._ensure_client()
        return self._run(client.rpc_request(method, params or []))

    def init_runtime(
        self,
        block_hash: str | None = None,
        block_id: int | None = None,
        init: bool = False,
    ) -> Any:
        client = self._ensure_client()
        return self._run(
            client.init_runtime(
                block_hash=block_hash,
                block_id=block_id,
                init=init,
            )
        )

    def query(
        self,
        module: str,
        storage_function: str,
        params: list[Any] | None = None,
        block_hash: str | None = None,
        **kwargs: Any,
    ) -> Any:
        client = self._ensure_client()
        return self._run(
            client.query(
                module,
                storage_function,
                params=params,
                block_hash=block_hash,
                **kwargs,
            )
        )

    def query_map(
        self,
        module: str,
        storage_function: str,
        params: list[Any] | None = None,
        block_hash: str | None = None,
        **kwargs: Any,
    ) -> list[tuple[Any, Any]]:
        client = self._ensure_client()

        async def collect() -> list[tuple[Any, Any]]:
            result = await client.query_map(
                module,
                storage_function,
                params=params,
                block_hash=block_hash,
                **kwargs,
            )
            pairs: list[tuple[Any, Any]] = []
            async for key, value in result:
                pairs.append((key, value))
            return pairs

        return self._run(collect())

    def get_events(self, block_hash: str | None = None) -> Any:
        client = self._ensure_client()
        return self._run(client.get_events(block_hash=block_hash))

    async def _shutdown_client(self) -> None:
        client = self._client
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            future = asyncio.run_coroutine_threadsafe(self._shutdown_client(), self._loop)
            future.result(timeout=20)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        try:
            self._loop.close()
        except Exception:
            pass
