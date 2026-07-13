# trading_brain — Arhitectură și funcționalități

> Documentul descrie **ce este** `trading_brain`, cum se împarte pe module și cum
> curg datele. Planul de execuție pe faze este în [EXECUTION_PLAN.md](EXECUTION_PLAN.md).

---

## 1. Scop și granițe

`trading_brain` este **creierul decizional** care lipsește sistemului. Rolul codului
Python este **pur matematic**: colectează date de piață în timp real, calculează
indicatori multi-timeframe, curăță știrile și produce un pachet numeric/text compact.
Pachetul e trimis către un **LLM comercial** (Claude), unicul care face corelarea
contextuală tehnic ↔ știri și întoarce o decizie direcțională. Python validează rigid
decizia (Risk Engine) și o trimite spre execuție către API-ul existent **trading_hands**.

### Ce FACE
- Colectează OHLCV MTF + știri, cu discipline anti look-ahead.
- Calculează features deterministe (regim, ADX, ATR, structură S/R).
- Cheamă LLM-ul **doar** când un pre-filtru determinist confirmă un setup (economie de cost).
- Calculează **SL/TP determinist în Python** (LLM-ul NU propune niveluri).
- Validează riscul local (spread, SL obligatoriu, cooldown, fail-closed).
- Rulează în **Shadow** (fantomă) și **Live**, cu o poartă de promovare între ele.
- Reconstruiește rezultatul tranzacțiilor și alimentează un **feedback loop** în prompt.

### Ce NU face
- Nu antrenează și nu rulează modele ML locale (XGBoost, rețele etc.).
- Nu trimite screenshot-uri sau serii brute de lumânări către LLM.
- Nu vorbește direct cu XTB — doar prin trading_hands, peste HTTP.
- Nu tratează `confidence`-ul LLM ca probabilitate calibrată.
- Nu face position sizing (volumul e fix la nivelul trading_hands — vezi §3).

---

## 2. Context de sistem

```
                       features text/numeric              JSON decizie
  OHLCV + știri  ─────────────────────────►  Claude LLM  ──────────────►  Risk Engine
   (data feed)                                (creierul)                   (Python, rigid)
       │                                                                        │
       │ /quote, spread real                                                    │ payload validat
       ▼                                                                        ▼
  ┌───────────────┐  HTTP (7 endpointuri + /trades/closed*)  ┌──────────────┐  WebSocket  ┌──────────┐
  │ trading_brain │ ─────────────────────────────────────────►│ trading_hands│ ──────────► │ XTB demo │
  │   (Python)    │ ◄─────────────────────────────────────────│    (Go)      │  CoreAPI    │ xStation │
  └───────┬───────┘   status/quote/positions/balance          └──────┬───────┘             └──────────┘
          │                                                           │ gRPC-Web  (* /trades/closed =
          ▼                                                           ▼            extensie trading_hands,
   PostgreSQL `trading_brain`                                    ipax.xtb.com      SUBPROIECT, Faza 4)
   (snapshots / decisions / trades + feedback)                  (istoric închideri: PnL/fill final)
```

> Brain-ul **nu** vorbește direct cu ipax. Rezultatul autoritativ al închiderilor e obținut de
> **trading_hands** din ipax (gRPC-Web, extensie în Faza 4) și consumat de brain printr-un endpoint
> nou pe trading_hands (`/trades/closed`, marcat `*`).

### 2.1 Interfața cu trading_hands (confirmat din cod)
API HTTP la `http://127.0.0.1:4000`, 7 endpointuri:

| Endpoint | Rol | Trading necesar |
|---|---|---|
| `GET /status` | conectat + cont + demo | nu |
| `GET /balance` | balance/equity/free_margin | nu |
| `GET /positions` | **doar poziții deschise** (open_price, sl, tp absolute, side, external_id) | nu |
| `GET /quote/{symbol}` | bid/ask live XTB (→ spread real) | nu |
| `GET /instruments/{q}` | rezolvă simbol, tradeable, session_type, min_volume, volume_step | nu |
| `POST /purchase` | deschide o poziție | da |
| `POST /close/{id}` | închide o poziție | da |

