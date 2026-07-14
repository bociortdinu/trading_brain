"""Feed-vs-broker basis: reliable only when the quote is observed near the bar close.
Beyond the lag threshold the magnitudes are withheld (dominated by price movement)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.collect import compute_basis

UTC = timezone.utc
_BAR = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)


def test_basis_reliable_when_quote_near_bar_close():
    observed = _BAR + timedelta(seconds=30)
    b = compute_basis(4000.0, 3999.5, 4000.5, _BAR, observed, observed, max_lag_seconds=90)
    assert b["basis_reliable"] is True
    assert b["basis_abs"] is not None and b["basis_pct"] is not None


def test_basis_withheld_when_quote_lags():
    observed = _BAR + timedelta(minutes=6)  # 360s > 90s -> dominated by price movement
    b = compute_basis(4023.0, 3999.5, 4000.5, _BAR, observed, observed, max_lag_seconds=90)
    assert b["basis_reliable"] is False
    assert b["basis_abs"] is None and b["basis_pct"] is None   # NOT a contaminated number
    assert b["observation_lag_seconds"] == 360.0
    assert b["xtb_spread_pct"] > 0                              # spread is lag-independent, kept
