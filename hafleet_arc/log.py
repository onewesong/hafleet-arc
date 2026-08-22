"""统一的 HAFleet ARC 控制台日志输出。"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def log(message: str, *args: Any, **kwargs: Any) -> None:
    """Print one flushed log line with a local timezone timestamp.

    ``*args``/``**kwargs`` are accepted so callers can migrate from ``print``
    without changing unrelated call sites; logging always flushes immediately.
    """

    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)