### 2.2 Sursa de rezultat autoritativ (subproiect separat)
trading_hands **nu** expune PnL/tranzacții închise. `/positions` = doar deschise; tabela
`orders` are coloane `profit/close_price/close_time/closed` **nefolosite**. Panoul „Closed
positions" al web-clientului XTB folosește un **backend diferit** — `ipax.xtb.com` peste
**gRPC-Web** (`GetClosedPositions`, `SubscribeToClosedPositionEvent`, …). Integrarea acestui
istoric autoritativ este un **subproiect** (extensie în trading_hands), tratat în Faza 4 din plan.

---

## 3. Constrângeri validate (din codul trading_hands)

Aceste constrângeri sunt **hard** și modelează designul:

| Constrângere | Sursă | Impact |
|---|---|---|
| **Volum FIX** (`TRADING_VOLUME`), `allocation` nu dimensionează | AI_manual, purchase.go | Fără position sizing; riscul se controlează doar prin *dacă* intri + distanța SL |
| `preds_proba ≥ 0.5` altfel ordin respins | predictiondetails.go | Poarta de acțiune |
| **Semnul lui `take_profit` codează direcția** (`≥0` Buy, `<0` Sell), magnitudinea = % | predictiondetails.go | Maparea output→payload trebuie să aplice semnul |
| `stop_loss` = % pozitiv; SL/TP calculate **server-side** din quote | predictiondetails.go | Brain-ul trimite procente, nu prețuri |
| `TradeResult` = `{accepted, external_id, symbol, side, volume}` — fără preț/PnL | client.go | Rezultatul se reconstruiește separat (Faza 4) |
| Fără endpoint de tranzacții închise | (absență în cod) | Feedback loop-ul depinde de subproiectul ipax |
| Client XTB = **snapshot request/response**, aruncă push-urile fără `reqId`; fără reconnect auto | readLoop | Reconcilierea trebuie idempotentă + backfill, nu event tranzitoriu |

---

## 4. Principii de design

1. **MVP prin compoziție + config tipizat**, nu ABC + registry + YAML dinamic. Extensibilitatea
   se păstrează prin `Protocol` doar la cele două cusături care variază real: `MarketDataProvider`
   și `Strategy`. Un asset nou = o intrare de config + (eventual) o funcție de strategie.
2. **SL/TP determinist în Python.** LLM-ul decide doar `BUY / SELL / NO_TRADE`. Motiv: sub volum
   fix, SL e singura pârghie reală de risc → o vrem deterministă, testabilă, reproductibilă, și
   ținută constantă în evaluare ca să măsurăm curat edge-ul direcțional. Opțional, LLM-ul poate
   alege dintr-un enum mic de preset-uri (`tight/normal/wide`) mapate la multipli ATR.
3. **Fail-closed pe output-ul LLM.** Un output invalid sau sub prag e **respins** și devine
   `NO_TRADE`; o valoare sub prag **nu** e niciodată ridicată la prag. (SL/TP-ul determinist
   calculat de Python e o mărime mărginită de limitele de config — mecanism distinct de output-ul LLM.)
4. **`confidence` ≠ probabilitate.** Semnal **ordinal** (mai mare = model mai sigur), folosit doar
   la gating/ranking și **logat separat**. Ce trimitem ca `preds_proba` = **constantă sentinelă**
   (≥0.5) până există calibrare empirică (curbă de fiabilitate din trade-uri reconciliate).
5. **Cheamă LLM-ul doar la setup.** Pre-filtru determinist (regim, ADX, apropiere de nivel, spread,
   sesiune) reduce numărul de apeluri; proporția reală se măsoară în Faza 2 (nu o presupunem).
   Prompt caching (TTL 1h) pe partea statică. Model tiering:
   Haiku pentru pre-clasificare/știri, Sonnet pentru decizie.
6. **Anti look-ahead.** Doar lumânări **închise**; regulă `as_of` pentru știri și feedback; triple
   timestamp (publicare / ingestie / decizie); features folosite se îngheață (nu recalcula din date revizuite).
7. **Rezultat autoritativ pentru bani reali.** Feedback-ul care alimentează decizii live vine din
   istoricul autoritativ (Faza 4). Shadow-ul produce doar outcome **modelat**, etichetat ca atare.

---

## 5. Module și funcționalități

