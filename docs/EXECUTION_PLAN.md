# trading_brain — Plan de execuție

> Complementar la [ARCHITECTURE.md](ARCHITECTURE.md). Fazele sunt secvențiale ca
> dependențe; fiecare are **livrabile** și un **criteriu de ieșire (DoD)** verificabil.
> Regula de aur: nu treci mai departe fără DoD-ul fazei curente.

---

## Rezumat faze

| Fază | Titlu | Livrează | Blochează |
|---|---|---|---|
| 0 | Fundație | schelet, config, client trading_hands, health-check | tot |
| 1 | Colectare + Features | snapshots MTF reale în DB (fără AI) | 2 |
| 2 | Brain + Risk Engine | decizii validate (fără execuție) | 3 |
| 3 | Shadow Mode | execuție fantomă + reconciliere modelată | 5 (parțial) |
| 4 | Istoric autoritativ (ipax) | rezultat live autoritativ (subproiect) | 6 |
| 5 | Feedback loop + evaluare | walk-forward cu baseline/costuri/calibrare | 6 |
| 6 | Go-live controlat | live cu volum minim + kill-switch | — |

Cross-cutting (în toate fazele): manifest de reproducere, teste, discipline anti look-ahead.

---

## Faza 0 — Fundație

**Scop:** un schelet care rulează și confirmă că vedem date reale de la trading_hands.

- Folosește numele corect `trading_brain/`; elimină folderul-typo gol `traiding_brain/`.
- `pyproject.toml` (Python 3.12+), structura de module din arhitectură, `settings.py` tipizat
  (URL trading_hands, DSN Postgres, chei API, timeframes, praguri de risc).
- `brokers_bridge/trading_hands.py`: client async tipizat pentru cele 7 endpointuri.
- DB `trading_brain` creat cu rol privilegiat (`database.bootstrap`) + migrare versionată `0001_initial` aplicată cu rolul aplicației (`database.migrate`), pe același server Postgres (port 5433).
- Un script de smoke: `GET /status` → conectat + demo; `GET /instruments/gold` → rezolvă simbolul,
  `tradeable`, `session_type`; `GET /quote/{symbol}` → bid/ask real.

**DoD:** smoke-ul întoarce simbol rezolvat + un quote real; schema DB creată; `settings` încarcă din `.env`.

---

## Faza 1 — Colectare de date + Feature Engineering (FĂRĂ AI)

**Scop:** produci features corecte și le persiști, validabile independent de LLM.

- `data_collector/providers/`: implementare `MarketDataProvider` (provider REST OHLCV) pentru
  D1/H4/H1/M15; **doar bare închise**.
- `data_collector/news/`: fetch + curățare + dedup, cu `publication_time` și `ingestion_time`.
- `features/engineering.py`: regim (EMA 21/50/200 stack + ADX + pantă), ATR%, RSI, structură S/R
  (swing highs/lows), distanțe la nivel. `features/mtf.py`: agregare + scor de confluență + `spread_pct` din `/quote`.
- La fiecare M15 închis: scrie `market_snapshots` (coloane fierbinți promovate + JSONB).

**DoD:** indicatorii verificați numeric vs. o referință (ex. TradingView) pe câteva bare;
snapshot-uri scrise la M15 close; nicio bară în formare folosită (test anti look-ahead trece).

**DoD împărțit în două:**

*A. Pipeline / replay — ✅ VALIDAT pe date reale:*
- Provider Polygon/Massive (`C:XAUUSD`), Candle/serie strict validate, ancorare la un singur `as_of`,
  provenance, scheduler M15 cu `WindowCache` (TTL per-tf), spread/basis observat din `/quote`.
- Indicatori validați vs. implementare **independentă** (pandas ewm).
- Calendar de sesiune **DST-aware** (ET) + excepții **confirmate** versionate (nu auto-etichetează goluri).
- Separare `full_window_quality` (audit, toate golurile) de `decision_eligibility` (fereastră recentă + prospețime).
- Snapshot real istoric **persistat** (id=61) cu `eligible_for_decision=False`, motiv `stale_feed` — fail-closed
  e o poartă de decizie, nu o ștergere a observației. Demonstrează pipeline-ul replay.
- Rol PostgreSQL dedicat aplicației (DML-only), separat de rolul admin.
- Fix căutare instrumente trading_hands (match exact înainte de plafon) + teste Go.

*B. Online readiness — ⛔ BLOCAT (dependent de feed real-time):*
- Free-tier Polygon **întârzie** datele (ultima bară vineri) → snapshot-urile online sunt corect `stale`.
- Shadow Online rămâne blocat până alegem un feed real-time (plătit). Free-tier = doar dev/replay.

