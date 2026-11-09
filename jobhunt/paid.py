"""Paid circle (ikigai circle 4), computed in Python from salary evidence.

Codex reports numbers only (``salary_min``, ``salary_max``, ``currency``,
``period``, ``stated``); the anchors and FX rates live in ``profile.toml``, so the
score is deterministic and testable. ``score = 1 + number of anchors the monthly
THB midpoint reaches``.
"""

from __future__ import annotations

from dataclasses import dataclass

CURRENCY_KEYS = {"THB": "thb", "SGD": "sgd", "USD": "usd", "HKD": "hkd"}


@dataclass(frozen=True)
class PaidResult:
    score: int | None
    estimated: bool
    thb_month_mid: float | None
    reason: str


def _rate(currency: str, profile: dict) -> float | None:
    """THB per unit of ``currency``."""
    code = (currency or "").strip().upper()
    if code == "THB":
        return 1.0
    key = CURRENCY_KEYS.get(code)
    if not key:
        return None
    value = profile.get("paid", {}).get(f"{key}_to_thb")
    return float(value) if isinstance(value, (int, float)) else None


def score(paid: dict | None, profile: dict) -> PaidResult:
    paid = paid or {}
    anchors = [float(a) for a in profile.get("paid", {}).get("anchors_thb_month", [])]
    stated = bool(paid.get("stated"))

    low = paid.get("salary_min")
    high = paid.get("salary_max")
    if low is None and high is None:
        return PaidResult(None, False, None, paid.get("reason") or "No salary stated or estimated.")

    if low is None:
        low = high
    if high is None:
        high = low
    low, high = float(low), float(high)
    if high < low:
        low, high = high, low

    currency = (paid.get("currency") or "").strip().upper() or None
    if currency is None:
        return PaidResult(None, not stated, None, "Salary given without a currency, so it cannot be compared.")
    rate = _rate(currency, profile)
    if rate is None:
        return PaidResult(None, not stated, None,
                          f"No FX rate for {currency} in profile.toml, so the salary cannot be compared.")

    period = paid.get("period")
    if period not in ("month", "year"):
        period = "month"
    mid = (low + high) / 2
    if period == "year":
        mid = mid / 12
    mid_thb = mid * rate
    reached = sum(1 for anchor in anchors if mid_thb >= anchor)
    result = 1 + reached

    basis = paid.get("reason") or ""
    label = f"{currency} {low:,.0f}-{high:,.0f} per {period}"
    prefix = "" if stated else "estimate, "
    text = f"{prefix}{label} -> about THB {mid_thb:,.0f}/month"
    if basis:
        text = f"{text}. {basis}"
    return PaidResult(result, not stated, mid_thb, text)
