# trading_brain

Decision brain for the XTB trading system. Pure math in Python (data + indicators);
a commercial LLM (Claude) is the sole decision-maker; a rigid Risk Engine gates every
order sent to **trading_hands** over HTTP.

- Architecture & functionality: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Execution plan (phased): [docs/EXECUTION_PLAN.md](docs/EXECUTION_PLAN.md)

## Phase 0 — run it

Requires Python 3.12+.

```bash
cd trading_brain
python -m venv .venv && source .venv/bin/activate
pip install -e .            # or: pip install httpx pydantic pydantic-settings
cp .env.example .env        # adjust if needed

# 1. Smoke test against trading_hands (must be running):
python -m app.smoke

# 2. Database (two separate roles):
pip install 'psycopg[binary]'
# 2a. create the DB with a PRIVILEGED role (once):
BRAIN_ADMIN_DB_DSN='postgresql://<admin>:<pw>@127.0.0.1:5433/postgres' python -m database.bootstrap
# 2b. apply versioned migrations with the APP role (idempotent):
python -m database.migrate
```

`app.smoke` places no orders — it only reads `/status`, `/instruments`, `/quote`.

## Phase 1 — collect market snapshots

```bash
# Configure the data provider in .env:
#   BRAIN_MARKET_DATA_PROVIDER=polygon
#   BRAIN_POLYGON_API_KEY=...           (free tier: keep BRAIN_POLYGON_MIN_INTERVAL_SECONDS=13)
pip install -e .                        # installs numpy too

# one snapshot for the latest available M15 bar (live provider):
python -m app.collect

# offline (no provider / no key), from CSV files named {SYMBOL}_{tf}.csv:
python -m app.collect --csv /path/to/csv

# scheduler: process due bar(s) once, or run forever waking at each M15 close:
python -m app.jobs --once
python -m app.jobs
```

`app.collect`/`app.jobs` write `market_snapshots` (idempotent on `(symbol, bar_close)`;
the contextual spread is a separate append-only fact, never mutating the observation).
No orders are placed.

## Phase 3 / 5 — shadow backtest & walk-forward evaluation

No real money, no execution. The deterministic maker (`ConfluenceStrategy`) is free; `--maker
claude` is the real paid model, guarded by a hard call cap + explicit confirmation.

```bash
# Backtest over historical bars (single position at a time, full cost model):
python -m shadow.runner --count 2500                      # deterministic, free
python -m shadow.runner --count 2500 --persist --run-id my-exp   # write the auditable chain
python -m shadow.runner --maker claude --max-llm-calls 50 --yes  # PAID, capped + confirmed

# Continuous shadow-online (decide on each M15 close, reconcile open trades):
python -m shadow.online --once            # one tick   (deterministic maker only online)
python -m shadow.online                   # loop, waking at each M15 close

# Walk-forward evaluation: baselines (confluence / random / flat), bootstrap CI, drawdown,
# confidence calibration (ECE), regime coverage, per-fold + overall:
python -m shadow.evaluation --count 2500 --folds 2
```

Backtests take an exclusive lock on `run_id` (a stateful run must be serial); a crashed run
resumes without re-calling the model or duplicating trades.

## Tests

```bash
pip install -e '.[dev]'                 # pytest + pandas (indicator cross-check)
python -m pytest -q                     # no-infra tests (repository tests skip without a DB)
BRAIN_DB_DSN='postgresql://user:pw@127.0.0.1:5433/trading_brain' python -m pytest -q   # incl. DB tests
```

## Layout

| Path | Role |
|---|---|
| `config/` | typed settings (Pydantic) |
| `brokers_bridge/` | async HTTP client for the 8 trading_hands endpoints (incl. `/candles`) |
| `data_collector/` | `MarketDataProvider` (XTB real-time, Polygon/Massive, CSV) + strict candle/series validation + session calendars + news (`as_of`) |
| `features/` | indicators (numpy), regime/S-R engineering, MTF `FeaturePacket`, eligibility |
| `database/` | versioned `migrations/` (0001–0019) + `migrate.py` (app role) + `bootstrap.py` (admin role) + `repository.py` + `feedback.py` (as_of-safe track record) |
| `app/` | `smoke`, `collect`, `decide`, `jobs` (M15 scheduler) |
| `decision/` | `schema` (strict I/O contract, incl. news + feedback), `prefilter`, `llm_client` (Anthropic, fail-closed), `pipeline` |
| `risk/` | `engine.py` — rigid gate + deterministic ATR-based SL/TP (never the LLM's job) |
| `shadow/` | `virtual_broker`, `reconciler`, `runner` (backtest), `online` (continuous), `metrics`, `evaluation` (walk-forward + baselines) |
| `core/` | shared models/enums |
