from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from petroleum_rto.cdu.calibration.etl import (
    PLANT_TIMEZONE,
    EtlError,
    find_dated_row,
    is_summary_row,
    iter_dated_rows,
    parse_lab_observed_at,
    parse_lab_value,
    read_ooxml_workbook,
)

LAB_DIRECTORY = Path("data/cdu/raw/original_data/延炼300万吨常压装置/化验数据")


@pytest.mark.parametrize(
    (
        "filename",
        "physical_rows",
        "max_column",
        "dated_rows",
        "expected_sha256",
    ),
    [
        (
            "原油20230701-20240630.xlsx",
            69,
            "P",
            59,
            "06d7928ab20503c519581f5887d5567998ec173fd1bcc744a0e83f31ddf93f57",
        ),
        (
            "原油20240701-20250630.xlsx",
            62,
            "AB",
            52,
            "c89576985e8f97edf904db3bcb6d83d03e83a221e22304c5c7394e3b7036acb5",
        ),
        (
            "原油-20250701-20260615.xlsx",
            61,
            "Q",
            51,
            "bfe9f10cf0a612916b8d55e668a3038a36c84ef22fffac09dce8f2c720da65aa",
        ),
        (
            "直汽20230701-20240630.xlsx",
            411,
            "M",
            401,
            "200f6aefb5788207bf438cfcb9276002f6fe464f7d21335b87b166f38bea4974",
        ),
        (
            "直汽20240701-20250630.xlsx",
            377,
            "N",
            367,
            "54ff9ba075aca85fe6b2977b51f720dfe7eac48f9f9b8b91d2052a73fade2e67",
        ),
        (
            "直汽--20250701-20260615.xlsx",
            368,
            "L",
            358,
            "8c1a6f5600442feb9fd89a0b6b94427d79a855d3773a21bebc7bdb4b05a66522",
        ),
        (
            "常一线20230701-20240630.xlsx",
            344,
            "U",
            334,
            "92bc86d9fc8ab5863cde8bae4b0fe41993d8f3e15e1c42c68bceb6183ad7005e",
        ),
        (
            "常一线20240701-20250630.xlsx",
            282,
            "X",
            272,
            "58253edadbff0a8d6b7efda5e5c405ffa8443e6a3c64a22ea1e6f2c93ff5fcd7",
        ),
        (
            "常一线-20250701-20260615.xlsx",
            273,
            "V",
            263,
            "73d39407f334754c792d47538136d9ff6363006205a19605944e715233f2a6a7",
        ),
        (
            "混合柴油20230701-20240630.xlsx",
            387,
            "J",
            377,
            "b98fb912aaccc6ff305b20309ea18ef57a8ca1ff2be5a8b79620ccbae667ad98",
        ),
        (
            "混合柴油20240701-20500630.xlsx",
            382,
            "J",
            372,
            "dc31e252fc7bbb6a63b1eee63d689c72bee2d67e0f410cb86551d2216e00ca5a",
        ),
        (
            "混合柴油-20250701-20260615.xlsx",
            367,
            "J",
            357,
            "746a2b3f1ae7d255a160a366bef38c461dc488e845457998f8717c6a0cee4547",
        ),
    ],
)
def test_real_three_period_workbook_structure_ignores_false_a1_dimension(
    repo_root: Path,
    filename: str,
    physical_rows: int,
    max_column: str,
    dated_rows: int,
    expected_sha256: str,
) -> None:
    workbook = read_ooxml_workbook(repo_root / LAB_DIRECTORY / filename)
    sheet = workbook.first_sheet

    assert workbook.source_sha256 == expected_sha256
    assert sheet.name == "默认导出结果"
    assert sheet.reported_dimension == "A1"
    assert len(sheet.rows) == physical_rows
    assert sheet.max_row == physical_rows
    assert sheet.max_column == max_column
    assert sheet.effective_dimension == f"A1:{max_column}{physical_rows}"
    assert len(tuple(iter_dated_rows(sheet))) == dated_rows


@pytest.mark.parametrize(
    ("filename", "timestamp_cell", "value_cell", "expected_value", "expected_row"),
    [
        ("直汽--20250701-20260615.xlsx", "B349", "G349", "167.2", 349),
        ("常一线-20250701-20260615.xlsx", "B254", "S254", "230.0", 254),
        ("混合柴油-20250701-20260615.xlsx", "B348", "G348", "371.0", 348),
        ("原油-20250701-20260615.xlsx", "B52", "D52", "痕迹", 52),
    ],
)
def test_real_target_cells_and_timezone_are_stable(
    repo_root: Path,
    filename: str,
    timestamp_cell: str,
    value_cell: str,
    expected_value: str,
    expected_row: int,
) -> None:
    sheet = read_ooxml_workbook(repo_root / LAB_DIRECTORY / filename).first_sheet
    timestamp = sheet.require_text(timestamp_cell)
    observed_at = parse_lab_observed_at(timestamp)

    assert observed_at.tzinfo == PLANT_TIMEZONE
    utc_offset = observed_at.utcoffset()
    assert utc_offset is not None
    assert utc_offset.total_seconds() == 8 * 3600
    assert find_dated_row(sheet, observed_at).row_number == expected_row
    assert sheet.require_text(value_cell) == expected_value


@pytest.mark.parametrize(
    ("raw_text", "kind", "value"),
    [
        ("", "missing", None),
        ("/", "missing", None),
        ("痕迹", "trace", None),
        ("<0.05", "upper_bound", 0.05),
        ("≤365", "upper_bound", 365.0),
        ("+7", "explicit_positive", 7.0),
        ("-5", "exact", -5.0),
        ("汇总文本", "text", None),
    ],
)
def test_lab_scalar_semantics_are_not_silently_coerced(
    raw_text: str,
    kind: str,
    value: float | None,
) -> None:
    parsed = parse_lab_value(raw_text)

    assert parsed.raw_text == raw_text
    assert parsed.kind == kind
    assert parsed.value == value


def test_real_trace_limit_plus_and_summary_rows_are_classified(repo_root: Path) -> None:
    crude = read_ooxml_workbook(
        repo_root / LAB_DIRECTORY / "原油-20250701-20260615.xlsx"
    ).first_sheet
    diesel = read_ooxml_workbook(
        repo_root / LAB_DIRECTORY / "混合柴油-20250701-20260615.xlsx"
    ).first_sheet

    assert parse_lab_value(crude.require_text("D52")).kind == "trace"
    assert parse_lab_value(diesel.require_text("G3")).kind == "upper_bound"
    assert parse_lab_value(diesel.require_text("I348")).kind == "explicit_positive"
    summary = next(row for row in diesel.rows if row.row_number == 363)
    assert is_summary_row(summary)
    assert 363 not in {row.row_number for _, row in iter_dated_rows(diesel)}


def test_invalid_lab_timestamps_are_rejected() -> None:
    with pytest.raises(EtlError, match="invalid laboratory timestamp"):
        parse_lab_observed_at("2026-02-30 08:00:00")
    with pytest.raises(EtlError, match="invalid laboratory timestamp"):
        parse_lab_observed_at("2026-06-04T08:00:00")


def test_find_dated_row_requires_aware_time(repo_root: Path) -> None:
    sheet = read_ooxml_workbook(
        repo_root / LAB_DIRECTORY / "直汽--20250701-20260615.xlsx"
    ).first_sheet

    with pytest.raises(EtlError, match="timezone"):
        find_dated_row(
            sheet,
            datetime(2026, 6, 4, 8, 0, 0, tzinfo=PLANT_TIMEZONE).replace(tzinfo=None),
        )
