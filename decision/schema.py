"""Decision layer contracts (Faza 2).

Division of authority (unchanged from the design):
- Python does PURE MATH -> a compact DecisionInput (NO raw candles).
- The commercial LLM is the SOLE decision-maker -> DecisionOutput (BUY/SELL/NO_TRADE).
- LLM `confidence` is an ORDINAL signal, NOT a calibrated probability: it is used only as a
  threshold by the Risk Engine, never to size a position.
- SL/TP are computed DETERMINISTICALLY in Python (risk/engine.py), never by the LLM.

DecisionInput feature set (justification — why this and not raw candles):
- Per timeframe D1/H4/H1/M15 (roles: macro / major-trend / structure / trigger): `regime`,
  `ema_align`, `adx` (trend STRENGTH), `ema50_slope_pct` (trend DIRECTION+steepness — ADX has
  no sign), `rsi` (momentum/exhaustion), `atr_pct` (volatility -> drives SL sizing),
  `nearest_support_pct` / `nearest_resistance_pct` (proximity to levels -> invalidation & TP
  context). These are the minimal derived signals a discretionary MTF trader reads; raw OHLC
  is deliberately excluded (token cost, overfitting, non-determinism, and the LLM should
  reason on STRUCTURE, not tick data).
- Top level: `confluence`, `macro_bias`, `major_trend`, `regime_h1`, `price`, `spread_pct` +
  `spread_provenance`, `news` (with status), and the reproducibility versions.

Fail-closed: a malformed DecisionOutput raises at construction (pydantic), so an invalid or
out-of-vocabulary model reply can never reach the Risk Engine.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from core.models import Direction

# Reproducibility manifest versions (stored on every decision).
DECISION_PROMPT_VERSION = "decision-prompt-2026.1"
DECISION_SCHEMA_VERSION = "decision-schema-2026.3"   # 2026.3: + as_of-safe feedback context (Faza 5)
STRATEGY_VERSION = "strategy-mvp-2026.1"

Mode = Literal["online", "replay"]
# Where spread_pct came from. For a HISTORICAL bar we must never borrow the CURRENT XTB
# quote — that is look-ahead. Until historical/modeled spread is wired, replay is 'unavailable'.
SpreadProvenance = Literal["observed_xtb", "historical", "modeled", "unavailable"]
NewsStatus = Literal["ok", "unavailable"]   # 'ok' + empty items == no_relevant_news


class TimeframeView(BaseModel):
    model_config = {"extra": "ignore"}
    regime: str | None = None
    ema_align: str | None = None
    adx: float | None = None
    rsi: float | None = None
    atr_pct: float | None = None
    ema50_slope_pct: float | None = None
    nearest_support_pct: float | None = None
    nearest_resistance_pct: float | None = None


class NewsContext(BaseModel):
    # 'unavailable' (feed down / not wired) MUST be distinguished from 'ok' + [] (genuinely
    # no relevant news). The LLM is told which it is; they mean different things.
    status: NewsStatus = "unavailable"
    items: list[dict] = Field(default_factory=list)


class FeedbackContext(BaseModel):
    """The shadow track record so far (Faza 5), as_of-safe. Only trades CLOSED before this
    decision's as_of are ever included (see database.feedback). Empty for a fresh run."""
    regime_performance: list[dict] = Field(default_factory=list)  # per-regime win_rate/expectancy
    recent_trades: list[dict] = Field(default_factory=list)       # last K closed trades, verbatim-ish


class DecisionInput(BaseModel):
    symbol: str
    as_of: datetime
    mode: Mode
    price: float
    regime_h1: str
    macro_bias: str
    major_trend: str
    confluence: str
    spread_pct: float | None = None
    spread_provenance: SpreadProvenance = "unavailable"
    timeframes: dict[str, TimeframeView] = Field(default_factory=dict)
    news: NewsContext = Field(default_factory=NewsContext)
    feedback: FeedbackContext = Field(default_factory=FeedbackContext)
    feature_pipeline_version: str
    schema_version: str = DECISION_SCHEMA_VERSION

    def canonical(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def input_hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


def decision_fingerprint(*, input_hash: str, model: str, provider: str,
                         prompt_version: str = DECISION_PROMPT_VERSION,
                         strategy_version: str = STRATEGY_VERSION,
                         risk_config_version: str) -> str:
    """Run-scoped decision identity for idempotency: the SAME frozen input decided by the SAME
    model + prompt + strategy + risk config + data provider is the SAME decision. Changing any
    of these (a new prompt, a different provider) is a DIFFERENT decision and must NOT be
    deduped against the old one. Hashed so it fits a single indexed column."""
    parts = [input_hash, model, provider, prompt_version, strategy_version, risk_config_version]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


class DecisionOutput(BaseModel):
    """The LLM's structured decision. STRICT: only BUY/SELL/NO_TRADE, confidence in [0,1]
    (ORDINAL), non-empty rationale. Extra fields rejected (no smuggled SL/TP/size)."""
    model_config = {"extra": "forbid"}

    direction: Direction
    confidence: float = Field(ge=0.0, le=1.0)     # ordinal, not a probability
    rationale: str = Field(min_length=1, max_length=4000)
    key_factors: list[str] = Field(default_factory=list, max_length=12)


class DecisionMaker(Protocol):
    """The commercial LLM (or a deterministic fake in tests). Receives ONLY the compact
    DecisionInput and returns a DecisionOutput. Never sees raw candles; never sets SL/TP/size."""
    async def decide(self, inp: DecisionInput) -> DecisionOutput: ...


_TF_KEYS = ("1day", "4h", "1h", "15min")


def _spread_provenance(packet, mode: str) -> SpreadProvenance:
    if packet.spread_pct is None:
        return "unavailable"
    # The only wired source is the live XTB quote observed online. A replay packet must not
    # carry an observed_xtb spread (the collector does not fetch a current quote in replay).
    return "observed_xtb" if mode == "online" else "modeled"


def build_decision_input(packet, *, mode: str, news: NewsContext | None = None,
                         feedback: FeedbackContext | None = None) -> DecisionInput:
    """Deterministically project a FeaturePacket into the LLM-facing input. `feedback` is the
    as_of-safe shadow track record (database.feedback); empty when not supplied. It IS part of
    the input hash — a decision made with feedback X differs from one made with feedback Y."""
    tfs = {k: TimeframeView(**packet.timeframes.get(k, {})) for k in _TF_KEYS if k in packet.timeframes}
    return DecisionInput(
        symbol=packet.symbol,
        as_of=packet.bar_close,
        mode=mode,
        price=packet.price,
        regime_h1=packet.regime,
        macro_bias=packet.macro_bias,
        major_trend=packet.major_trend,
        confluence=packet.confluence,
        spread_pct=packet.spread_pct,
        spread_provenance=_spread_provenance(packet, mode),
        timeframes=tfs,
        news=news if news is not None else NewsContext(status="unavailable"),
        feedback=feedback if feedback is not None else FeedbackContext(),
        feature_pipeline_version=packet.pipeline_version,
    )