*C. Amânate:*
- **Știri LIVE** → primul subpas al Fazei 2 (interfața + `as_of` + provider static există).
- Paritate indicatori vs. TradingView pe bare reale (pas manual).
- Fix-ul de căutare are efect live doar după **rebuild/restart** trading_hands (înainte de smoke-ul final Phase 0).

---

## Faza 2 — Brain + Risk Engine (decizii, FĂRĂ execuție)

**Scop:** transformi features în decizii validate, ieftin și reproductibil.

- `brain/schemas.py`: `AIDecision` Pydantic (`direction: BUY|SELL|NO_TRADE`, `confidence`,
  `invalidation`, `rationale`, opțional `risk_preset: tight|normal|wide`).
- `brain/prompt_builder.py`: pachet JSON (features + știri `as_of` + feedback placeholder);
  partea statică (reguli + schemă + few-shot) separată pentru **prompt caching (TTL 1h)**.
- `brain/llm_client.py`: Anthropic SDK, `messages.parse()` cu `output_config.format` (Structured
  Outputs), `thinking: adaptive`. Model: `claude-sonnet-5` decizie; `claude-haiku-4-5` pre-clasificare/știri.
- `strategy.prefilter(...)`: taie apelul LLM când nu e setup (regim/ADX/nivel/spread/sesiune).
- `risk_manager/engine.py`: **SL/TP determinist** (ex. `SL = k·ATR`, `TP = R·SL`); validare
  fail-closed (invalid/sub prag → NO_TRADE); spread ≤ max; SL obligatoriu; cooldown; sesiune.
  `confidence` logat separat; `preds_proba` = sentinelă ≥0.5.
- Scrie `decisions` cu **manifest de reproducere** (vezi Cross-cutting).

**DoD:** pe snapshot-uri reale se produc decizii validate; Structured Outputs garantează forma;
constrângerile numerice sunt impuse de Risk Engine (nu de schemă); `cache_read_input_tokens > 0`
la apeluri repetate; pre-filtrul reduce demonstrabil apelurile.

### Progres — checkpoint de corectitudine PRE-LLM (închis)

Înainte de primul apel LLM s-au închis patru corecții de contract, fiecare cu teste:

1. **Contract temporal determinist** — Polygon ancorează bucket-urile la `from`-ul cererii
   (dovedit pe răspuns real: `from :08:37` → bare `:08/:23/:38`, exact `:08`-ul din snapshot
   id=61). Fix: `from` aliniat la grila UTC-epocă (`floor_to_grid`); barele returnate sunt
   **verificate on-grid** (`validate_series` ridică → fail-closed). D1 la 00:00 UTC (DST-invariant).
   Cod: [data_collector/providers/base.py](../data_collector/providers/base.py),
   [data_collector/providers/polygon.py](../data_collector/providers/polygon.py); teste: `tests/test_temporal.py`.
2. **Snapshot ≠ eligibilitate** — snapshot-ul de piață e o observație IMUTABILĂ; eligibilitatea
   e un verdict separat, per `(snapshot, mode, policy_version)` (`snapshot_evaluations`, migrarea
   0006). Aceeași bară poate fi eligibilă în replay și stale online, fără suprascriere.
3. **Online fail-closed pe quote lipsă** (`missing_xtb_quote`) + `market_mode` strict `online|replay`.
4. **Subpas știri** — model bitemporal (identitate/dedup/revizii/publication+first-seen),
   `as_of_view` fără look-ahead; cerințe în [docs/NEWS_PROVIDER_REQUIREMENTS.md](NEWS_PROVIDER_REQUIREMENTS.md).

Module decizie livrate (FĂRĂ execuție, FĂRĂ apel LLM real; DecisionMaker injectat, fake în teste):
[decision/schema.py](../decision/schema.py) · [decision/prefilter.py](../decision/prefilter.py) ·
[risk/engine.py](../risk/engine.py) · [decision/pipeline.py](../decision/pipeline.py).
Următorul pas: `decision/llm_client.py` (Anthropic SDK, Structured Outputs) — **primul apel LLM real**.

---

## Faza 3 — Shadow Mode (execuție fantomă + reconciliere modelată)

**Scop:** măsori dacă există *vreun* edge, cu costuri corect modelate, fără bani reali.

- `shadow/virtual_broker.py`: intrare ASK(long)/BID(short), ieșire BID/ASK — **un** spread per
  round-trip. Nu folosi balance/equity ca PnL.
