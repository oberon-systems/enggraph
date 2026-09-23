"""Write reports out as CSV."""

from __future__ import annotations

import csv
import io

from worker.reports.daily import DailyTotals


def export_csv(reports: list[DailyTotals]) -> str:
    """Render daily totals as CSV text."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["day", "charged_cents", "refunded_cents", "net_cents"])
    for report in reports:
        writer.writerow(
            [
                report.day.isoformat(),
                report.charged_cents,
                report.refunded_cents,
                report.net_cents,
            ]
        )
    return out.getvalue()
