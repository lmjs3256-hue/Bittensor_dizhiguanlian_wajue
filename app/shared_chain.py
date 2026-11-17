"""One priority-aware Substrate connection shared by event and wallet jobs."""

from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable


PRIORITY_EVENT = 0
PRIORITY_NORMAL = 100
DEFAULT_EVENT_TIMEOUT_SECONDS = 30.0
DEFAULT_NORMAL_TIMEOUT_SECONDS = 120.0


@dataclass(order=True)
class _WorkItem:
    priority: int
    sequence: int
    future: Future[Any] = field(compare=False, repr=False)
    operation: Callable[[Any], Any] = field(compare=False, repr=False)


class SharedChainGateway:
    """Own one AsyncSubstrateInterface and serialize work with priorities.

    Event-monitor operations use ``PRIORITY_EVENT``. Background address-index
    scans use ``PRIORITY_NORMAL``. Because both sides submit individual
    operations to the same queue, a pending event request runs before the next
    address-scan read while reusing one WebSocket connection.
    """

    def __init__(
        self,
        url: str,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.url = url
        self._client_factory = client_factory
        self._client: Any = None
        self._initialized = False
        self._closed = False
        self._sequence = 0
        self._lock = threading.Lock()
        self._reset_lock = threading.Lock()
        self._current_item: _WorkItem | None = None
        self._counters = {
            "event": 0,
            "normal": 0,
            "errors": 0,
            "timeouts": 0,
            "resets": 0,
        }
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="shared-substrate-chain",
            daemon=True,
        )
        self._thread.start()
        self._queue: asyncio.PriorityQueue[_WorkItem] | None = None
        self._worker_task: asyncio.Task[Any] | None = None
        ready = threading.Event()
        self._loop.call_soon_threadsafe(self._start_worker, ready)
        if not ready.wait(timeout=5):
            raise RuntimeError("shared chain gateway did not start")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _start_worker(self, ready: threading.Event) -> None:
        self._queue = asyncio.PriorityQueue()
        self._worker_task = self._loop.create_task(self._worker())
        ready.set()

    async def _ensure_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                from async_substrate_interface import AsyncSubstrateInterface

                self._client = AsyncSubstrateInterface(self.url)
        if not self._initialized:
            await self._client.initialize()
            self._initialized = True
        return self._client

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            if item.future.cancelled():
                continue
            self._current_item = item
            try:
                client = await self._ensure_client()
                result = item.operation(client)
                if inspect.isawaitable(result):
                    result = await result
                if not item.future.done():
                    item.future.set_result(result)
            except asyncio.CancelledError:
                if not item.future.done():
                    item.future.set_exception(RuntimeError("shared chain operation cancelled"))
                raise
            except Exception as exc:  # noqa: BLE001 - propagated to the caller
                with self._lock:
                    self._counters["errors"] += 1
                if not item.future.done():
                    item.future.set_exception(exc)
            finally:
                if self._current_item is item:
                    self._current_item = None

    def submit(
        self,
        priority: int,
        operation: Callable[[Any], Any],
        *,
        timeout: float | None = None,
    ) -> Future[Any]:
        if self._closed:
            raise RuntimeError("shared chain gateway is closed")
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
            bucket = "event" if priority <= PRIORITY_EVENT else "normal"
            self._counters[bucket] += 1
        future: Future[Any] = Future()

        async def enqueue() -> None:
            assert self._queue is not None
            await self._queue.put(_WorkItem(priority, sequence, future, operation))

        enqueue_future = asyncio.run_coroutine_threadsafe(enqueue(), self._loop)
        try:
            enqueue_future.result(timeout=timeout or 5)
        except Exception:
            future.cancel()
            raise
        return future

    def _operation_timeout(self, priority: int, timeout: float | None) -> float:
        if timeout is not None:
            return max(0.1, float(timeout))
        return (
            DEFAULT_EVENT_TIMEOUT_SECONDS
            if priority <= PRIORITY_EVENT
            else DEFAULT_NORMAL_TIMEOUT_SECONDS
        )

    def run_sync(
        self,
        priority: int,
        operation: Callable[[Any], Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        effective_timeout = self._operation_timeout(priority, timeout)
        future = self.submit(priority, operation, timeout=min(effective_timeout, 5.0))
        try:
            return future.result(timeout=effective_timeout)
        except TimeoutError as exc:
            with self._lock:
                self._counters["timeouts"] += 1
            future.cancel()
            self.reset(reason=f"operation timeout after {effective_timeout:g}s")
            raise TimeoutError(
                f"shared chain operation timed out after {effective_timeout:g}s"
            ) from exc

    async def run_async(
        self,
        priority: int,
        operation: Callable[[Any], Any],
        *,
        timeout: float | None = None,
    ) -> Any:
        effective_timeout = self._operation_timeout(priority, timeout)
        future = self.submit(priority, operation, timeout=min(effective_timeout, 5.0))
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(future),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError as exc:
            with self._lock:
                self._counters["timeouts"] += 1
            future.cancel()
            await asyncio.to_thread(
                self.reset,
                reason=f"operation timeout after {effective_timeout:g}s",
            )
            raise TimeoutError(
                f"shared chain operation timed out after {effective_timeout:g}s"
            ) from exc

    async def initialize(self, *, priority: int = PRIORITY_NORMAL) -> None:
        await self.run_async(priority, lambda client: client.initialize())

    async def init_runtime(
        self,
        *,
        block_hash: str | None = None,
        block_id: int | None = None,
        init: bool = False,
        priority: int = PRIORITY_NORMAL,
    ) -> Any:
        return await self.run_async(
            priority,
            lambda client: client.init_runtime(
                block_hash=block_hash,
                block_id=block_id,
                init=init,
            ),
        )

    async def get_block_number(self, *, priority: int = PRIORITY_NORMAL) -> int:
        return int(await self.run_async(priority, lambda client: client.get_block_number()))

    async def get_block_with_events(
        self,
        block_number: int,
        *,
        priority: int = PRIORITY_NORMAL,
    ) -> tuple[Any, Any, str]:
        async def read(client: Any) -> tuple[Any, Any, str]:
            block = await client.get_block(block_number=block_number)
            if block is None:
                raise RuntimeError(f"block {block_number} is unavailable")
            block_hash = str(block["header"]["hash"])
            events = await client.get_events(block_hash=block_hash)
            return block, events, block_hash

        return await self.run_async(priority, read)

    async def query(self, *args: Any, priority: int = PRIORITY_NORMAL, **kwargs: Any) -> Any:
        return await self.run_async(priority, lambda client: client.query(*args, **kwargs))

    async def query_map(
        self,
        *args: Any,
        priority: int = PRIORITY_NORMAL,
        **kwargs: Any,
    ) -> list[tuple[Any, Any]]:
        async def read(client: Any) -> list[tuple[Any, Any]]:
            result = await client.query_map(*args, **kwargs)
            pairs: list[tuple[Any, Any]] = []
            async for key, value in result:
                pairs.append((key, value))
            return pairs

        return await self.run_async(priority, read)

    async def get_events(self, block_hash: str, *, priority: int = PRIORITY_NORMAL) -> Any:
        return await self.run_async(
            priority,
            lambda client: client.get_events(block_hash=block_hash),
        )

    def rpc_request_sync(
        self,
        method: str,
        params: list[Any] | None = None,
        *,
        priority: int = PRIORITY_EVENT,
        timeout: float | None = None,
    ) -> Any:
        return self.run_sync(
            priority,
            lambda client: client.rpc_request(method, params or []),
            timeout=timeout,
        )

    def query_sync(self, *args: Any, priority: int = PRIORITY_EVENT, **kwargs: Any) -> Any:
        return self.run_sync(priority, lambda client: client.query(*args, **kwargs))

    def query_map_sync(
        self,
        *args: Any,
        priority: int = PRIORITY_EVENT,
        **kwargs: Any,
    ) -> list[tuple[Any, Any]]:
        async def read(client: Any) -> list[tuple[Any, Any]]:
            result = await client.query_map(*args, **kwargs)
            pairs: list[tuple[Any, Any]] = []
            async for key, value in result:
                pairs.append((key, value))
            return pairs

        return self.run_sync(priority, read)

    def init_runtime_sync(
        self,
        *,
        block_hash: str | None = None,
        block_id: int | None = None,
        init: bool = False,
        priority: int = PRIORITY_EVENT,
        timeout: float | None = None,
    ) -> Any:
        return self.run_sync(
            priority,
            lambda client: client.init_runtime(
                block_hash=block_hash,
                block_id=block_id,
                init=init,
            ),
            timeout=timeout,
        )

    def get_events_sync(
        self,
        block_hash: str,
        *,
        priority: int = PRIORITY_EVENT,
        timeout: float | None = None,
    ) -> Any:
        return self.run_sync(
            priority,
            lambda client: client.get_events(block_hash=block_hash),
            timeout=timeout,
        )

    def reset(self, *, reason: str = "manual reset", timeout: float = 15.0) -> dict[str, Any]:
        """Cancel the active client, fail pending work, and create a fresh connection."""

        if self._closed:
            return self.stats()
        with self._reset_lock:
            async def restart() -> None:
                worker = self._worker_task
                if worker is not None:
                    worker.cancel()
                    try:
                        await worker
                    except asyncio.CancelledError:
                        pass

                current = self._current_item
                if current is not None and not current.future.done():
                    current.future.set_exception(RuntimeError(f"shared chain reset: {reason}"))
                self._current_item = None

                queue = self._queue
                if queue is not None:
                    while not queue.empty():
                        item = queue.get_nowait()
                        if not item.future.done():
                            item.future.set_exception(
                                RuntimeError(f"shared chain reset: {reason}")
                            )

                client = self._client
                self._client = None
                self._initialized = False
                if client is not None:
                    try:
                        await asyncio.wait_for(client.close(), timeout=5.0)
                    except Exception:
                        pass

                self._queue = asyncio.PriorityQueue()
                self._worker_task = self._loop.create_task(self._worker())

            if not self._loop.is_running():
                return self.stats()
            future = asyncio.run_coroutine_threadsafe(restart(), self._loop)
            try:
                future.result(timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"shared chain reset failed: {exc}") from exc
            with self._lock:
                self._counters["resets"] += 1
        return self.stats()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(self._counters)
        queue_size = 0
        if self._loop.is_running() and self._queue is not None:
            try:
                queue_size = self._queue.qsize()
            except Exception:
                queue_size = 0
        return {
            "url": self.url,
            "running": not self._closed and self._thread.is_alive(),
            "initialized": self._initialized,
            "queue_size": queue_size,
            **counters,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        async def shutdown() -> None:
            if self._worker_task is not None:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
            if self._queue is not None:
                while not self._queue.empty():
                    item = self._queue.get_nowait()
                    if not item.future.done():
                        item.future.set_exception(RuntimeError("shared chain gateway closed"))
            if self._client is not None:
                try:
                    await self._client.close()
                except Exception:
                    pass
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        try:
            future = asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
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


_shared_gateway: SharedChainGateway | None = None
_shared_lock = threading.Lock()


def configure_shared_chain(url: str) -> SharedChainGateway:
    global _shared_gateway
    with _shared_lock:
        if _shared_gateway is not None and _shared_gateway.url == url and _shared_gateway.stats()["running"]:
            return _shared_gateway
        if _shared_gateway is not None:
            _shared_gateway.close()
        _shared_gateway = SharedChainGateway(url)
        return _shared_gateway


def get_shared_chain(url: str) -> SharedChainGateway:
    global _shared_gateway
    with _shared_lock:
        if _shared_gateway is None or not _shared_gateway.stats()["running"]:
            _shared_gateway = SharedChainGateway(url)
        elif _shared_gateway.url != url:
            raise RuntimeError(
                f"共享主链连接已配置为 {_shared_gateway.url}，不能再连接 {url}"
            )
        return _shared_gateway


def shared_chain_stats() -> dict[str, Any]:
    with _shared_lock:
        return _shared_gateway.stats() if _shared_gateway else {"running": False}


def reset_shared_chain(
    url: str | None = None,
    *,
    reason: str = "manual reset",
) -> dict[str, Any]:
    """Reset the shared connection in place without replacing its adapter object."""

    global _shared_gateway
    with _shared_lock:
        gateway = _shared_gateway
        if gateway is None and url:
            gateway = SharedChainGateway(url)
            _shared_gateway = gateway
    if gateway is None:
        return {"running": False, "resets": 0, "reason": reason}
    return {**gateway.reset(reason=reason), "reason": reason}


def shutdown_shared_chain() -> None:
    global _shared_gateway
    with _shared_lock:
        gateway = _shared_gateway
        _shared_gateway = None
    if gateway is not None:
        gateway.close()