- Verificare **intrabar** SL/TP (M1 dacă e disponibil). Când SL și TP cad în același interval:
  raportează **bandă pesimist (SL-first) / optimist (TP-first)** + **rata de ambiguitate**; nu elimina cazurile.
- Separă **shadow online** (quote de intrare observat live după decizie; latență reală) de
  **replay istoric** (latență **modelată**, ex. fill la open-ul M1 următor + slippage).
- `execution/reconciler.py` (shadow): închide virtual pe `/quote` sau date fine; calculează
  R-multiple; populează `trades` cu `mode='shadow'`, `pnl_modeled`, benzi + ambiguitate.
- Modelare costuri: spread (măsurat), comision (dacă instrumentul are — de verificat), swap
  (aproximat pentru holduri overnight), slippage/latență (modelate). Marchează măsurat vs. aproximat.

**DoD:** rulare continuă produce trade-uri shadow reconciliate; metricile raportează benzi +
rată de ambiguitate + net de costuri; distincția online/replay e explicită în date.

### Progres — motorul de măsurare (implementat + testat)

Nucleul Shadow Mode e gata (pur, determinist, testat):
- [shadow/virtual_broker.py](../shadow/virtual_broker.py) — `open_virtual_trade` (nivele SL/TP din `sl_pct/tp_pct` deterministe; intrare la ask/bid; spread ca un cost round-trip; provenance measured|modeled).
- [shadow/reconciler.py](../shadow/reconciler.py) — `reconcile` verificare **intrabar** SL/TP; când ambele cad în aceeași bară → **bandă pesimist (SL-first) / optimist (TP-first)** + flag `ambiguous` (nu se elimină cazul); R-multiple **net de spread**; timeout; open. Funcționează cu orice timeframe de bare (M15 acum, M1 mai târziu).
- [shadow/metrics.py](../shadow/metrics.py) — `summarize`: win rate, expectancy R, **rată de ambiguitate**, bandă `avg_r_pessimistic..optimistic`, breakdown pe motiv de ieșire.
- `database.repository.record_shadow_trade` → tabelul `trades` (`mode='shadow'`, benzi + ambiguitate).
Teste: `tests/test_shadow.py` (16) + `test_repository` full-chain (snapshot→eval→decizie→trade shadow).

**Rămâne pentru Faza 3:** runner-ul care leagă decizii→trade-uri→reconciliere pe o rulare replay/continuă; **model de spread istoric/modelat** ca deciziile replay să treacă de Risk Engine (altfel `missing_spread`); modelarea latenței online (fill la quote observat) vs replay (fill la open-ul M1 următor + slippage); costuri suplimentare (comision de verificat, swap overnight).

---

## Faza 4 — Istoric autoritativ (SUBPROIECT în trading_hands)

**Scop:** rezultat live autoritativ (close_price, profit, fill final). Necesar pentru bani reali.

**Sursă:** `ipax.xtb.com` peste gRPC-Web (`GetClosedPositions` = backfill request/response;
`SubscribeToClosedPositionEvent` = evenimente live). Verificări **înainte** de implementare:

- **Schema reală:** recuperează din bundle-ul web-clientului (`*_pb.js` getter↔field_number,
  path complet serviciu), nu field-numbers ghicite empiric. Decodare locală redactată a unui răspuns.
- **Auth:** emitere / refresh / expirare / **asociere demo** — nu doar headerul final.
- **Transport:** test read-only pentru gRPC nativ (ALPN h2, `grpcurl list`, status Unauthenticated
  vs HTTP 415) — nu presupune nativ din captura gRPC-Web.
- **Teste structurale:** paginare, retenție, backfill după restart, închideri parțiale,
  cardinalitate position/order/fill (→ cheia unică în DB).

**Implementare:**
- Client gRPC (opțiuni, în ordinea preferinței): (1) generat din `.proto` recuperat peste gRPC
  nativ; (2) bibliotecă gRPC-Web Go; (3) framing manual **doar** dacă 1–2 imposibile (risc de mentenanță).
- Rutină de reconciliere în trading_hands: `GetClosedPositions` la pornire/reconnect + pe timer
  → **UPSERT idempotent** în `orders` (`profit/close_price/close_time/closed`), cheiat pe positionId.
- **Endpoint nou de citire** (ex. `GET /trades/closed?since=…`) consumat de `reconciler` (mode live).
- Păstrează **scoping demo-only** (garanție că nu se citește cont real).

**DoD:** un trade demo real închis apare cu `close_price/profit/close_time` autoritative; corelarea
`positionId ↔ external_id` confirmată; reconcilierea recuperează închiderile după un downtime simulat.