```
trading_brain/
├── config/          settings tipizate (Pydantic) + asset config
├── core/            interfețe (Protocol), modele interne, ceas/sesiuni
├── data_collector/  providers OHLCV (REST) + news (curățate, deduplicate)
├── features/        indicatori deterministi + agregare MTF (D1/H4/H1/M15)
├── brokers_bridge/  client HTTP tipizat pentru trading_hands (7 endpointuri)
├── brain/           prompt builder (+ feedback), client LLM (Structured Outputs, caching), scheme Pydantic
├── risk_manager/    validare rigidă: spread, SL obligatoriu, cooldown, fail-closed; SL/TP determinist mărginit
├── execution/       router (shadow vs live) + reconciler (reconstruiește rezultatul)
├── shadow/          broker virtual (bid/ask, online vs replay, benzi pesimist/optimist)
├── database/        schema, repository, feedback (statistici + situații similare)
└── app/             orchestrator (loop M15) + jobs (scheduler + reconciler)
```

| Modul | Funcționalitate |
|---|---|
| **data_collector** | OHLCV MTF dintr-un provider REST stabil (context tehnic); știri curățate/deduplicate cu `publication_time` + `ingestion_time`. Basis: contextul tehnic pe feed extern, **spread & trigger pe `/quote` XTB**. |
| **features** | Clasificare regim (EMA stack + ADX + pantă, nu MA-cross simplu), ATR (volatilitate → SL), RSI, structură S/R (swing highs/lows), scor de confluență MTF. Output = ~15–20 numere/etichete, nu lumânări. |
| **brokers_bridge** | Client async tipizat: `status`, `balance`, `positions`, `quote`, `instruments`, `purchase`, `close`. Rezolvă simbolul aur prin `/instruments`, verifică `tradeable`/`session_type`. |
| **brain** | Construiește pachetul JSON (features + știri + feedback), injectează feedback din DB, apelează LLM cu **Structured Outputs** (schemă strictă) + **prompt caching**; întoarce `AIDecision` validat. |
| **risk_manager** | Poartă rigidă înaintea execuției: spread ≤ max, SL obligatoriu, cooldown/frecvență, sesiune deschisă, `confidence ≥ prag` altfel `NO_TRADE`. Calculează **SL/TP determinist** (ATR-based). Fail-closed. |
| **execution** | `router`: shadow vs live. `reconciler`: reconstruiește rezultatul (shadow = modelat; live = autoritativ din Faza 4), calculează R-multiple, populează `trades`. |
| **shadow** | Broker virtual: intrare la ASK (long)/BID (short), ieșire la BID/ASK (**un** spread per round-trip), verificare intrabar, benzi pesimist/optimist + rată de ambiguitate când SL și TP cad în același interval. |
| **database** | PostgreSQL `trading_brain` (separat de trading_hands, același server). Coloane fierbinți promovate + JSONB rece. `feedback` extrage statistici agregate + ultimele K trades (+ pgvector opțional în Faza 5). |
| **app** | Orchestrare: la fiecare **M15 închis** rulează pipeline-ul decizional; scheduler separat rulează reconciler-ul la interval. |

---

## 6. Fluxul decizional (end-to-end)

```
M15 CLOSE
   │
   ▼
1. data_collector: OHLCV MTF (doar bare închise) + știri (as_of) ; /quote XTB (spread real)
   │
   ▼
2. features: FeaturePacket compact (regim, ADX, ATR, structură, confluență)
   │
   ▼
3. strategy.prefilter(features) ── FALSE ──► STOP (nu chemăm LLM ; log NO_SETUP)
   │ TRUE
   ▼
4. brain: prompt (features + știri + feedback din DB) ──► LLM (Structured Outputs)
   │                                                        └─ AIDecision: {BUY|SELL|NO_TRADE, confidence, rationale, invalidation}
   ▼
5. risk_manager:
     - validează output (invalid/sub prag → NO_TRADE, fail-closed)
     - spread ≤ max ? SL obligatoriu ? cooldown ? sesiune deschisă ?
     - calculează SL%/TP% determinist (ATR) ; aplică semnul TP după direcție
   │ approved
   ▼
6. execution.router:
     - SHADOW → shadow.virtual_broker.open(...)   (fără /purchase)
     - LIVE   → brokers_bridge.purchase(...)       (preds_proba = sentinelă ≥0.5)
                apoi /positions ca să capturezi SL/TP absolute
   │
   ▼
7. database: scrie snapshot + decision (+ trade open) cu manifest de reproducere
   │
   ▼  (asincron, la interval)
8. execution.reconciler:
     - SHADOW → outcome modelat (bid/ask, benzi pesimist/optimist)
     - LIVE   → outcome autoritativ via trading_hands `/trades/closed` (din ipax, Faza 4)
     - calculează R-multiple, populează trades, alimentează feedback loop-ul
```

