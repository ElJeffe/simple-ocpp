# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""CSV downloads."""

import csv
import io
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Literal

from fastapi import Query, Response

from .. import store


CsvDecimal = Literal[".", ","]
DECIMAL = Query(None, description="Decimal separator; the page sends the one it shows")


def csv_decimal(requested: CsvDecimal | None) -> CsvDecimal:
  """The request's separator (the browser's), else the saved one, else a point."""
  if requested is not None:
    return requested
  return "," if store.localisation()["decimal_separator"] == "," else "."


def csv_response(
    filename: str, header: list[str], rows: Iterable[Sequence[object]], decimal: CsvDecimal
) -> Response:
  """A CSV file download. Starts with a byte order mark so that spreadsheet
  programs read it as UTF-8 (for the € sign and names with accents).

  With a decimal comma, columns are separated by semicolons, as spreadsheet
  programs expect in those regions.
  """
  out = io.StringIO()
  writer = csv.writer(out, delimiter=";" if decimal == "," else ",")
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


def number(value: float | None, decimals: int, decimal: CsvDecimal) -> str:
  if value is None:
    return ""
  text = f"{value:.{decimals}f}"
  return text.replace(".", ",") if decimal == "," else text
