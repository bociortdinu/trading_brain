"""Typed configuration for trading_brain (Phase 0).

MVP principle: a single typed config object, not a dynamic registry. Extend by
adding fields here, not by wiring a plugin loader.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BRAIN_",
        extra="ignore",
    )

    # trading_hands HTTP API
    trading_hands_url: str = "http://127.0.0.1:4000"
    http_timeout_seconds: float = 10.0

    # PostgreSQL. `db_dsn` is the APPLICATION role (runtime DML only). `admin_db_dsn` is a
    # SEPARATE privileged role used by bootstrap/migrate. Repository tests are destructive and
    # may use only `test_db_dsn`, whose database name is required to end in `_test`.
    db_dsn: str = "postgresql://trading_hands@127.0.0.1:5433/trading_brain"
    admin_db_dsn: str | None = None
    test_db_dsn: str | None = None

    # Asset under analysis. `symbol_query` is the XTB symbol; the data feed uses a
    # provider-specific symbol resolved via `provider_symbol_map`.
    symbol_query: str = "GOLD"
    timeframes: list[str] = Field(default_factory=lambda: ["1day", "4h", "1h", "15min"])

    # Market-data provider selection (composition via config; interface is MarketDataProvider).
    #   polygon = Massive/Polygon.io REST (delayed on free tier)
    #   xtb     = real-time via trading_hands /candles (same venue as execution, no basis)
    #   csv     = offline files
    market_data_provider: Literal["polygon", "csv", "xtb"] = "polygon"
    csv_dir: str | None = None
    # Brain symbol -> provider symbol (Polygon forex/metals ticker for gold is C:XAUUSD).
    provider_symbol_map: dict[str, str] = Field(default_factory=lambda: {"GOLD": "C:XAUUSD"})

    # Polygon / Massive
    polygon_api_key: str | None = None
    polygon_base_url: str = "https://api.polygon.io"
    # Client-side spacing between provider requests. Free tier is ~5 req/min, so
    # ~13s keeps us under it; set to 0 on a paid plan.
    polygon_min_interval_seconds: float = 13.0

    # Decision eligibility. "online" measures freshness vs the wall clock; "replay"
    # vs as_of. The free tier is delayed, so it is for replay/dev — online Shadow is
    # blocked until a real-time feed is chosen. STRICT: a typo (e.g. "onlien") is
    # rejected at load time rather than silently treated as some default.
    market_mode: Literal["online", "replay"] = "online"
    eligibility_recent_window_bars: dict[str, int] = Field(
        default_factory=lambda: {"15min": 8, "1h": 6, "4h": 4, "1day": 3}
    )
    eligibility_max_feed_lag_seconds: int = 1800
    eligibility_max_quote_lag_seconds: int = 120
    # The feed-vs-broker basis is trustworthy only when the XTB quote is observed close to
    # the bar close; beyond this lag the number is dominated by price movement, so it is
    # marked unreliable and the basis magnitudes are not reported.
    max_basis_lag_seconds: int = 90

    # LLM decision layer (Faza 2). ONE configurable baseline model; the benchmark model runs
    # on the SAME frozen inputs for comparison. No Haiku tiering yet (news classification is
    # not a separate versioned/measured step yet).
    anthropic_api_key: str | None = None
    decision_model: str = "claude-sonnet-5"
    benchmark_model: str = "claude-opus-4-8"
    decision_max_tokens: int = 1024

    # Shadow backtest: a MODELED spread for historical (replay) bars — we never borrow the
    # current live quote for a past bar. ~XTB gold spread observed live (~0.018-0.02%).
    replay_spread_pct: float = 0.02
    # Modeled execution slippage (adverse) applied to every shadow entry AND exit fill, on top
    # of the spread. Keeps shadow R-multiples honest (not over-optimistic).
    slippage_pct: float = 0.005
    # Real XTB GOLD financing terms. Default = NOT modeled (shadow R is then not net of financing;
    # the persisted cost manifest says so). Wire these from xStation5 -> GOLD -> Specification. The
    # MODEL supports a long/short swap split, a triple-swap weekday, and a DST-aware rollover tz;
    # leaving them unset falls back to the single swap_pct_per_night at a fixed 22:00 UTC rollover.
    commission_pct: float = 0.0
    swap_pct_per_night: float = 0.0                 # legacy single rate (both directions) if no split
    swap_long_pct_per_night: float | None = None    # BUY overnight (%/night of notional)
    swap_short_pct_per_night: float | None = None   # SELL overnight
    triple_swap_weekday: int | None = None          # 0=Mon..6=Sun charged 3x (e.g. 2 = Wednesday)
    swap_currency: str | None = None                # informational: currency the swap is quoted in
    financing_terms_version: str = "unset"          # provenance of the terms above
    rollover_hour_utc: int = 22                     # rollover hour, interpreted in rollover_tz
    rollover_tz: str = "UTC"                        # IANA tz for the DST-aware rollover wall-clock
    # Intrabar reconciliation granularity. "15min" (default) reconciles on the decision bar, so
    # SL and TP can both land in one bar (the pessimistic/optimistic ambiguity band). "1min"
    # resolves the touch ordering at M1 — fetched only for reconciliation, and it falls back to
    # 15min (flagged on the trade) if the provider cannot serve M1.
    reconcile_timeframe: str = "15min"

    def provider_symbol(self, brain_symbol: str) -> str:
        # Only Polygon uses a different ticker (C:XAUUSD); XTB and CSV use the brain symbol
        # (GOLD) directly. Keeps the symbol mapping correct when switching providers.
        if self.market_data_provider == "polygon":
            return self.provider_symbol_map.get(brain_symbol, brain_symbol)
        return brain_symbol


def load_settings() -> Settings:
    return Settings()
