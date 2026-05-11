from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from loguru import logger

from src.config import Config

if TYPE_CHECKING:
    from loguru import Record

_RESERVED_EXTRA_KEYS = {"module", "extras_json"}

_FORMAT = (
    "{time:YYYY-MM-DDTHH:mm:ss.SSS!UTC}Z | {level: <7} | "
    "{extra[module]} | {message}{extra[extras_json]}\n{exception}"
)


def _patch_record(record: Record) -> None:
    extras = {
        k: v
        for k, v in record["extra"].items()
        if k not in _RESERVED_EXTRA_KEYS
    }
    if extras:
        try:
            serialized = " " + json.dumps(extras, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            serialized = " " + repr(extras)
    else:
        serialized = ""
    record["extra"]["extras_json"] = serialized
    record["extra"].setdefault("module", record.get("name") or "root")


def setup_logging(config: Config) -> None:
    """Configure loguru sinks for console and file output.

    File sink rotates daily, retains 14 days, gzip-compressed. The console
    sink is gated by `config.log_level`. All sinks include an ISO-8601 UTC
    timestamp, level, module name, message, and JSON-serialized extras.

    Args:
        config: Loaded configuration.
    """
    logger.remove()
    config.logs_dir.mkdir(parents=True, exist_ok=True)

    logger.configure(
        extra={"module": "root", "extras_json": ""},
        patcher=_patch_record,
    )

    logger.add(
        sys.stderr,
        level=config.log_level,
        format=_FORMAT,
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )

    logger.add(
        str(config.logs_dir / "bot.log"),
        level="DEBUG",
        rotation="00:00",
        retention="14 days",
        compression="gz",
        format=_FORMAT,
        backtrace=True,
        diagnose=False,
        enqueue=True,
        encoding="utf-8",
    )
