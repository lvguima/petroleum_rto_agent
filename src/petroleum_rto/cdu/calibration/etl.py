"""Read-only, narrow ETL helpers for the source laboratory OOXML workbooks.

The plant workbooks used by M5 advertise ``dimension=A1`` even though their
worksheet XML contains hundreds of rows.  This module deliberately walks
``sheetData`` instead of trusting that advisory dimension.  It supports only
the OOXML cell encodings present in the source files and never writes to them.
"""

from __future__ import annotations

import hashlib
import math
import posixpath
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


class EtlError(ValueError):
    """Raised when a source workbook cannot be read without guessing."""


LabValueKind = Literal[
    "exact",
    "explicit_positive",
    "upper_bound",
    "trace",
    "missing",
    "text",
]

PLANT_TIMEZONE = timezone(timedelta(hours=8), name="UTC+08:00")

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REFERENCE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
_PLAIN_NUMBER = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?$")
_UPPER_BOUND = re.compile(
    r"^(?:<|<=|＜|≤)\s*([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?)$"
)
_SUMMARY_LABELS = frozenset(
    {
        "指标集",
        "最大值",
        "最小值",
        "平均值",
        "算术平均值",
        "合格次数",
        "不合格次数",
    }
)


def _tag(local_name: str) -> str:
    return f"{{{_MAIN_NS}}}{local_name}"


def file_sha256(path: Path) -> str:
    """Return the lowercase SHA-256 digest of one source file."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise EtlError(f"cannot read source file {path}: {exc}") from exc
    return digest.hexdigest()


def column_name_to_index(name: str) -> int:
    """Convert an OOXML column name to its one-based index."""

    if not name or not name.isascii() or not name.isalpha() or not name.isupper():
        raise EtlError(f"invalid OOXML column name {name!r}")
    result = 0
    for character in name:
        result = result * 26 + ord(character) - ord("A") + 1
    if result > 16384:
        raise EtlError(f"OOXML column exceeds XFD: {name!r}")
    return result


def column_index_to_name(index: int) -> str:
    """Convert a one-based column index to an OOXML column name."""

    if isinstance(index, bool) or not 1 <= index <= 16384:
        raise EtlError(f"invalid OOXML column index {index!r}")
    characters: list[str] = []
    remaining = index
    while remaining:
        remaining, offset = divmod(remaining - 1, 26)
        characters.append(chr(ord("A") + offset))
    return "".join(reversed(characters))


def split_cell_reference(reference: str) -> tuple[str, int]:
    """Split a strict A1-style cell reference."""

    match = _CELL_REFERENCE.fullmatch(reference)
    if match is None:
        raise EtlError(f"invalid OOXML cell reference {reference!r}")
    column = match.group(1)
    column_name_to_index(column)
    return column, int(match.group(2))


@dataclass(frozen=True)
class OoxmlCell:
    """One decoded worksheet cell, retaining its raw OOXML metadata."""

    reference: str
    text: str
    data_type: str | None
    style_id: int | None

    @property
    def column(self) -> str:
        return split_cell_reference(self.reference)[0]

    @property
    def row_number(self) -> int:
        return split_cell_reference(self.reference)[1]


@dataclass(frozen=True)
class OoxmlRow:
    """One physical ``sheetData`` row."""

    row_number: int
    cells: tuple[OoxmlCell, ...]

    def cell(self, column: str) -> OoxmlCell | None:
        """Return the cell in ``column``, including explicitly blank cells."""

        column_name_to_index(column)
        for cell in self.cells:
            if cell.column == column:
                return cell
        return None

    def text(self, column: str) -> str:
        """Return decoded text, treating an absent cell as blank."""

        cell = self.cell(column)
        return "" if cell is None else cell.text


@dataclass(frozen=True)
class OoxmlSheet:
    """One worksheet decoded independently of its unreliable dimension hint."""

    name: str
    package_path: str
    reported_dimension: str | None
    rows: tuple[OoxmlRow, ...]
    max_row: int
    max_column: str

    @property
    def effective_dimension(self) -> str:
        """Return the range actually observed in ``sheetData``."""

        if self.max_row == 0:
            return "A1"
        return f"A1:{self.max_column}{self.max_row}"

    def cell(self, reference: str) -> OoxmlCell | None:
        """Return one cell by A1 reference."""

        column, row_number = split_cell_reference(reference)
        for row in self.rows:
            if row.row_number == row_number:
                return row.cell(column)
        return None

    def require_text(self, reference: str) -> str:
        """Return one cell's text or fail with a stable source locator."""

        cell = self.cell(reference)
        if cell is None:
            raise EtlError(f"worksheet {self.name!r} does not contain {reference}")
        return cell.text


