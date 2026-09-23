"""Daily revenue report built from the payments table."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class DailyTotals:
    """Charges and refunds of one day."""

    day: date
    charged_cents: int
    refunded_cents: int

    @property
    def net_cents(self) -> int:
        """Charged minus refunded."""
        return self.charged_cents - self.refunded_cents


def build_daily_report(rows: list[tuple[date, str, int]], day: date) -> DailyTotals:
    """Sum charges and refunds for one day."""
    charged = sum(
        amount for when, kind, amount in rows if when == day and kind == "charge"
    )
    refunded = sum(
        amount for when, kind, amount in rows if when == day and kind == "refund"
    )
    return DailyTotals(day, charged, refunded)
