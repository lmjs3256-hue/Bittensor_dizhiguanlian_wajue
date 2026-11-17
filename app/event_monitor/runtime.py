"""Embedded Subnet Pulse runtime used by the merged Wallet Link service."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import DEFAULT_RPC_URL, ROOT_DIR, event_database_path
from ..shared_chain import shared_chain_stats
from .chain import ChainCollector
from .github import GitHubCollector
from .legacy_inherit import default_legacy_event_db, inherit_legacy_event_state
from .log_store import SQLiteLogHandler
from .rules import load_rules, load_simulation_signals
from .server import AppContext, EventHub, MonitorHTTPServer, WorkerStatus
from .storage import Store


PACKAGE_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = PACKAGE_ROOT / "config"
STATIC_DIR = PACKAGE_ROOT / "static"


@dataclass(frozen=True)
class RuntimeSettings:
    database: Path
    rules: Path
    product_info: Path
    static_dir: Path
    chain_enabled: bool
    github_enabled: bool
    simulation_enabled: bool
    demo_enabled: bool
    chain_rpc: str
    network: str
    chain_poll_seconds: float
    profile_sync_seconds: int
    github_poll_seconds: int
    env_path: Path
    legacy_db: Path | None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def load_env_file(path: Path) -> None:
    """Load the small KEY=VALUE subset used by the event monitor."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and not os.getenv(key):
            os.environ[key] = value


def settings_from_env(
    *,
    database: Path | None = None,
    chain_enabled: bool | None = None,
    github_enabled: bool | None = None,
    chain_rpc: str | None = None,
) -> RuntimeSettings:
    env_path = ROOT_DIR / ".env"
    load_env_file(env_path)
    inherit_legacy = _env_bool("WALLET_LINK_EVENT_INHERIT_LEGACY", True)
    legacy_value = os.getenv("WALLET_LINK_EVENT_LEGACY_DB", "").strip()
    legacy_db = (
        Path(legacy_value).expanduser().resolve()
        if legacy_value
        else (default_legacy_event_db(ROOT_DIR) if inherit_legacy else None)
    )
    return RuntimeSettings(
        database=(database or event_database_path()).resolve(),
        rules=CONFIG_DIR / "event_rules.json",
        product_info=CONFIG_DIR / "product_info.json",
        static_dir=STATIC_DIR,
        chain_enabled=(
            _env_bool("WALLET_LINK_EVENT_CHAIN_ENABLED", True)
            if chain_enabled is None else bool(chain_enabled)
        ),
        github_enabled=(
            _env_bool("WALLET_LINK_EVENT_GITHUB_ENABLED", True)
            if github_enabled is None else bool(github_enabled)
        ),
        simulation_enabled=_env_bool("WALLET_LINK_EVENT_SIMULATION_ENABLED", True),
        demo_enabled=_env_bool("WALLET_LINK_EVENT_DEMO_ENABLED", False),
        chain_rpc=(
            chain_rpc
            or os.getenv("WALLET_LINK_RPC_URL")
            or DEFAULT_RPC_URL
        ),
        network=os.getenv("WALLET_LINK_EVENT_NETWORK", "finney"),
        chain_poll_seconds=float(os.getenv("WALLET_LINK_EVENT_CHAIN_POLL_SECONDS", "2")),
        profile_sync_seconds=int(os.getenv("WALLET_LINK_EVENT_PROFILE_SYNC_SECONDS", "604800")),
        github_poll_seconds=int(os.getenv("WALLET_LINK_EVENT_GITHUB_POLL_SECONDS", "300")),
        env_path=env_path,
        legacy_db=legacy_db,
    )