@dataclass(frozen=True)
class OoxmlWorkbook:
    """Read-only representation of the narrow workbook subset used by M5."""

    source_path: Path
    source_sha256: str
    sheets: tuple[OoxmlSheet, ...]

    def sheet(self, name: str) -> OoxmlSheet:
        """Return a uniquely named worksheet."""

        matches = [sheet for sheet in self.sheets if sheet.name == name]
        if len(matches) != 1:
            raise EtlError(f"workbook must contain exactly one sheet named {name!r}")
        return matches[0]

    @property
    def first_sheet(self) -> OoxmlSheet:
        """Return the first worksheet, rejecting an empty workbook."""

        if not self.sheets:
            raise EtlError("workbook contains no worksheets")
        return self.sheets[0]


def _read_xml(archive: ZipFile, package_path: str) -> ElementTree.Element:
    try:
        payload = archive.read(package_path)
    except KeyError as exc:
        raise EtlError(f"OOXML package is missing {package_path}") from exc
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise EtlError(f"invalid XML in OOXML part {package_path}: {exc}") from exc


def _shared_strings(archive: ZipFile) -> tuple[str, ...]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return ()
    root = _read_xml(archive, "xl/sharedStrings.xml")
    values: list[str] = []
    for item in root.findall(_tag("si")):
        values.append("".join(node.text or "" for node in item.iter(_tag("t"))))
    return tuple(values)


def _worksheet_paths(archive: ZipFile) -> tuple[tuple[str, str], ...]:
    workbook = _read_xml(archive, "xl/workbook.xml")
    relationships = _read_xml(archive, "xl/_rels/workbook.xml.rels")
    targets: dict[str, str] = {}
    for relationship in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship"):
        relationship_id = relationship.attrib.get("Id")
        target = relationship.attrib.get("Target")
        if relationship_id and target and relationship.attrib.get("TargetMode") != "External":
            targets[relationship_id] = target

    sheets = workbook.find(_tag("sheets"))
    if sheets is None:
        raise EtlError("OOXML workbook is missing its sheets collection")
    result: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    for sheet in sheets.findall(_tag("sheet")):
        name = sheet.attrib.get("name", "").strip()
        relationship_id = sheet.attrib.get(f"{{{_OFFICE_REL_NS}}}id")
        if not name or relationship_id not in targets:
            raise EtlError("OOXML worksheet has no name or internal relationship")
        if name in seen_names:
            raise EtlError(f"duplicate OOXML worksheet name {name!r}")
        target = targets[relationship_id]
        if target.startswith("/"):
            package_path = posixpath.normpath(target.lstrip("/"))
        else:
            package_path = posixpath.normpath(posixpath.join("xl", target))
        if package_path == "xl" or not package_path.startswith("xl/") or ".." in package_path.split("/"):
            raise EtlError(f"worksheet relationship escapes the OOXML package: {target!r}")
        result.append((name, package_path))
        seen_names.add(name)
    return tuple(result)


