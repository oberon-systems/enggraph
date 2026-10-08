"""Write JSON the way the previous server did, character for character.

An answer is text an agent reads and a test compares, so a number has to be
spelled as JavaScript spells it: `1`, not `1.0`, and `1e-7`, not `1e-07`.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


def fixed(value: float, places: int) -> float:
    """Round as JavaScript's `Number(value.toFixed(places))` does."""
    # toFixed takes the larger of two equally near results; format() the even.
    step = Decimal(1).scaleb(-places)
    return float(Decimal(value).quantize(step, rounding=ROUND_HALF_UP))


def number(value: float) -> str:
    """Spell a float as JavaScript's Number.prototype.toString does."""
    if math.isnan(value) or math.isinf(value):
        return "null"
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    parts = Decimal(repr(abs(value))).as_tuple()
    digits = "".join(str(digit) for digit in parts.digits).lstrip("0")
    exponent = int(parts.exponent)
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    digits = stripped
    count = len(digits)
    point = count + exponent
    if count <= point <= 21:
        return sign + digits + "0" * (point - count)
    if 0 < point <= 21:
        return f"{sign}{digits[:point]}.{digits[point:]}"
    if -6 < point <= 0:
        return f"{sign}0.{'0' * -point}{digits}"
    power = point - 1
    mantissa = digits[0] + ("." + digits[1:] if count > 1 else "")
    return f"{sign}{mantissa}e{'+' if power > 0 else '-'}{abs(power)}"


def instant(value: datetime) -> str:
    """Spell a timestamp as Date.prototype.toISOString does."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    moment = value.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _write(value: Any, indent: int, depth: int) -> str:  # noqa: ANN401
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return number(value)
    if isinstance(value, Decimal):
        return json.dumps(str(value))
    if isinstance(value, datetime):
        return json.dumps(instant(value))
    if isinstance(value, date):
        return json.dumps(instant(datetime(value.year, value.month, value.day)))
    inner = "\n" + " " * (indent * (depth + 1)) if indent else ""
    outer = "\n" + " " * (indent * depth) if indent else ""
    colon = ": " if indent else ":"
    if isinstance(value, dict):
        if not value:
            return "{}"
        fields = [
            f"{json.dumps(str(key), ensure_ascii=False)}{colon}"
            f"{_write(item, indent, depth + 1)}"
            for key, item in value.items()
        ]
        return "{" + inner + ("," + inner).join(fields) + outer + "}"
    if isinstance(value, list | tuple):
        if not value:
            return "[]"
        items = [_write(item, indent, depth + 1) for item in value]
        return "[" + inner + ("," + inner).join(items) + outer + "]"
    raise TypeError(f"{type(value).__name__} has no JSON spelling")


def dumps(value: Any, indent: int = 0) -> str:  # noqa: ANN401
    """Return what JSON.stringify(value, null, indent) returns."""
    return _write(value, indent, 0)
