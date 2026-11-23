# SPDX-FileCopyrightText: 2026 Jef Steelant
# SPDX-License-Identifier: GPL-3.0-only

"""A small PDF writer for one-table reports, so exports need no extra package.

Uses the Helvetica font every PDF viewer has built in. That font covers
Western European text (Windows-1252); other characters are shown as "?".
"""

from collections.abc import Sequence

PAGE_WIDTH, PAGE_HEIGHT = 595, 842  # A4, in points
MARGIN = 50
ROW_HEIGHT = 20
VALUE_COLUMN_WIDTH = 105

# Helvetica character widths for codes 32 to 126, in 1/1000 of the font size.
_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)
_EM_DASH = 0x97


def _width(text: bytes, size: float) -> float:
  def char(code: int) -> int:
    if code == _EM_DASH:
      return 1000
    return _WIDTHS[code - 32] if 32 <= code <= 126 else 556
  return sum(char(code) for code in text) * size / 1000


def _text(
    x: float, y: float, text: str, size: float = 10, font: str = "F1", right: bool = False
) -> bytes:
  data = text.encode("cp1252", "replace")
  if right:
    x -= _width(data, size)
  escaped = data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
  escaped = escaped.replace(b"\r", b" ").replace(b"\n", b" ")
  return b"BT /%s %g Tf %.2f %.2f Td (%s) Tj ET\n" % (font.encode(), size, x, y, escaped)


def _rule(y: float, width: float = 0.5) -> bytes:
  return b"%g w %d %.2f m %d %.2f l S\n" % (width, MARGIN, y, PAGE_WIDTH - MARGIN, y)


def _row(y: float, cells: Sequence[str], font: str = "F1") -> bytes:
  out = _text(MARGIN + 4, y, cells[0], font=font)
  values = cells[1:]
  for i, cell in enumerate(values):
    edge = PAGE_WIDTH - MARGIN - 4 - (len(values) - 1 - i) * VALUE_COLUMN_WIDTH
    out += _text(edge, y, cell, font=font, right=True)
  return out


def table_pdf(
    title: str, details: Sequence[str], header: Sequence[str],
    rows: Sequence[Sequence[str]], total: Sequence[str],
) -> bytes:
  """A report: title, detail lines, then a table that continues over pages.

  The first column is left-aligned, the others right-aligned.
  """
  bottom = MARGIN + 34
  pages: list[bytes] = []
  y = PAGE_HEIGHT - MARGIN - 18
  page = _text(MARGIN, y, title, size=20, font="F2")
  y -= 10
  for line in details:
    y -= 16
    page += _text(MARGIN, y, line)
  y -= 16

  def table_head(at: float) -> tuple[bytes, float]:
    at -= ROW_HEIGHT
    return _row(at, header, "F2") + _rule(at - 6, 1), at - 6

  head, y = table_head(y)
  page += head
  for cells in rows:
    if y - ROW_HEIGHT * 2 < bottom:  # always leave room for the total
      pages.append(page)
      head, y = table_head(PAGE_HEIGHT - MARGIN)
      page = head
    y -= ROW_HEIGHT
    page += _row(y, cells) + _rule(y - 6, 0.25)
  y -= ROW_HEIGHT
  page += _row(y, total, "F2")
  pages.append(page)

  count = len(pages)
  pages = [
      content + _text(MARGIN, MARGIN - 10, "simple-ocpp", size=8)
      + _text(PAGE_WIDTH - MARGIN, MARGIN - 10, f"Page {i} of {count}", size=8, right=True)
      for i, content in enumerate(pages, 1)
  ]
  return _document(pages)


def _document(contents: list[bytes]) -> bytes:
  """Objects: 1 catalog, 2 page tree, 3-4 fonts, then a page and content per page."""
  page_ids = [5 + 2 * i for i in range(len(contents))]
  kids = b" ".join(b"%d 0 R" % i for i in page_ids)
  font = b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
  objects = [
      b"<< /Type /Catalog /Pages 2 0 R >>",
      b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(contents)),
      font % b"Helvetica",
      font % b"Helvetica-Bold",
  ]
  for page_id, content in zip(page_ids, contents):
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R"
        b" /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>"
        % (PAGE_WIDTH, PAGE_HEIGHT, page_id + 1)
    )
    objects.append(b"<< /Length %d >>\nstream\n%sendstream" % (len(content), content))

  out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
  offsets: list[int] = []
  for number, body in enumerate(objects, 1):
    offsets.append(len(out))
    out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
  xref = len(out)
  out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
  for offset in offsets:
    out += b"%010d 00000 n \n" % offset
  out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
  return bytes(out)