def _decode_cell(cell: ElementTree.Element, shared_strings: tuple[str, ...]) -> OoxmlCell:
    reference = cell.attrib.get("r", "")
    split_cell_reference(reference)
    data_type = cell.attrib.get("t")
    style_text = cell.attrib.get("s")
    try:
        style_id = None if style_text is None else int(style_text)
    except ValueError as exc:
        raise EtlError(f"invalid style id at {reference}") from exc
    if style_id is not None and style_id < 0:
        raise EtlError(f"negative style id at {reference}")

    value_node = cell.find(_tag("v"))
    if data_type == "inlineStr":
        inline = cell.find(_tag("is"))
        text = "" if inline is None else "".join(
            node.text or "" for node in inline.iter(_tag("t"))
        )
    elif value_node is None or value_node.text is None:
        text = ""
    elif data_type == "s":
        try:
            shared_index = int(value_node.text)
            text = shared_strings[shared_index]
        except (ValueError, IndexError) as exc:
            raise EtlError(f"invalid shared-string index at {reference}") from exc
    else:
        text = value_node.text
    return OoxmlCell(reference, text, data_type, style_id)


def _read_sheet(
    archive: ZipFile,
    *,
    name: str,
    package_path: str,
    shared_strings: tuple[str, ...],
) -> OoxmlSheet:
    root = _read_xml(archive, package_path)
    dimension_node = root.find(_tag("dimension"))
    reported_dimension = None if dimension_node is None else dimension_node.attrib.get("ref")
    sheet_data = root.find(_tag("sheetData"))
    if sheet_data is None:
        raise EtlError(f"worksheet {name!r} is missing sheetData")

    rows: list[OoxmlRow] = []
    seen_rows: set[int] = set()
    max_row = 0
    max_column_index = 0
    for row_node in sheet_data.findall(_tag("row")):
        row_text = row_node.attrib.get("r")
        try:
            row_number = int(row_text) if row_text is not None else 0
        except ValueError as exc:
            raise EtlError(f"worksheet {name!r} contains an invalid row number") from exc
        if row_number <= 0 or row_number in seen_rows:
            raise EtlError(f"worksheet {name!r} contains a duplicate or invalid row {row_number}")
        cells: list[OoxmlCell] = []
        seen_columns: set[str] = set()
        for cell_node in row_node.findall(_tag("c")):
            cell = _decode_cell(cell_node, shared_strings)
            if cell.row_number != row_number:
                raise EtlError(f"cell {cell.reference} does not belong to row {row_number}")
            if cell.column in seen_columns:
                raise EtlError(f"duplicate cell in column {cell.column} on row {row_number}")
            cells.append(cell)
            seen_columns.add(cell.column)
            max_column_index = max(max_column_index, column_name_to_index(cell.column))
        cells.sort(key=lambda item: column_name_to_index(item.column))
        rows.append(OoxmlRow(row_number, tuple(cells)))
        seen_rows.add(row_number)
        max_row = max(max_row, row_number)
    rows.sort(key=lambda item: item.row_number)
    max_column = "A" if max_column_index == 0 else column_index_to_name(max_column_index)
    return OoxmlSheet(
        name=name,
        package_path=package_path,
        reported_dimension=reported_dimension,
        rows=tuple(rows),
        max_row=max_row,
        max_column=max_column,
    )


def read_ooxml_workbook(path: Path) -> OoxmlWorkbook:
    """Read the source workbook without trusting cached dimensions or formulas."""

    digest = file_sha256(path)
    try:
        with ZipFile(path, "r") as archive:
            shared_strings = _shared_strings(archive)
            sheets = tuple(
                _read_sheet(
                    archive,
                    name=name,
                    package_path=package_path,
                    shared_strings=shared_strings,
                )
                for name, package_path in _worksheet_paths(archive)
            )
    except (OSError, BadZipFile) as exc:
        raise EtlError(f"cannot read OOXML workbook {path}: {exc}") from exc
    if not sheets:
        raise EtlError(f"OOXML workbook has no worksheets: {path}")
    return OoxmlWorkbook(path, digest, sheets)


