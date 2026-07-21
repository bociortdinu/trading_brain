# trading_brain

Decision brain for the XTB trading system. Pure math in Python (data + indicators);
a commercial LLM (Claude) is, *by design*, the sole directional decision-maker; a rigid
Risk Engine gates every candidate decision. **No order is currently sent** — there is no live
router and `execution_ready` is always `False`; the Risk Engine only approves *shadow*
eligibility (deterministic SL/TP), and `trading_hands` is used read-only for data. (Runtime
today: backtests and the continuous online loop run the free deterministic strategy as a
stand-in — Claude stays gated off in the unbounded loop until it has a daily/monthly cost cap
and safe lifecycle.)

- Architecture & functionality: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Execution plan (phased): [docs/EXECUTION_PLAN.md](docs/EXECUTION_PLAN.md)
- Current completion/readiness audit: [docs/PROJECT_COMPLETION_REPORT.md](docs/PROJECT_COMPLETION_REPORT.md)
- One-command operable stack (Docker Compose): [docs/RUNBOOK.md](docs/RUNBOOK.md) — `make up`

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
# 2b. apply versioned migrations — DDL runs as ADMIN, then least-privilege DML is granted to the
#     app role (BRAIN_ADMIN_DB_DSN required; the app role cannot run DDL):
BRAIN_ADMIN_DB_DSN='postgresql://<admin>:<pw>@127.0.0.1:5433/postgres' python -m database.migrate
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

## Phase 2.5 — harvest a persistent bar archive (do this FIRST)

XTB's CoreAPI serves a **rolling window** over a session-bound connection, so a backtest that
fetches live needs `trading_hands` authenticated, cannot run on a weekend, and is not
reproducible — `freeze_dataset` hashes the bars but never stores them, so once the window rolls
the exact bytes are gone. `app.harvest` writes the bars to disk in the format
`CsvMarketDataProvider` reads, **merging** with what is already there, so repeated harvests
accumulate history far beyond the provider's window.

```bash
# while trading_hands is up and authenticated (BRAIN_MARKET_DATA_PROVIDER=xtb):
python -m app.harvest --out data/bars --count 10000

# then every backtest runs offline, free, reproducible, no live session:
BRAIN_MARKET_DATA_PROVIDER=csv BRAIN_CSV_DIR=data/bars \
    python -m shadow.runner --count 2500 --persist --run-id det-smoke
```

No orders, no paid AI, no DB writes. Conflicts are **fail-closed**: if a fetched bar disagrees
with the archived bar for the same `open_time`, the run aborts and names the bars rather than
rewriting history (`--on-conflict keep|replace` to decide deliberately).

It also reports **backtest-readiness**, and exits non-zero when the archive is not ready. Every
timeframe needs `MIN_BARS` (200) of warmup *before* the first bar you want to evaluate, and the
daily is usually the binding constraint — an archive with thousands of M15 bars but a shallow
daily silently yields `bars_evaluated=0`, which is indistinguishable from a broken pipeline.

## Phase 3 / 5 — shadow backtest & temporal-fold evaluation

No real money, no execution. The deterministic maker (`ConfluenceStrategy`) is free; `--maker
claude` is the real paid model, guarded by a hard call cap + explicit confirmation.

```bash
# Backtest over historical bars (single position at a time; cost model = spread + slippage +
# gap-through-stop + commission/swap IF their rates are configured, else NOT modelled):
python -m shadow.runner --count 2500                      # deterministic, free
python -m shadow.runner --count 2500 --persist --run-id my-exp   # write the auditable chain
python -m shadow.runner --maker claude --max-llm-calls 50 --yes  # PAID, capped + confirmed
python -m shadow.runner --persist --run-id fb --feedback --maker claude --yes  # PAID; only Claude reads feedback (deterministic ignores it)

# Continuous shadow-online (decide on each M15 close, reconcile open trades):
python -m shadow.online --once            # one tick   (deterministic maker only online)
python -m shadow.online                   # loop, waking at each M15 close

# Temporal fold report: baselines (confluence + random both trade; flat = the never-trade zero line), bootstrap CI,
# drawdown, confidence discrimination (ordinal, not ECE), regime coverage, per-fold + overall:
python -m shadow.evaluation --count 2500 --folds 2
```

