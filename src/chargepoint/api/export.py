"""CSV downloads."""

import csv
import io
from collections.abc import Iterable, Sequence
from datetime import datetime

from fastapi import Response


def csv_response(filename: str, header: list[str], rows: Iterable[Sequence[object]]) -> Response:
  """A CSV file download. Starts with a byte order mark so that spreadsheet
  programs read it as UTF-8 (for the € sign and names with accents)."""
  out = io.StringIO()
  writer = csv.writer(out)
  writer.writerow(header)
  writer.writerows(rows)
  return Response(
      "﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
      headers={"Content-Disposition": f'attachment; filename="{filename}"'},
  )


def local_time(timestamp: str | None) -> str:
  """A charger's ISO timestamp as server-local "YYYY-MM-DD HH:MM:SS"."""
  if not timestamp:
    return ""
  try:
    return datetime.fromisoformat(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")
  except ValueError:
    return timestamp


def number(value: float | None, decimals: int) -> str:
  return "" if value is None else f"{value:.{decimals}f}"
