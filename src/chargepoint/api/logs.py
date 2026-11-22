"""The latest log lines."""

import logging
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from .. import logbuffer

router = APIRouter()

LogLevel = Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class LogsOut(BaseModel):
  entries: list[logbuffer.LogEntry]  # newest first
  sources: list[str]  # all sources currently in the buffer


@router.get("/api/logs")
async def list_logs(
    level: LogLevel = "INFO",
    source: str | None = None,
    limit: int = Query(500, ge=1, le=logbuffer.MAX_ENTRIES),
) -> LogsOut:
  """Latest log lines at `level` or higher, optionally from one source."""
  min_level = logging.getLevelNamesMapping()[level]
  return LogsOut(
      entries=logbuffer.buffer.query(min_level, source, limit),
      sources=logbuffer.buffer.sources(),
  )
