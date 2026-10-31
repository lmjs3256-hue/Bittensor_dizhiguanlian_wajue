from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT_DIR / "data"
DEFAULT_RPC_URL = "wss://entrypoint-finney.opentensor.ai:443"
SYSTEM_PREFIX = "5EYCAe5"


def database_path() -> Path:
    """Return a local SQLite path; tests can inject an isolated database."""

    value = os.getenv("WALLET_LINK_DB")
    # Routers can expose non-file providers with this scheme. Reject it here
    # rather than silently opening a pseudo-file with a misleading name.
    if value and value not in {"sqlite://", "sqlite:///:memory:"}:
        if value.startswith("sqlite:///"):
            return Path(value[10:])
        if value.startswith("sqlite://"):
            raise ValueError("WALLET_LINK_DB must be a plain filesystem path")
        return Path(value)
    return DEFAULT_DATA_DIR / "wallet_link.db"


@dataclass(frozen=True)
class Settings:
    rpc_url: str = os.getenv("WALLET_LINK_RPC_URL", DEFAULT_RPC_URL)
    scan_batch_blocks: int = 4
    scan_batch_seconds: float = 0.2
    rpc_timeout_seconds: float = 25.0


def settings() -> Settings:
    return Settings()
