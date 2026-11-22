"""Keeps the latest log lines in memory for the Logs page."""

import json
import logging
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import TypedDict, cast

# Below DEBUG: messages that repeat all the time, such as heartbeats.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

# OCPP messages a charger keeps sending; their raw lines are logged as TRACE.
REPEATING_ACTIONS = {"Heartbeat", "MeterValues"}

MAX_ENTRIES = 2000  # per group: TRACE lines can't push out the other levels
MAX_MESSAGE_LENGTH = 8000

# Logger names as shown on the page; other loggers keep their own name.
SOURCE_NAMES = {
    "root": "server",
    "ocpp": "ocpp",
    "websockets.server": "websocket",
    "uvicorn.error": "web",
}


class LogEntry(TypedDict):
  id: int
  time: str  # ISO timestamp
  level: str
  level_no: int
  source: str
  message: str


class BufferHandler(logging.Handler):
  def __init__(self, capacity: int = MAX_ENTRIES) -> None:
    super().__init__()
    self.entries: deque[LogEntry] = deque(maxlen=capacity)
    self.trace_entries: deque[LogEntry] = deque(maxlen=capacity)
    self.next_id = 1
    self.setFormatter(logging.Formatter("%(message)s"))

  def emit(self, record: logging.LogRecord) -> None:
    try:
      message = self.format(record)[:MAX_MESSAGE_LENGTH]
      entry: LogEntry = {
          "id": self.next_id,
          "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
          "level": record.levelname,
          "level_no": record.levelno,
          "source": SOURCE_NAMES.get(record.name, record.name),
          "message": message,
      }
      self.next_id += 1
      (self.trace_entries if record.levelno < logging.DEBUG else self.entries).append(entry)
    except Exception:
      self.handleError(record)

  def query(
      self, min_level: int = logging.NOTSET, source: str | None = None, limit: int = 200
  ) -> list[LogEntry]:
    """Matching entries, newest first."""
    self.acquire()
    try:
      snapshot = list(self.entries)
      if min_level < logging.DEBUG:
        snapshot = sorted([*snapshot, *self.trace_entries], key=lambda e: e["id"])
    finally:
      self.release()
    found: list[LogEntry] = []
    for entry in reversed(snapshot):
      if entry["level_no"] >= min_level and (source is None or entry["source"] == source):
        found.append(entry)
        if len(found) == limit:
          break
    return found

  def sources(self) -> list[str]:
    self.acquire()
    try:
      return sorted({e["source"] for e in (*self.entries, *self.trace_entries)})
    finally:
      self.release()


def set_level(record: logging.LogRecord, level: int) -> None:
  record.levelno = level
  record.levelname = logging.getLevelName(level)


class OcppLevelFilter(logging.Filter):
  """Sets the level of the OCPP library's lines, which it logs as INFO.

  Raw messages are DEBUG, or TRACE for repeating ones. A reply doesn't name
  its action, so it is matched by the id of its call. CALLError warnings are
  DEBUG too: the code that made the call logs what the failure means.
  """

  def __init__(self) -> None:
    super().__init__()
    self.pending: OrderedDict[str, None] = OrderedDict()

  def filter(self, record: logging.LogRecord) -> bool:
    if record.msg == "Received a CALLError: %s'":
      set_level(record, logging.DEBUG)
    elif record.msg in ("%s: receive message %s", "%s: send %s"):
      set_level(record, TRACE if self.is_repeating(record) else logging.DEBUG)
    return True

  def is_repeating(self, record: logging.LogRecord) -> bool:
    args = record.args
    if not isinstance(args, tuple) or len(args) != 2 or not isinstance(args[1], str):
      return False
    try:
      parsed: object = json.loads(args[1])
    except ValueError:
      return False
    if not isinstance(parsed, list):
      return False
    frame = cast(list[object], parsed)
    if len(frame) < 3 or not isinstance(frame[1], str):
      return False
    kind, unique_id, action = frame[0], frame[1], frame[2]
    if kind == 2 and isinstance(action, str) and action in REPEATING_ACTIONS:
      self.pending[unique_id] = None
      while len(self.pending) > 1000:  # replies that never came
        self.pending.popitem(last=False)
      return True
    if kind in (3, 4) and unique_id in self.pending:
      del self.pending[unique_id]
      return True
    return False


class ServerLibraryFilter(logging.Filter):
  """websockets and uvicorn: their routine INFO lines are DEBUG, since the
  server logs its own start, stop and charger connections. A rejected
  connection is a WARNING: it is why a charger can't connect."""

  def filter(self, record: logging.LogRecord) -> bool:
    if record.levelno == logging.INFO:
      rejected = str(record.msg).startswith("connection rejected")
      set_level(record, logging.WARNING if rejected else logging.DEBUG)
    return True


buffer = BufferHandler()


def install() -> None:
  """Collects all levels for the Logs page; the console stays at INFO."""
  root = logging.getLogger()
  if buffer in root.handlers:
    return
  for handler in root.handlers:
    handler.setLevel(logging.INFO)
  root.setLevel(TRACE)
  root.addHandler(buffer)
  # These libraries are far too chatty below INFO.
  for name in ("asyncio", "websockets", "uvicorn"):
    logging.getLogger(name).setLevel(logging.INFO)
  logging.getLogger("ocpp").addFilter(OcppLevelFilter())
  for name in ("websockets.server", "uvicorn.error"):
    logging.getLogger(name).addFilter(ServerLibraryFilter())