Backtests take an exclusive lock on `run_id` (a stateful run must be serial). A crashed run
resumes **when the config is unchanged** (the execution config is part of the decision
fingerprint), without re-calling the model or duplicating trades.

## Operator dashboard — vezi sistemul cap-coadă

Dashboard local, read-only: stare XTB/DB, quote și bare, lag-ul brain-ului, alerte, traseul
`bară → features → eligibilitate → decizie → risk → trade shadow`, run-uri și auditul/costul LLM.

```bash
pip install -e '.[db]'
python -m dashboard --open       # http://127.0.0.1:8080
```

Nu expune endpointuri de ordine sau scriere, iar conexiunile sale DB sunt forțate `READ ONLY`.
Alertele operaționale (ex. trade-uri rămase open) pot include **run-uri legacy neverificate**;
în schimb **metricile oficiale de track record consumă doar run-uri cu manifest verificat** —
`NO_VERIFIED_TRACK_RECORD` până când există așa ceva. Detalii: [dashboard/README.md](dashboard/README.md).

## Tests

```bash
pip install -e '.[dev]'                 # pytest + pandas (indicator cross-check)
python -m pytest -q                     # no-infra tests (repository tests skip without a test DB)
BRAIN_TEST_DB_DSN='postgresql://user:pw@127.0.0.1:5433/trading_brain_test' \
BRAIN_TEST_ADMIN_DB_DSN='postgresql://admin:pw@127.0.0.1:5433/trading_brain_test' python -m pytest -q
# Repository tests refuse a database whose name does not end in `_test`. BRAIN_TEST_ADMIN_DB_DSN is
# the admin/owner used for cleanup (facts are append-only for the app role, so cleanup deletes must
# run as admin). The app role has NO UPDATE/DELETE on fact tables incl. decisions; pruning is a
# separate retention role (migration 0026). The append-only assertion test FAILS (never skips) if
# that guarantee regresses, so BRAIN_TEST_ADMIN_DB_DSN is required for the DB suite to clean up.
# One-time setup (creates only the named `_test` DB, then applies the normal migrations):
BRAIN_TEST_DB_DSN='postgresql://user:pw@127.0.0.1:5433/trading_brain_test' python -m database.bootstrap_test
```

## Layout

| Path | Role |
|---|---|
| `config/` | typed settings (Pydantic) |
| `brokers_bridge/` | async HTTP client for the 8 trading_hands endpoints (incl. `/candles`) |
| `data_collector/` | `MarketDataProvider` (XTB real-time, Polygon/Massive, CSV) + strict candle/series validation + session calendars + news (`as_of`) |
| `features/` | indicators (numpy), regime/S-R engineering, MTF `FeaturePacket`, eligibility |
| `database/` | versioned `migrations/` (0001–0027), admin-run DDL, isolated test-DB bootstrap, repository/feedback and operational telemetry |
| `app/` | `smoke`, `collect`, `decide`, `jobs` (M15 scheduler), `harvest` (persistent bar archive) |
| `decision/` | `schema` (strict I/O contract, incl. news + feedback), `prefilter`, `llm_client` (Anthropic, fail-closed), `pipeline` |
| `risk/` | `engine.py` — rigid gate + deterministic ATR-based SL/TP (never the LLM's job) |
| `shadow/` | `virtual_broker`, `reconciler`, `runner` (backtest), `online` (continuous), `metrics`, `evaluation` (temporal-fold report + baselines; not true walk-forward — the maker is not trainable per fold) |
| `core/` | shared models/enums |
