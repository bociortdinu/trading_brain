"""Typed configuration for trading_brain (Phase 0).

MVP principle: a single typed config object, not a dynamic registry. Extend by
adding fields here, not by wiring a plugin loader.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BRAIN_",
        extra="ignore",
    )

    # trading_hands HTTP API
    trading_hands_url: str = "http://127.0.0.1:4000"
    http_timeout_seconds: float = Field(10.0, gt=0)

    # PostgreSQL. `db_dsn` is the APPLICATION role (runtime DML only). `admin_db_dsn` is a
    # SEPARATE privileged role used by bootstrap/migrate. Repository tests are destructive and
    # may use only `test_db_dsn`, whose database name is required to end in `_test`.
    db_dsn: str = "postgresql://trading_hands@127.0.0.1:5433/trading_brain"
    admin_db_dsn: str | None = None
    test_db_dsn: str | None = None

    # Asset under analysis. `symbol_query` is the XTB symbol; the data feed uses a
    # provider-specific symbol resolved via `provider_symbol_map`.
    symbol_query: str = "GOLD"
    # Experiment identity for Shadow Online. A run_id pins ONE frozen config (run manifest); set
    # this to a release/build id whenever the config or strategy changes, so a legitimate upgrade
    # starts a NEW run instead of hitting RunConfigMismatch. If unset, the default embeds the
    # strategy version (so at least a strategy change forces a new run).
    run_id: str | None = None
    timeframes: list[str] = Field(default_factory=lambda: ["1day", "4h", "1h", "15min"])

    # Market-data provider selection (composition via config; interface is MarketDataProvider).
    #   polygon = Massive/Polygon.io REST (delayed on free tier)
    #   xtb     = real-time via trading_hands /candles (same VENUE as execution -> reduces venue
    #             mismatch, but the bar feed and the /quote can still differ in timestamp/aggregation)
    #   csv     = offline files
    market_data_provider: Literal["polygon", "csv", "xtb"] = "polygon"
    csv_dir: str | None = None
    # Economic calendar (scheduled macro releases). Free key: fredaccount.stlouisfed.org.
    # Unset -> the calendar is simply absent; the pipeline still runs, news status stays
    # 'unavailable', and NO blackout is applied (fail-OPEN here is deliberate: a missing
    # calendar must not silently halt trading, but it IS reported as unavailable so the
    # model is never told "no events" when we simply do not know).
    fred_api_key: str | None = None
    calendar_blackout_minutes_before: int = Field(30, ge=0)
    # Short by measurement, not by taste — a 15-minute after-window blocked the only winning
    # trade in the 2026-07-14 CPI backtest. See CalendarConfig in economic_calendar.py.
    calendar_blackout_minutes_after: int = Field(5, ge=0)
    calendar_context_lookahead_minutes: int = Field(240, ge=0)
    # Brain symbol -> provider symbol (Polygon forex/metals ticker for gold is C:XAUUSD).
    provider_symbol_map: dict[str, str] = Field(default_factory=lambda: {"GOLD": "C:XAUUSD"})

    # Polygon / Massive
    polygon_api_key: str | None = None
    polygon_base_url: str = "https://api.polygon.io"
    # Client-side spacing between provider requests. Free tier is ~5 req/min, so
    # ~13s keeps us under it; set to 0 on a paid plan.
    polygon_min_interval_seconds: float = Field(13.0, ge=0)

    # Decision eligibility. "online" measures freshness vs the wall clock; "replay"
    # vs as_of. The free tier is delayed, so it is for replay/dev — online Shadow is
    # blocked until a real-time feed is chosen. STRICT: a typo (e.g. "onlien") is
    # rejected at load time rather than silently treated as some default.
    market_mode: Literal["online", "replay"] = "online"
    eligibility_recent_window_bars: dict[str, int] = Field(
        default_factory=lambda: {"15min": 8, "1h": 6, "4h": 4, "1day": 3}
    )
    eligibility_max_feed_lag_seconds: int = Field(1800, gt=0)
    eligibility_max_quote_lag_seconds: int = Field(120, gt=0)
    # The feed-vs-broker basis is trustworthy only when the XTB quote is observed close to
    # the bar close; beyond this lag the number is dominated by price movement, so it is
    # marked unreliable and the basis magnitudes are not reported.
    max_basis_lag_seconds: int = Field(90, gt=0)

    # LLM decision layer (Faza 2). ONE configurable baseline model; the benchmark model runs
    # on the SAME frozen inputs for comparison. No Haiku tiering yet (news classification is
    # not a separate versioned/measured step yet).
    anthropic_api_key: str | None = None
    decision_model: str = "claude-sonnet-5"
    benchmark_model: str = "claude-opus-4-8"
    decision_max_tokens: int = Field(1024, gt=0)
    # MASTER KILL-SWITCH for PAID Anthropic calls. Default OFF: every entry point that could spend
    # money (app.decide --paid, app.llm_smoke, shadow.runner --maker claude) must fail-closed unless
    # this is explicitly true. A configured api key alone must NEVER be sufficient to spend.
    paid_ai_enabled: bool = False
    # Central financial gateway (decision/paid_gateway.py). Only a model on the allowlist may be
    # used paid; HTTP attempts per logical decision are hard-capped (1 = no retries, for a canary);
    # and USD budgets (per run / UTC day / UTC month) default to 0 = NOTHING permitted until you set
    # them. Budget accounting is derived from the paid_attempts ledger.
    paid_ai_model_allowlist: list[str] = Field(default_factory=lambda: ["claude-haiku-4-5"])
    paid_max_http_attempts: int = Field(1, ge=1)
    paid_budget_run_usd: float = Field(0.0, ge=0)
    paid_budget_day_usd: float = Field(0.0, ge=0)
    paid_budget_month_usd: float = Field(0.0, ge=0)

    # Shadow backtest: a MODELED spread for historical (replay) bars — we never borrow the
    # current live quote for a past bar. ~XTB gold spread observed live (~0.018-0.02%).
    replay_spread_pct: float = Field(0.02, ge=0)
    # Modeled execution slippage (adverse) applied to every shadow entry AND exit fill, on top
    # of the spread. Keeps shadow R-multiples honest (not over-optimistic).
    slippage_pct: float = Field(0.005, ge=0)
    # Real XTB GOLD financing terms. Default = NOT modeled (shadow R is then not net of financing;
    # the persisted cost manifest says so). Wire these from xStation5 -> GOLD -> Specification. The
    # MODEL supports a long/short swap split, a triple-swap weekday, and a DST-aware rollover tz;
    # leaving them unset falls back to the single swap_pct_per_night at a fixed 22:00 UTC rollover.
    commission_pct: float = 0.0
    swap_pct_per_night: float = 0.0                 # legacy single rate (both directions) if no split
    swap_long_pct_per_night: float | None = None    # BUY overnight (%/night of notional)
    swap_short_pct_per_night: float | None = None   # SELL overnight
    triple_swap_weekday: int | None = Field(None, ge=0, le=6)   # 0=Mon..6=Sun charged 3x (e.g. 2=Wed)
    swap_currency: str | None = None                # informational: currency the swap is quoted in
    financing_terms_version: str = "unset"          # provenance of the terms above
    rollover_hour_utc: int = Field(22, ge=0, le=23)             # rollover hour, in rollover_tz
    rollover_tz: str = "UTC"                        # IANA tz for the DST-aware rollover wall-clock
    # Intrabar reconciliation granularity. "15min" (default) reconciles on the decision bar, so
    # SL and TP can both land in one bar (the pessimistic/optimistic ambiguity band). "1min"
    # resolves the touch ordering at M1 — fetched only for reconciliation, and it falls back to
    # 15min (flagged on the trade) if the provider cannot serve M1.
    reconcile_timeframe: Literal["1min", "15min"] = "15min"

    @field_validator("rollover_tz")
    @classmethod
    def _validate_rollover_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"invalid BRAIN_ROLLOVER_TZ {v!r}: {exc}") from exc
        return v

    @field_validator("eligibility_recent_window_bars")
    @classmethod
    def _validate_eligibility_windows(cls, v: dict[str, int]) -> dict[str, int]:
        valid = {"1min", "15min", "1h", "4h", "1day"}
        if not v:
            raise ValueError("eligibility_recent_window_bars must not be empty")
        for tf, n in v.items():
            if tf not in valid:
                raise ValueError(f"eligibility_recent_window_bars: unknown timeframe {tf!r}")
            if not isinstance(n, int) or n <= 0:
                raise ValueError(f"eligibility_recent_window_bars[{tf!r}] must be a positive int, got {n!r}")
        return v

    @model_validator(mode="after")
    def _reject_nonfinite_floats(self) -> "Settings":
        import math
        for name, val in self.__dict__.items():
            if isinstance(val, float) and not math.isfinite(val):
                raise ValueError(f"{name} must be a finite number, got {val!r} (NaN/inf rejected)")
        return self

    def provider_symbol(self, brain_symbol: str) -> str:
        # Only Polygon uses a different ticker (C:XAUUSD); XTB and CSV use the brain symbol
        # (GOLD) directly. Keeps the symbol mapping correct when switching providers.
        if self.market_data_provider == "polygon":
            return self.provider_symbol_map.get(brain_symbol, brain_symbol)
        return brain_symbol


def warn_if_env_world_readable(env_path: str = ".env") -> str | None:
    """A .env holds DB/API secrets and must be 0600. Return a warning string if it is group- or
    world-accessible (does not raise — the caller logs it), else None."""
    import os
    import stat
    try:
        mode = os.stat(env_path).st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (f"{env_path} is group/other-accessible (mode {stat.S_IMODE(mode):#o}); it holds "
                f"secrets — run: chmod 600 {env_path}")
    return None


def load_settings() -> Settings:
    import logging
    warning = warn_if_env_world_readable(Settings.model_config.get("env_file", ".env"))
    if warning:
        logging.getLogger("config.settings").warning("insecure permissions: %s", warning)
    return Settings()
