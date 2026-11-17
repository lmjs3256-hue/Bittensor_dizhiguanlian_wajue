from __future__ import annotations

import logging

from .storage import Store


class SQLiteLogHandler(logging.Handler):
    def __init__(self, store: Store, level: int = logging.INFO):
        super().__init__(level)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.store.add_log(record.levelname, record.name, record.getMessage())
        except Exception:
            # Logging must never destabilize the collector.
            self.handleError(record)