class EventMonitorRuntime:
    """Own the collectors and a loopback-only HTTP API for the event module.

    The public Wallet Link service proxies ``/event-api/*`` to this server.  The
    internal socket is bound to a random loopback port and never exposed
    directly, so remote clients still pass through the main access guard.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._server: MonitorHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._workers: list[tuple[str, Any]] = []
        self._worker_threads: list[threading.Thread] = []
        self._log_handler: SQLiteLogHandler | None = None
        self.context: AppContext | None = None
        self.port: int | None = None
        self.error: str | None = None
        self.settings: RuntimeSettings | None = None
        self.legacy_inheritance: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return bool(self._server and self._thread and self._thread.is_alive() and not self.error)

    def start(self, settings: RuntimeSettings) -> None:
        with self._lock:
            if self.running:
                return
            self._cleanup_locked()
            self.error = None
            self.settings = settings
            try:
                context, workers = self._create_application(settings)
                handler = SQLiteLogHandler(context.store)
                handler.setFormatter(logging.Formatter("%(message)s"))
                logger = logging.getLogger("app.event_monitor")
                logger.addHandler(handler)
                self._log_handler = handler

                stop = threading.Event()
                worker_threads: list[threading.Thread] = []
                for name, worker in workers:
                    thread = threading.Thread(
                        target=worker.run,
                        args=(stop,),
                        name=f"event-monitor-{name}",
                        daemon=True,
                    )
                    thread.start()
                    worker_threads.append(thread)

                self.context = context
                self._workers = workers
                self._worker_threads = worker_threads
                self._stop = stop

                server = MonitorHTTPServer(("127.0.0.1", 0), context)
                thread = threading.Thread(
                    target=server.serve_forever,
                    kwargs={"poll_interval": 0.5},
                    name="event-monitor-http",
                    daemon=True,
                )
                thread.start()
                self._server = server
                self._thread = thread
                self.port = int(server.server_address[1])
            except Exception as exc:  # noqa: BLE001 - merged service must still start
                self.error = str(exc)
                self._cleanup_locked()
                raise

    def stop(self) -> None:
        with self._lock:
            self._cleanup_locked()

    def health(self) -> dict[str, Any]:
        context = self.context
        return {
            "running": self.running,
            "port": self.port,
            "error": self.error,
            "settings": {
                "database": str(self.settings.database) if self.settings else "",
                "chain_enabled": self.settings.chain_enabled if self.settings else False,
                "github_enabled": self.settings.github_enabled if self.settings else False,
                "network": self.settings.network if self.settings else "",
            },
            "workers": context.status.snapshot() if context else {},
            "shared_chain": shared_chain_stats(),
            "legacy_inheritance": self.legacy_inheritance,
        }

    def force_chain_reconnect(self) -> dict[str, Any]:
        """Ask the chain collector to reconnect after a shared-gateway reset."""

        with self._lock:
            for name, worker in self._workers:
                if name != "chain":
                    continue
                reconnect = getattr(worker, "force_reconnect", None)
                if callable(reconnect):
                    return {"reset": True, **reconnect()}
            return {"reset": False, "state": "unavailable"}

    def _create_application(self, settings: RuntimeSettings) -> tuple[AppContext, list[tuple[str, Any]]]:
        store = Store(settings.database)
        if settings.legacy_db is not None and settings.legacy_db != settings.database:
            try:
                self.legacy_inheritance = inherit_legacy_event_state(
                    settings.database,
                    settings.legacy_db,
                )
            except Exception as exc:  # noqa: BLE001 - inheritance must not block startup
                self.legacy_inheritance = {
                    "inherited": False,
                    "source": str(settings.legacy_db),
                    "error": str(exc),
                }
        hub = EventHub()
        status = WorkerStatus()
        rules, _catalog = load_rules(settings.rules)
        simulation_signals = load_simulation_signals(settings.rules)
        product_info = json.loads(settings.product_info.read_text(encoding="utf-8"))
        github_token_configured = bool(os.getenv("GITHUB_TOKEN", "").strip())
        github_batch_size = 30 if github_token_configured else 1
        context = AppContext(
            store=store,
            hub=hub,
            status=status,
            static_dir=settings.static_dir,
            product_info=product_info,
            simulation_enabled=settings.simulation_enabled,
            demo_enabled=settings.demo_enabled,
            simulation_signals=simulation_signals,
            env_path=settings.env_path,
            runtime_config={
                "chain_poll_seconds": settings.chain_poll_seconds,
                "profile_sync_seconds": settings.profile_sync_seconds,
                "github_poll_seconds": settings.github_poll_seconds,
                "github_batch_size": github_batch_size,
                "github_token_configured": github_token_configured,
                "trading_poll_seconds": 5,
                "priority_order": ["重点监控", "优先监控", "普通监控"],
                "embedded_in": "bittensor-wallet-link-intel",
            },
        )
        workers: list[tuple[str, Any]] = []
        if settings.chain_enabled:
            workers.append(("chain", ChainCollector(
                url=settings.chain_rpc,
                network=settings.network,
                store=store,
                rules=rules,
                publish=hub.publish,
                status=status.set,
                poll_seconds=settings.chain_poll_seconds,
                profile_sync_seconds=settings.profile_sync_seconds,
            )))
        else:
            status.set("chain", "disabled", {"message": "链上采集已关闭"})
        if settings.github_enabled:
            workers.append(("github", GitHubCollector(
                store=store,
                publish=hub.publish,
                status=status.set,
                interval_seconds=settings.github_poll_seconds,
                network=settings.network,
                max_repositories_per_poll=github_batch_size,
            )))
        else:
            status.set("github", "disabled", {"message": "GitHub 采集已关闭"})
        for worker_name, worker in workers:
            if worker_name == "chain":
                context.request_profile_sync = worker.start_repository_sync
            elif worker_name == "github":
                context.github_token_update = worker.update_token
        return context, workers

    def _cleanup_locked(self) -> None:
        stop = self._stop
        stop.set()
        for _, worker in self._workers:
            close = getattr(worker, "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass
        for thread in self._worker_threads:
            if thread.is_alive():
                thread.join(timeout=3)
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        if self.context is not None:
            try:
                self.context.hub.close()
            except Exception:
                pass
        if self._log_handler is not None:
            logging.getLogger("app.event_monitor").removeHandler(self._log_handler)
        self._server = None
        self._thread = None
        self._workers = []
        self._worker_threads = []
        self._log_handler = None
        self.context = None
        self.port = None
        self._stop = threading.Event()


EVENT_MONITOR = EventMonitorRuntime()