---

## Faza 5 — Feedback loop + Protocol de evaluare walk-forward

**Scop:** demonstrezi (sau infirmi) că feedback-ul și LLM-ul aduc edge net, cu incertitudine.

- `database/feedback.py`: nivel 1 statistici agregate pe regim; nivel 2 ultimele K trades verbatim
  (`as_of`: doar închise înainte de decizie); nivel 3 opțional kNN pgvector.
- **Protocol de evaluare** (înlocuiește pragul naiv „50–100"):
  - **Walk-forward** out-of-sample pe ≥2 ferestre neatinse în timpul iterării promptului.
  - **Baseline-uri:** (a) fără LLM (regulă deterministă, aceeași logică SL/TP); (b) fără feedback;
    (c) random/flat.
  - **Net de costuri** (spread + comision + swap + slippage modelat).
  - **Expectancy & drawdown cu interval de încredere** (bootstrap), nu punct.
  - **Calibrare confidence** (diagramă de fiabilitate, ECE).
  - **Acoperire pe regimuri** (minim trade-uri per strat înainte de a te încrede).

**DoD:** raport walk-forward cu baseline-uri, benzi de incertitudine, calibrare și acoperire;
LLM-ul bate baseline-ul fără-LLM și varianta fără-feedback net de costuri (altfel: nu treci la Faza 6).

---

## Faza 6 — Go-live controlat

**Scop:** bani reali (demo), minim de risc, reversibil.

- Flip `execution/router` pe `mode='live'`; `/purchase` cu volum minim; **o singură poziție** simultan.
- **Kill-switch:** drawdown zilnic, spread anormal, pierdere conexiune, divergență shadow↔live.
- Monitorizare paritate shadow↔live; alerting pe erori/`5xx`/latență.

**Poarta de intrare (toate obligatorii):** DoD-urile Fazelor 4 și 5 + profit/fill confirmat
**final/decontat** + scoping demo dovedit + reconciliere completă după downtime.

---

## Cross-cutting (în toate fazele)

### Manifest de reproducere (pe fiecare `decision`)
`prompt_version`, `model_id`, `output_schema_version`, `feature_pipeline_version`,
`strategy_version`, `risk_config_version`, `data_provider` + `provider_snapshot_version`,
`input_hash`, `latency_ms`, `retry_count`, `error_class`, `api_request_id`, `usage_tokens`,
`cache_hit`, `as_of`. Plus tabela `system_versions`.

### Discipline anti look-ahead
Doar bare închise; `as_of` pentru știri și feedback; triple timestamp (publicare/ingestie/decizie);
îngheață features folosite; split temporal strict în evaluare.

### Testare
Unit pe features (valori de referință), pe Risk Engine (SL/TP determinist, praguri, fail-closed),
pe maparea output→payload (semnul TP). Integrare pe clientul trading_hands (mock + smoke real).
Teste anti look-ahead. Teste de idempotență pe reconciler.

---

## Registru de riscuri / necunoscute deschise

| # | Necunoscută | Fază | Mitigare |
|---|---|---|---|
| 1 | Schema/`eid`/câmpuri ipax `GetClosedPositions` | 4 | Recuperare din bundle (getter↔field_number), nu ghicit |
| 2 | Auth ipax (emitere/refresh/expirare/demo) | 4 | Trace login + JWT claims redactate |
| 3 | Native gRPC vs. doar grpc-web | 4 | Test read-only ALPN/`grpcurl`/status |
| 4 | Retenție/paginare istoric (acoperă gap-ul?) | 4 | Teste structurale înainte de schema DB |
| 5 | Comision/swap pe aur XTB demo | 3/4 | Verificare pe instrument; până atunci = aproximat |
| 6 | Frecvența setup-urilor M15 (durata evaluării) | 5 | Poate necesita luni calendaristice; declarat |
| 7 | Calibrarea `confidence` | 5 | Ordinal până există date; apoi isotonic/Platt |
| 8 | `/quote` face subscribe+unsubscribe per apel | 3 | Polling la frecvență joasă acum; cache persistent în trading_hands dacă e nevoie |

---

## Ordinea recomandată de start

`Faza 0 → 1 → 2 → 3` se pot face fără trading_hands modificat (folosesc doar endpointurile
existente + shadow). **Faza 4 (ipax) rulează în paralel** ca subproiect independent, dar
**poarta de go-live (Faza 6) rămâne blocată** până când 4 și 5 sunt complete. Nu porni Faza 6
pe outcome modelat.
