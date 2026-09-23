"""Tests for the daily revenue report."""

from datetime import date

from worker.reports.daily import build_daily_report
from worker.reports.export import export_csv


def test_net_is_charges_minus_refunds() -> None:
    """Net revenue subtracts refunds."""
    day = date(2024, 1, 2)
    report = build_daily_report([(day, "charge", 500), (day, "refund", 200)], day)
    assert report.net_cents == 300


def test_export_has_header() -> None:
    """The CSV export starts with a header row."""
    assert export_csv([]).startswith("day,")