@dataclass(frozen=True)
class ParsedLabValue:
    """A source cell classified without silently inventing a numeric value."""

    raw_text: str
    normalized_text: str
    value: float | None
    kind: LabValueKind


def _finite_number(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise EtlError(f"laboratory value must be finite: {text!r}")
    return value


def parse_lab_value(raw_text: str | None) -> ParsedLabValue:
    """Classify exact, censored, trace, missing, signed, and text cells.

    ``痕迹`` is not converted to zero, ``/`` remains missing, and a ``<``
    result retains its censoring qualifier.  A leading plus sign is numeric but
    remains distinguishable from an unsigned result.
    """

    raw = "" if raw_text is None else raw_text
    normalized = raw.strip()
    if normalized in {"", "/"}:
        return ParsedLabValue(raw, normalized, None, "missing")
    if normalized.casefold() in {"痕迹", "trace"}:
        return ParsedLabValue(raw, normalized, None, "trace")
    upper_match = _UPPER_BOUND.fullmatch(normalized)
    if upper_match is not None:
        return ParsedLabValue(raw, normalized, _finite_number(upper_match.group(1)), "upper_bound")
    if normalized.startswith("+") and _PLAIN_NUMBER.fullmatch(normalized):
        return ParsedLabValue(raw, normalized, _finite_number(normalized), "explicit_positive")
    if _PLAIN_NUMBER.fullmatch(normalized):
        return ParsedLabValue(raw, normalized, _finite_number(normalized), "exact")
    return ParsedLabValue(raw, normalized, None, "text")


def parse_lab_observed_at(raw_text: str, *, source_timezone: tzinfo = PLANT_TIMEZONE) -> datetime:
    """Parse the plant's local laboratory timestamp and attach its declared zone."""

    normalized = raw_text.strip()
    match = re.fullmatch(
        r"([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})",
        normalized,
    )
    if match is None:
        raise EtlError(f"invalid laboratory timestamp {raw_text!r}")
    year, month, day, hour, minute, second = (int(field) for field in match.groups())
    try:
        result = datetime(year, month, day, hour, minute, second, tzinfo=source_timezone)
    except ValueError as exc:
        raise EtlError(f"invalid laboratory timestamp {raw_text!r}") from exc
    if result.utcoffset() is None:
        raise EtlError("source_timezone must provide a UTC offset")
    return result


def iter_dated_rows(
    sheet: OoxmlSheet,
    *,
    timestamp_column: str = "B",
    source_timezone: tzinfo = PLANT_TIMEZONE,
) -> Iterator[tuple[datetime, OoxmlRow]]:
    """Yield only real dated samples, excluding headers and summary rows."""

    column_name_to_index(timestamp_column)
    for row in sheet.rows:
        text = row.text(timestamp_column).strip()
        if not text:
            continue
        try:
            observed_at = parse_lab_observed_at(text, source_timezone=source_timezone)
        except EtlError:
            continue
        yield observed_at, row


def find_dated_row(
    sheet: OoxmlSheet,
    observed_at: datetime,
    *,
    timestamp_column: str = "B",
) -> OoxmlRow:
    """Return a unique laboratory row at an aware timestamp."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise EtlError("observed_at must include a timezone")
    matches = [
        row
        for candidate, row in iter_dated_rows(
            sheet,
            timestamp_column=timestamp_column,
            source_timezone=observed_at.tzinfo,
        )
        if candidate == observed_at
    ]
    if len(matches) != 1:
        raise EtlError(
            f"expected one laboratory row at {observed_at.isoformat()}, found {len(matches)}"
        )
    return matches[0]


def is_summary_label(raw_text: str) -> bool:
    """Recognize the summary labels present in the plant exports."""

    normalized = raw_text.strip()
    return normalized in _SUMMARY_LABELS or normalized.endswith("合计")


def is_summary_row(sheet_row: OoxmlRow, *, label_column: str = "B") -> bool:
    """Return whether the row is a known non-observation summary row."""

    return is_summary_label(sheet_row.text(label_column))