---

## 7. Rezultat, metrici și feedback loop

- **Metrica principală:** `R-multiple = (exit − entry) / (entry − SL)` cu semn — normalizat, agnostic la broker, ideal pentru feedback.
- **Live:** exit/profit/fill **autoritativ** via trading_hands `/trades/closed` (sursă ipax, Faza 4). **Shadow:** outcome **modelat** din bid/ask, cu benzi pesimist/optimist și rată de ambiguitate; **nu** se folosește balance/equity ca PnL pentru shadow.
- **Feedback loop (3 niveluri, ieftin → scump):** (1) statistici agregate pe regim (win-rate, sumă R, streak); (2) ultimele K trades verbatim cu rezultat + motiv; (3) similaritate kNN pe features (pgvector, opțional Faza 5). Injectat în prompt **doar** cu `as_of` (trade-uri închise înainte de decizie).

---

## 8. Modelul de date (rezumat)

| Tabelă | Rol | Coloane fierbinți (indexate) | JSONB |
|---|---|---|---|
| `market_snapshots` | fotografia pieței trimisă la analiză | ts (ingestie), **bar_close** (cheie idempotență), symbol, regime, adx_h1, atr_pct_m15, spread_pct | features, news_digest |
| `decisions` | input + output LLM + guvernarea Risk Engine + manifest reproducere | ts, model, direction (**intern** BUY/SELL/NO_TRADE), confidence, sl_pct, tp_pct, risk_verdict, mode | ai_input, ai_output |
| `trades` | tranzacția + rezultatul reconstruit | status, symbol, side, mode, external_id, r_multiple, pnl, exit_reason | — |
| `system_versions` | manifest de versiuni pentru reproducere | component, version, ts | details |

- **Enum-uri intern/extern:** `decisions.direction` folosește vocabularul **intern** `BUY/SELL/NO_TRADE`; contractul **extern** `Buy/Sell/NoAction` apare doar în payload-ul `/purchase` (maparea într-un singur loc, `core.models.to_external_model_type`).
- **Idempotență snapshot:** cheie deterministă `UNIQUE(symbol, bar_close)` (nu `now()`), ca re-rularea aceleiași bare M15 să nu dubleze.
- **Indexare:** BRIN pe `ts`; B-tree pe `(symbol, regime, bar_close)`; index parțial pe `trades(status) WHERE status='open'` (reconciler); GIN pe JSONB doar ad-hoc.
- **`external_id` — unicitate AMÂNATĂ:** index **neunic** deocamdată; se promovează la `UNIQUE` abia după confirmarea cardinalității position/order/fill din ipax (Faza 4).
- **Constrângeri:** CHECK pe enum-uri (regime, direction, side, status, mode, risk_verdict), valori pozitive (prețuri, SL), și consistența statusului (`open`⇒fără `closed_at`; `closed`⇒`closed_at`+`exit_price`).

---

## 9. Moduri de rulare și poarta de promovare

| Mod | Execuție | Outcome | Când |
|---|---|---|---|
| **Shadow online** | virtual, dar quote de intrare **observat** live după decizie | modelat (latență reală) | validare inițială |
| **Replay istoric** | virtual pe date istorice | modelat (latență **modelată**) | backtesting/tuning |
| **Live** | `/purchase` real, volum minim | autoritativ (via trading_hands, sursă ipax) | doar după poartă |

**Poarta shadow→live** (blocată până sunt îndeplinite, cu incertitudine cuantificată): baseline fără LLM depășit; variantă fără feedback depășită; expectancy > 0 **net de costuri**; drawdown în toleranță; calibrare `confidence` măsurată; acoperire pe regimuri; **profit/fill autoritativ confirmat final**; scoping demo dovedit; reconciliere care recuperează toate închiderile după downtime. Detaliile protocolului walk-forward sunt în plan.
