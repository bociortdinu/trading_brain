# trading_brain — Plan de execuție

> Complementar la [ARCHITECTURE.md](ARCHITECTURE.md). Fazele sunt secvențiale ca
> dependențe; fiecare are **livrabile** și un **criteriu de ieșire (DoD)** verificabil.
> Regula de aur: nu treci mai departe fără DoD-ul fazei curente.

---

## Rezumat faze

| Fază | Titlu | Livrează | Stadiu (2026-07-18) |
|---|---|---|---|
| 0 | Fundație | schelet, config, client trading_hands, health-check | ✅ livrat |
| 1 | Colectare + Features | snapshots MTF reale în DB (fără AI) | ✅ replay validat; știri LIVE + paritate TradingView neverificate |
| 2 | Brain + Risk Engine | decizii validate (fără execuție) | ✅ livrat; DoD prompt caching nevalidat |
| 3 | Shadow Mode | execuție fantomă + reconciliere modelată | ✅ motor livrat; swap/comision reale + M1 + rulare continuă neverificate |
| 4 | Istoric autoritativ (ipax) | rezultat live autoritativ (subproiect) | 🔬 SPIKE (cere trade-uri live) |
| 5 | Feedback loop + evaluare | temporal-fold report + baseline-uri + feedback wired | ◑ parțial; verdict LLM = rulare plătită, amânat |
| 6 | Go-live controlat | live cu volum minim + kill-switch | ⛔ neînceput (bani reali; porțile 4+5) |

Cross-cutting (în toate fazele): manifest de reproducere, teste, discipline anti look-ahead.

---

## Stadiu actual (2026-07-18)

Instantaneu exact al proiectului. (Secțiunile „Status onest / Runda N" de mai jos sunt
**changelog-ul de audit** — cum am ajuns aici; nu le citi ca stare curentă.)

**Livrat și testat (Fazele 0–3 + framework-ul Fazei 5):**
- **Colectare + features**: providere OHLCV (XTB real-time via trading_hands, Polygon/Massive,
  CSV), doar bare închise, calendar de sesiune DST-aware, snapshot **imutabil** (OHLCV+features)
  separat de spreadul contextual (`spread_observations`, append-only) și de eligibilitate
  (`snapshot_evaluations`, per mode+policy). Backtest mult accelerat (indicatorii precalculați,
  ~12× pe termenul dominant) — **NU O(n) strict**: `_slice`/`validate_series`/filtrarea pivoților
  rămân O(n)/bară → O(n²) cu constantă mult mai mică.
- **Decizie + risc**: contract I/O strict (Structured Outputs), prefiltru, `AnthropicDecisionMaker`
  fail-closed (primul apel LLM real făcut), Risk Engine determinist (SL/TP din ATR, spread
  obligatoriu, sesiune pe **calendarul providerului**, fail-closed). Maker selectabil
  `deterministic|claude`.
- **Shadow Mode**: broker virtual (mid + cost round-trip plat + slippage + gap-through-stop +
  comision/swap dacă ratele sunt setate), reconciliere intrabar cu bandă pesimist/optimist,
  backtest + online continuu, **position gate** (o poziție o dată), **rezervare atomică**
  înainte de apelul plătit + **recovery după crash** (refolosește decizia persistată, nu
  reapelează maker-ul), lock exclusiv pe `run_id`.
- **Feedback loop + evaluare (Faza 5, deterministic)**: track record `as_of`-safe injectat în
  `DecisionInput` (și în backtest via `--feedback`); **temporal-fold report** cu baseline-uri care
  chiar tranzacționează (confluence + random; flat = linia zero care nu tranzacționează niciodată),
  bootstrap CI, drawdown, **discriminare** confidence (ordinal, NU ECE), acoperire pe regimuri.
- **Persistență + audit**: `decisions`/`trades`/`snapshot_evaluations`/`spread_observations`/
  `llm_calls`/`decision_reservations`; tabelele de fapte **append-only pentru app-role**
  (UPDATE+DELETE revocate; retenția = admin); identitatea snapshotului include **sursa**
  (symbol+provider+pipeline+bar_close); `schema_migrations` read-only pentru app-role.
- **trading_hands** (Go): endpoint `/candles` real-time (paginat), keepalive + **reconnect dovedit**
  cu mock CoreAPI, data race pe `account` reparat.

**Cifre reale:** **327 teste** (288 fără DB + 39 DB-gated pe o bază `_test` izolată). Categorii de
verificare: *fără-infra* și *DB-gated* rulate local; *live Compose*, *live XTB* și *CI remote* încă
NErulate (Docker daemon indisponibil în mediu; CI-ul n-a rulat verde încă). Go
`-race`/`vet`/`gofmt` curate, launcher Node **8 teste**.
**24 migrări** (0001–0024). Versiuni: features `1.2.0`, decision-schema `2026.3`, prompt `2026.1`,
strategy `2026.1`, risk `2026.2`.

**NU e făcut / deferit (onest):**
- **Măsurarea edge-ului real cu LLM** — cere o rulare **plătită** `--maker claude` (amânată de user).
  Framework-ul + baseline-urile deterministe există; lipsește doar rularea LLM.
- **Serviciu monitorizat** — `shadow.online`/`app.jobs` nu rulează ca daemon → nu se acumulează
  track record încă.
- **Faza 4 (ipax)** = SPIKE; **Faza 6 (go-live)** = neînceput (ambele cer execuție/close-uri reale).
- **Rate reale swap/comision** (numerele din xStation5 — modelul long/short + DST + triple-swap +
  valută + terms_version e FĂCUT, lipsesc doar ratele reale); **știri LIVE** (cere sursă/API key);
  `llm_calls` per-attempt (retry_count făcut, rânduri nu);
  exact-once la LLM = **imposibil** (Anthropic nu acceptă cheie de idempotency — închis);
  un **job de retenție** dedicat (admin) — tabelele de fapte sunt acum append-only pentru app-role;
  nivel 3 kNN/pgvector pentru feedback.

---

## Status onest (istoric — runda 3 de review extern)

> Changelog de audit. Reflectă starea de la momentul rundei, nu cea curentă (vezi „Stadiu actual").

Supraevaluări corectate (reviewer-ul a avut dreptate de fiecare dată):

- **NU** „audit închis integral / Faza 3 completă". Faza 3 e funcțională dar are datorii (mai jos).
- Backtest-ul determinist NU e un „no-edge robust pe 26 zile": semnalele s-au concentrat într-un
  singur regim intraday, iar rularea veche era un **event-study** cu până la 23 poziții suprapuse.
  Există acum un **position gate** (o poziție o dată) → executabil, dar fereastra rămâne limitată.
- Modelul de cost **nu** e „complet": comision/swap = 0 (**nemodelate**), un singur swap (fără
  long/short), rollover fix 22:00 UTC fără DST/triple-swap.
- „Idempotency end-to-end" (runda 2) era de fapt doar un **lookup**. Acum e reală: dedupe ATOMIC
  *înainte* de LLM + test care rulează backtestul de două ori.
- Sistemul **nu acumulează track record**: `shadow.online`/`app.jobs` nu rulează ca serviciu.
- Teste reale: **211 passed / 13 skipped** fără DB, **224** cu DB (raportasem greșit 207).
- Commit-uri: **13 în trading_brain, 5 în trading_hands** (raportasem greșit 15+4, apoi 11+3).

**Reparat în runda 3 (cu teste):**
1. **Dedupe ÎNAINTE de LLM + atomic** (migrarea 0011): `decisions.run_id` + `input_fingerprint`
   (input_hash ⊕ model ⊕ prompt/strategy/risk version ⊕ provider) cu UNIQUE parțial; `insert_decision`
   face `ON CONFLICT DO NOTHING` → două procese concurente nu mai pot insera dublu (fără TOCTOU).
   O re-rulare completă **face RESUME: zero apeluri plătite** (dovedit de test, nu doar de lookup).
   **Garanție onestă:** at-most-one-concurrent, NU exact-once — dacă un worker apelează modelul cu
   succes și moare înainte să persiste, după expirarea lease-ului alt worker reapelează (o plată
   duplicată). Fereastra se închide doar cu o cheie de idempotency acceptată de provider, pe care
   nu o avem. Nu pretindem „no duplicate charge".
2. **Guardrails financiare pentru `--maker claude`**: `--max-llm-calls` (cap hard, oprire curată),
   estimare worst-case de cost + confirmare explicită (`--yes`), `llm_calls` persistate și în
   backtest, clientul Anthropic închis în `finally`.
3. **Position gate + reconciliere-ÎNTÂI în Shadow Online** (înainte exista doar în backtest, iar
   reconcilierea rula după o eventuală deschidere → poziții suprapuse).
4. `opened_at` = `basis.observed_at` (timpul **local real** al observației, nu tickul brokerului).
5. **Bara parțială de intrare**: reconcilierul o ignora complet (poziția era imună la SL/TP până la
   următoarea graniță M15). Acum: politică **conservatoare** — stop-ul contează, TP-ul nu.
6. `cost_manifest`: rată `!= 0` (un swap **negativ** e aplicat → e „modeled"); migrarea 0012
   corectează manifestele FALSE ale celor 45 de trade-uri istorice (păstrate, dar cu afirmația reparată).
7. Docs: eliminate module inexistente (`brain/`, `risk_manager/`, `execution/` → reale: `decision/`,
   `risk/`, `shadow/`), „online blocat de feed plătit" (XTB e feed real-time gratuit), „următorul pas
   = primul apel LLM" (făcut). `.env.example` documentează costurile.
8. Captura ipax: `chmodSync(0600)` (mode-ul de la creare nu acoperea un fișier existent 0644).

**Reparat în runda 4 — separarea snapshot/spread (migrarea 0013):**

Root-cause-ul contaminării: `market_snapshots` purta `spread_pct`/`basis_observed`, care **nu sunt
proprietăți ale unei bare închise** — vin dintr-un quote live luat la un moment de observație. Cum
rândul era **ENRICHED** cu quote-ul mai târziu, snapshotul unei bare ajungea să afirme un spread pe
care decizia de pe aceeași bară nu-l folosise (snapshot 392: 0.0177 observat vs 0.02 modeled în input).

Modelul acum:
- `market_snapshots` = observație **imutabilă** OHLCV+features. NU mai are spread → **nu mai poate
  contrazice** o decizie. `upsert` nu mai face enrich pe spread (doar `data_quality` NULL).
- `spread_observations` = fapte **append-only** DESPRE un snapshot („la `observed_at`, cu această
  provenance, spreadul era X"). O bară poate avea zero (replay) sau mai multe.
- `decisions.spread_observation_id` = **exact** ce observație a consumat decizia (NULL = constantă
  modelată, păstrată în `ai_input`).
- **Guard-ul de contaminare**: `should_observe_spread(mode, is_latest)` — un quote descrie ACUM, deci
  doar bara `latest` **și** doar în `online`. Replay nu observă nimic (fail-closed pe mod necunoscut).

Pe cazul reviewer-ului: aceeași bară (snapshot 392) găzduiește acum decizia online (0.0177,
`observed_xtb`) și cea replay (0.02, `modeled`) **fără contradicție** — snapshotul nu mai afirmă
niciun spread, deci nu mai poate contrazice pe niciuna. **Corectură:** raportasem că decizia online
de pe 392 e „legată de observația ei" — **fals**. Ambele decizii istorice au `spread_observation_id
= NULL`: coloana a apărut după ce au fost scrise, iar legătura NU a fost inferată retroactiv.
Legarea e dovedită doar pentru decizii NOI (test dedicat). Teste: imutabilitatea snapshotului,
append-only + idempotent, replay-never-observes, online-și-replay-pe-aceeași-bară.

**Datorii deschise (oneste, NEreparate):**
- Swap real long/short + DST + triple-swap; reconcilierea folosește configul CURENT, nu cel salvat în
  `costs` la deschidere; ratele nu se reconstruiesc din `costs` pentru pozițiile deschise.
- `decisions.spread_observation_id` e NULL pentru deciziile **istorice** (scrise înainte de coloană);
  nu le-am inferat retroactiv legătura.
- Perf backtest **O(n²)** (rebuild MTF per bară) — blochează ferestre foarte mari.
- `llm_calls` agregat după retry (fără `retry_count`, fără FK direct la `decision_id`).
- DoD prompt caching nevalidat (apelul real: cache 0/0). News live neconectat.
  `max_clock_skew_seconds` nu intră în hash-ul policy-version.
- **Leak de subscribe la heartbeat: NEVERIFICAT** (vezi runda 5 — nu l-am „reparat" cu o presupunere).
- Faza 4 = **spike**. Edge real cu LLM = plătit, amânat de user.

**Reparat în runda 5 — reconnect XTB dovedit (trading_hands):**
- Înainte: reconnect-ul era doar *argumentat*. Testele Go acopereau exclusiv căile no-op
  (deja-alive / closed) — niciunul nu dovedea că sesiunea chiar se **recuperează**.
- De ce lipsea testul: `NewClient` impune allowlist pe endpoint (`api5demoa.x-station.eu`), deci
  un mock local era imposibil de folosit. Soluție: **seams package-private** (`dialFn`/`ticketFn`),
  setate de `NewClient` la implementările reale — **allowlist-ul rămâne intact** pentru orice
  apelant real; doar testele (același pachet) le înlocuiesc.
- `xstation/reconnect_test.go`: **mock CoreAPI WebSocket** (register/login/balance, răspunsuri pe
  reqId ca API-ul real + drop controlat). Testul: connect → alive → drop → heartbeat eșuat →
  re-establish → alive pe o conexiune **NOUĂ** + re-login + sesiunea recuperată chiar funcționează.
  Plus: `Close` oprește definitiv reconectarea (nu mai redial-ează).
- **Data race real reparat**: `parseLogin` rescrie `c.account` la fiecare reconnect, iar
  `Account()`/`GetBalance` îl citeau nesincronizat → `accountMu`. Testul a fost **validat că are
  dinți**: pe codul vechi `-race` raportează `WARNING: DATA RACE` (write vs read), pe cel nou trece.
  (Prima versiune a testului NU prindea race-ul — dormea fix și fereastra se închidea cu un singur
  login, deci fără scriere concurentă; acum așteaptă re-login-uri reale.)
- 23 teste xstation; `go test -race ./...`, `vet`, `gofmt` curate.

**Leak-ul de subscribe — de ce NU l-am reparat:** heartbeat-ul face `getAndSubscribeElement(eid 1043)`
la fiecare tick fără unsubscribe. Dacă serverul acumulează o subscripție per apel (leak) sau tratează
repetarea aceluiași eid ca idempotentă **nu e stabilit** — sesiuni de ore fără degradare vizibilă e
sugestiv, nu dovadă. `unsubscribeElement` **este** o comandă reală (GetQuote o folosește pe eid 2, cu
`keys`), dar forma fără `keys` de care ar avea nevoie eid 1043 nu a fost niciodată exercitată pe API-ul
live, iar serviciul e oprit acum → nu pot valida. Am documentat-o în cod în loc să livrez o formă
ghicită drept „fix".

---

## Runda 6 — ce a găsit reviewul și ce am reparat

**Cea mai gravă era a mea:** migrarea 0013 a **FABRICAT provenance**. A mutat `spread_pct` din
snapshot în `spread_observations` hardcodând `'observed_xtb'`, presupunând că orice spread stocat
venea de la un quote XTB. Fals: backtestul scrisese și el spreadul **modelat** (0.02) în acea coloană
→ **43 din 47** de rânduri pretindeau un quote de broker care nu existase niciodată. Exact
falsificarea pe care tabela există s-o prevină. **Migrarea 0014** le reetichetează `modeled`
(semnalul e fără echivoc și a fost verificat pe date: `basis IS NOT NULL` ⟺ quote real, cu
`quote_time` + `basis.xtb_spread_pct` potrivit — 4 rânduri; `basis IS NULL` ⟺ modelat — 43, toate
0.02, toate cu decizii care spun `modeled`). Nu am rescris 0013 (deja aplicată).

**Rezervare ATOMICĂ înainte de model (0016).** 0011 făcea unic RÂNDUL, nu **PLATA**: secvența era
`SELECT → LLM → INSERT ON CONFLICT`, deci doi workeri concurenți rataţi amândoi la SELECT plăteau
amândoi, iar unicitatea doar arunca rândul perdantului. Acum claim-ul se ia **înainte** de apel,
atomic, cu **lease** (un worker mort nu blochează inputul pe veci; `done` e terminal, `failed`
reîncercabil). Dovadă măsurată, nu argumentată: sub un barrier cu 8 workeri, logica veche →
**8/8 ar fi plătit**; cea nouă → **exact 1/8**.

**Resume-ul reconstruiește starea.** Înainte marca `resumed` și mergea mai departe, uitând orice
poziție deschisă → putea stivui a doua peste ea. Acum reîncarcă decizia/trade-ul și **`busy_until`**.
Test: rulare tăiată la jumătate → resume → **trade cu trade identic** cu o rulare neîntreruptă
(verificat că testul pică fără restaurarea `busy_until`).

**Fill imposibil pe bara parțială.** Pe bara de intrare parțială foloseam `_stop_exit_ref`, care
modelează gap-through-stop din `bar.open` — dar acel open e **anterior intrării**. O bară deschisă la
3900 sub un stop de 3988 „umplea" la 3900: o pierdere luată înainte ca trade-ul să existe (~−8R în
loc de −1R). Acum: cel mult stopul + slippage.

**`--maker claude` DEZACTIVAT în Shadow Online** — gardurile (cap, estimare, confirmare, închiderea
clientului) există doar în backtest; bucla online e nelimitată. Fail-closed până există și acolo.

**Integritate spread:** FK **compus** (0015) — o decizie nu mai poate indica observația altui
snapshot. **Append-only chiar impus**: nu era nicăieri (doar comentarii), iar GRANT-ul global dădea
UPDATE pe tot → acum `REVOKE UPDATE` pe `spread_observations`/`snapshot_evaluations`/`llm_calls`
(verificat: UPDATE refuzat real). DELETE rămâne (retenție + CASCADE) — trade-off documentat, mai slab
decât append-only strict.

**Go:** `Close()` în timpul unui dial în zbor putea instala o conexiune **după** shutdown (verificarea
`closed` era stală după apelurile de rețea) → re-verificare sub `connMu`, același lock pe care-l ia
Close. Testul pică pe codul vechi cu „connection installed AFTER Close".

**Docs:** `/candles` lipsea (8 endpointuri, nu 7); „fără reconnect auto" era fals; modelul de fill era
descris ASK/BID deși implementarea e **mid + cost plat**.

---

## Runda 7 — rezervarea era doar parțial corectă

Raportasem „toate 8 închise". **Greșit**: rezervarea și resume-ul erau incorecte sub concurență.
Reviewer-ul a avut dreptate; fiecare afirmație a fost reprodusă local înainte de a fi acceptată.

1. **Lease fără ownership.** `complete_decision_reservation` scria după `(fingerprint, run_id)`, fără
   să verifice cine deține claim-ul. Reprodus: A rezervă → lease-ul expiră → B preia → A, stale,
   completează → `status=done, worker=B, decision_id=NULL`, iar orice worker ulterior primește
   definitiv „done" pentru o decizie **inexistentă**. Fix: `claim_token` per claim + finalizare
   **CAS** (`WHERE status='in_progress' AND claim_token=:token`) care trebuie să atingă exact un
   rând, altfel `StaleClaimError`.
2. **Cap-ul lăsa rezervarea abandonată.** Claim-ul se lua înainte de verificarea capului. Măsurat cu
   cap=0: `[('in_progress', 1)]` rezervări, 0 decizii. Fix: capul se verifică **înainte** de claim
   (verificat: 0 rezervări rămase).
3. **Concurența rupea position gate-ul** — cel mai important. Rezervarea garantează un apel per
   fingerprint, dar backtestul e **stateful** (`busy_until`): doi workeri împart barele și fiecare
   își ține propria poziție. Măsurat: 26/25 apeluri, 19 trade-uri, **11 perechi suprapuse**. Fix:
   **lock exclusiv pe `run_id`** (advisory, fail-closed) — un experiment stateful nu se
   paralelizează pe bare. Re-măsurat cu lock: al doilea worker **refuzat**, **0 suprapuneri**.
   `held` sub lock e acum invariant violat → eroare, nu „sari bara".
4. **Resume incomplet**: `busy_until` nu se seta pentru un outcome încă **open** (acum: blochează
   până la finalul datelor, ca în rularea live); dispoziția `blocked_position_open` nu era
   persistată (acum: `decisions.blocked_reason`, decisă *înainte* de insert); `done` se putea marca
   deși `_persist_decision` întorsese `None` (acum: `done` doar dacă o decizie chiar a aterizat).
5. **Migrarea 0014 stricase propriul discriminator**: scrisesem nota de migrare **în `basis`**, deci
   toate cele 47 de rânduri deveniseră `basis IS NOT NULL` — exact opusul regulii pe care o
   documentasem („`basis NOT NULL` ⟺ quote real"). 0017 pune `basis = NULL` la cele modelate; nota
   stă în comentariul migrării, nu într-o coloană de date. Verificat: invariantul ține din nou.
6. **„Append-only" era impropriu.** E **UPDATE-protected**, nu append-only: DELETE rămâne acordat
   (retenție + CASCADE), deci delete+reinsert poate emula un update. Documentat ca atare, nu
   pretins rezolvat. `schema_migrations` e acum **read-only** pentru app-role (nu mai poate falsifica
   istoricul migrărilor).
7. **Docs**: ASK/BID → mid + cost plat; `execution/reconciler.py` (inexistent) → `shadow/reconciler.py`;
   `record_shadow_trade` → `upsert_shadow_trade`; concluzia „toate sl_hit" marcată ca **superseded**
   (event-study fără position gate, fereastră îngustă, manifest greșit).

**Datorii rămase (oneste):** DELETE într-un rol separat de retenție (append-only real); swap
long/short + DST + triple; reconcilierea folosește configul curent, nu cel salvat în `costs`;
`llm_calls` per-attempt; perf O(n²); Faza 4 = spike; edge real cu LLM = neplătit, nemăsurat.

---

## Runda 8 — starea terminală a rezervării + fereastra decizie→trade

Reviewer-ul a avut din nou dreptate; fiecare afirmație reprodusă local înainte de fix.

1. **DB permitea `done` fără decizie** (0018). `done, decision_id=NULL` era acceptat → orice worker
   ulterior primea „already decided" pentru o decizie inexistentă. Trei constrângeri DB (+ validare
   în repository): `done ⇒ decision_id NOT NULL`, `in_progress ⇒ claim_token NOT NULL`, și **FK
   compus** care leagă `decision_id` de **același** `(input_fingerprint, run_id)`.
2. **Fereastra decizie→trade.** `done` se elibera înainte de trade, deci un crash între ele lăsa
   decizie + rezervare `done` + **trade inexistent**, iar resume-ul îl considera final. Acum `done`
   se eliberează **după** persistarea trade-ului: un crash lasă `in_progress`, lease-ul expiră,
   iar resume-ul re-revendică, refolosește decizia și **reconstruiește trade-ul** (self-healing).
   Test dedicat: crash injectat între decizie și trade → resume → identic cu rularea curată.
3. **Exact-once NU e garantabil sub crash** — documentat onest: **at-most-one-concurrent**, nu
   exact-once. Un crash după apelul reușit dar înainte de persistare → reapelare (plată duplicată).
   Fără cheie de idempotency acceptată de provider, fereastra e inevitabilă.
4. **Teste de resume dedicate** (nu doar tabela trades): raport complet egal cu rularea curată;
   `blocked_position_open` persistat (`decisions.blocked_reason`) și reconstruit; crash între
   decizie și trade; eroare la persistarea trade-ului.
5. **Bug real găsit de detectorul corectat de overlap.** Testul vechi (`closed_at or opened`) era
   **orb la trade-urile deschise**. Detectorul corect (COALESCE …'infinity') a expus un bug de
   producție: un trade care rămâne **open** până la finalul datelor seta `busy_until` la close-ul
   ultimei bare, iar bara de graniță (`as_of == busy_until`, gate `<` strict) deschidea o **a doua
   poziție**. Fix: un trade deschis blochează **tot restul rulării** (`_FOREVER`), pe calea live ȘI
   la resume. Re-măsurat: 0 suprapuneri.
6. **`run_lock` ținea o tranzacție deschisă** (`idle in transaction` pe toată durata backtestului).
   Fix: conexiune `autocommit=True` (verificat: 0 idle-in-transaction).

**Datorii rămase (oneste):** cheie de idempotency provider (exact-once real); DELETE într-un rol
separat de retenție (append-only real); swap long/short + DST; reconcilierea folosește configul
curent nu cel salvat; `llm_calls` per-attempt; perf O(n²); Faza 4 = spike; edge real = nemăsurat.

---

## Runda 9 — „self-healing"-ul era el însuși defect

Afirmasem că resume-ul „refolosește decizia și reconstruiește trade-ul". **Fals în implementare** —
reviewer-ul a reprodus, iar eu am confirmat (46 apeluri la maker în recovery, 1 lanț `done` fără trade).

**Bug-ul (reprodus):** după re-revendicarea unui lease expirat, codul **reapela maker-ul**. Abia apoi
`insert_decision ... ON CONFLICT` întorcea id-ul deciziei **vechi**. Cu un maker care întorcea alt
verdict la resume (BUY→NO_TRADE), sistemul lega (sau nu) un trade nou de decizia veche approved →
`done` fără trade. Invariantul „done = lanț complet" încălcat. Testul meu nu prindea asta: același
maker determinist + fără verificarea numărului de apeluri.

**Reparat:**
- **Ramură reală de recovery.** După „reserved", dacă există deja o decizie pentru
  `(input_fingerprint, run_id)` → **NU** se apelează maker-ul. Trade-ul se reconstruiește **exclusiv**
  din decizia persistată (direcție, SL, TP — deterministe din decizie + fereastră), printr-un helper
  partajat cu calea fresh (trade identic). Dacă trade-ul există deja → doar validează + finalizează.
- `insert_decision` întoarce acum explicit `(decision_id, inserted)`; calea fresh **eșuează zgomotos**
  la un conflict neașteptat (un `rec` nou nu mai poate fi continuat peste o decizie veche).
- **Dovedit cu dinți:** primul run BUY, un bar corupt (trade șters + rezervare resetată), resume cu
  maker care ar zice NO_TRADE → `second.calls == 0`, trade-ul reconstruit `buy` (din decizia
  persistată, nu NO_TRADE), rezervarea `done` legată de decizia ei. + test crash-după-trade
  (rezervare `in_progress`, trade există) → resume validează, nu duplică, `done`.
- **Migrarea 0018 repară înainte de constrângere.** Adăuga constrângerile direct — pe o bază cu
  rânduri legacy corupte (`done`+decizie NULL) ar fi **eșuat** la ADD CONSTRAINT. Acum resetează întâi
  rândurile invalide la `failed` (reclaimabile). Validat: no-op pe baza curată.

---

## Runda 10 — reconcilierea onorează ratele de la deschidere

Fără review nou; am închis o datorie proprie semnalată în rundele anterioare.

**Problema:** `reconcile_open_trades` reconcilia pozițiile deschise cu ShadowConfig-ul **curent**, nu
cu cel de la deschidere. O schimbare de config (ex. `swap_pct_per_night`) între deschidere și
închidere re-preța silențios R-ul unei poziții deja deschise; ratele nu se reconstruiau din `costs`.

**Reparat:** `cost_manifest` stochează acum și `rollover_hour_utc` + `conservative_partial_entry`;
`open_shadow_trades` întoarce `costs` + `timeout_bars`; `shadow_config_from_costs(...)` reconstruiește
ShadowConfig-ul de la **deschidere**, iar `reconcile_open_trades` reconciliază fiecare trade cu al
**lui** (configul curent = doar fallback pentru manifeste legacy). Dovedit cu dinți: trade deschis cu
swap 0.05 ținut peste un rollover, reconciliat cu config curent swap=0 → R **net de swap-ul de la
deschidere**, nu de 0 (testul pică pe codul vechi).

**Datorii rămase (oneste):** cheie de idempotency provider (exact-once real); DELETE într-un rol
separat de retenție (append-only real); swap long/short + DST/triple; `llm_calls` per-attempt;
perf O(n²); Faza 4 = spike; edge real = nemăsurat.

**Ordinea recomandată** (per reviewer): ~~snapshot/spread~~ (r4) → ~~mock reconnect~~ (r5) →
~~rezervare atomică + provenance + resume~~ (r6–7) → ~~stare terminală + fereastra decizie→trade~~
(r8) → ~~recovery real fără reapelarea maker-ului~~ (r9) → **urmează**: pornirea monitorizată a Shadow
Online (doar maker determinist). Rămâne valabil: **nu** rula `--maker claude` pe mii de bare.

---

## Runda 11 — datorii proprii închise (fără review)

- **Reconciliere cu configul de la deschidere** (mai sus, r10): un config schimbat între deschidere
  și închidere nu mai re-preţează silențios R-ul unei poziții deschise.
- **Cheie de idempotency la provider — DEAD-END VERIFICAT.** SDK-ul Anthropic are infrastructura
  (`_idempotency_header`), dar clientul **nu setează niciodată** header-ul → cheia nu se trimite, iar
  API-ul nu deduplică. Deci exact-once chiar **nu** e posibil la Anthropic; „at-most-once" documentat
  e corect și inerent. Nu am livrat un „fix" no-op.
- **Audit `llm_calls` întărit** (migrarea 0019): `retry_count` (câte retry-uri transiente înainte de
  rezultat) + `decision_id` (leagă apelul plătit de decizia produsă; NULL pentru un apel eșuat).
  Apelul se logează acum **după** decizie (tabela e UPDATE-protected, deci legătura se scrie la
  INSERT, nu prin UPDATE ulterior). **Parțial:** logez `retry_count`, NU fiecare attempt ca rând
  separat — datoria rămâne notată ca atare.

---

## Runda 13 — Faza 5 nu era încă validă + fixuri de corectitudine

Reviewul a arătat că Faza 5 nu era un protocol valid și a găsit două bug-uri de corectitudine.
Reproduse local înainte de fix; „framework livrat" era prea tare.

- **Baseline-ul random nu tranzacționa** (confidence 0.5 < prag 0.60) → „bate random" era fals.
  Fix: `RandomMaker` emite confidence peste prag; test care **dovedește** trades > 0.
- **„Walk-forward" nu era walk-forward**: tăia istoricul per fold (pierdea warm-up + poziția, folduri
  < MIN_BARS → goale). Redesign: **o singură rulare continuă**, apoi partiționez **rândurile** pe
  segmente temporale (`temporal_folds`). Redenumit onest **temporal fold report** — un walk-forward
  real (train→OOS) cere un maker antrenabil (LLM, amânat).
- **Feedback-ul nu ajungea la LLM în backtest**: acum `backtest_over_windows(use_feedback=...)` +
  `shadow.runner --feedback` injectează track record-ul; comparația cu-feedback vs fără devine
  rulabilă sub `--maker claude`. Feedback-ul intră în `input_hash`.
- **Recovery nereproductibil la schimbarea configului**: rebuild-ul folosea configul rulării de
  resume. Fix: **execution config-ul (spread/slippage/comision/swap/rollover) intră în fingerprint**
  → o schimbare de config = decizie diferită, nu un recovery fals. Test.
- **Auditul llm_calls putea pierde un apel plătit** (INSERT în tranzacție separată). Fix: decizia +
  `llm_calls` într-**o singură tranzacție** (`insert_decision(llm_result=...)`); un eșec de audit
  face rollback la decizie. Test injectează eroarea → ambele 0.
- **Feedback look-ahead**: filtra pe `closed_at`. Adăugat `outcome_observed_at` (migrarea 0020) —
  când s-a **observat** rezultatul (online = timpul de reconciliere), nu când s-a atins prețul.
- **ECE pe confidence ordinal** → înlocuit cu **discriminare** (win rate per bucket + monotonicitate +
  spread), cu notă că ECE cere calibrare probabilistică pe train.
- **apiKey în excepții** (HttpNewsProvider) → redactat; nu mai apare URL-ul cu cheia.
- **Docs**: `O(n)` → onest O(n²) cu constantă mică; „full cost model" → „comision/swap doar dacă
  ratele sunt setate"; README migrări = **admin-role**; faze 1–3 cu gaps notate; commit count corect.

---

## Runda 12 — perf backtest O(n²) → O(n) *(vezi runda 13: „O(n)" era prea tare)*

`timeframe_features` era **87% din timpul unui backtest** (profilat): recalcula toți indicatorii
peste tot prefixul la fiecare bară (buclele Python din `_rma`/`ema`) → O(n²).

**Reparat, provably identic:** `TimeframeSeries` precalculează arrays-urile per timeframe **o
singură dată** și citește feature-urile la orice poziție `p` prin indexare. E **exact**
`timeframe_features(candles[:p+1])`, nu o aproximare — pentru că (a) ema/atr/rsi/adx sunt seed-uite
de la bara 0 și recursive, deci `arr[p]` = ultima valoare peste prefix (position-independent), și
(b) un pivot swing la `i` e „confirmat" abia la `i+right`, deci pivoții știuți la `p` sunt exact cei
cu `i ≤ p-right` = `swing_highs(high[:p+1])`. `build_feature_packet(precomputed=...)` schimbă **doar**
sursa feature-urilor tf; restul (anchor, data_quality, confluence, assembly) e aceeași cale de cod.

**Garanții:** `features_at(p)` == per-slice **byte-for-byte** pe uptrend/downtrend/oscillating la
fiecare bară (toate ramurile de regim + pivoți S/R); backtestul fast == slow (rânduri + metrici
identice). **12× speedup** măsurat (2500 bare: 26.7s → 2.25s), scalare liniară.

**Datorii rămase (oneste):** exact-once (imposibil la Anthropic — închis ca „nu se poate"); DELETE
într-un rol separat de retenție (append-only real); swap long/short + DST/triple; `llm_calls`
per-attempt (retry_count făcut, rânduri per-attempt nu); Faza 4 = spike; edge real = nemăsurat.

## Faza 0 — Fundație

**Scop:** un schelet care rulează și confirmă că vedem date reale de la trading_hands.

- Folosește numele corect `trading_brain/`; elimină folderul-typo gol `traiding_brain/`.
- `pyproject.toml` (Python 3.12+), structura de module din arhitectură, `settings.py` tipizat
  (URL trading_hands, DSN Postgres, chei API, timeframes, praguri de risc).
- `brokers_bridge/trading_hands.py`: client async tipizat pentru cele 8 endpointuri (incl. `/candles`).
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

*B. Online readiness — ✅ DEBLOCAT (feed real-time gratuit via XTB):*
- Free-tier Polygon **întârzie** datele → e bun doar pentru replay/backtest (snapshot-urile online
  ar fi corect `stale`).
- Rezolvat **fără abonament**: `BRAIN_MARKET_DATA_PROVIDER=xtb` ia bare real-time din trading_hands
  (CoreAPI xStation5, sesiune auto-menținută). Shadow Online **nu mai e blocat de feed**.
- Rămâne de rulat ca **serviciu monitorizat** ca să acumuleze track record (vezi „Status onest").

*C. Amânate:*
- **Știri LIVE** → primul subpas al Fazei 2 (interfața + `as_of` + provider static există).
- Paritate indicatori vs. TradingView pe bare reale (pas manual).
- Fix-ul de căutare are efect live doar după **rebuild/restart** trading_hands (înainte de smoke-ul final Phase 0).

---

## Faza 2 — Brain + Risk Engine (decizii, FĂRĂ execuție)

**Scop:** transformi features în decizii validate, ieftin și reproductibil.

- `brain/schemas.py`: `AIDecision` Pydantic (`direction: BUY|SELL|NO_TRADE`, `confidence`,
  `invalidation`, `rationale`, opțional `risk_preset: tight|normal|wide`).
> Notă de nume: modulele livrate sunt [decision/](../decision/) și [risk/](../risk/)
> (planul iniţial le numea `brain/` / `risk_manager/` — acele foldere NU există).

- [decision/schema.py](../decision/schema.py): pachet JSON (features + știri `as_of` + feedback
  placeholder); partea statică (reguli + schemă) separată pentru **prompt caching (TTL 1h)**.
- [decision/llm_client.py](../decision/llm_client.py): Anthropic SDK, `messages.parse()` cu
  Structured Outputs. Model: `claude-sonnet-5` decizie (configurabil).
- [decision/prefilter.py](../decision/prefilter.py): taie apelul LLM când nu e setup
  (regim/ADX/nivel/spread/sesiune).
- [risk/engine.py](../risk/engine.py): **SL/TP determinist** (`SL = k·ATR`, `TP = R·SL`); validare
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

Module decizie livrate (FĂRĂ execuție; DecisionMaker injectat, fake în teste):
[decision/schema.py](../decision/schema.py) · [decision/prefilter.py](../decision/prefilter.py) ·
[risk/engine.py](../risk/engine.py) · [decision/pipeline.py](../decision/pipeline.py) ·
[decision/llm_client.py](../decision/llm_client.py).

**Primul apel LLM real: FĂCUT** (Anthropic SDK, Structured Outputs, fail-closed; apelul e persistat
în `llm_calls` cu cost/tokens/request_id). Maker-ul e selectabil: `--maker deterministic` (gratuit,
default) sau `--maker claude` (plătit — cu cap `--max-llm-calls`, estimare de cost și confirmare).
**Neîncă făcut:** măsurarea edge-ului real cu LLM pe o fereastră mare (cost real, amânat de user)
și validarea DoD-ului de prompt caching (`cache_read_input_tokens > 0` — apelul real a raportat 0/0).

---

## Faza 3 — Shadow Mode (execuție fantomă + reconciliere modelată)

**Scop:** măsori dacă există *vreun* edge, cu costuri corect modelate, fără bani reali.

- `shadow/virtual_broker.py` — **LIVRAT, dar NU ca ASK/BID** (planul inițial cerea asta): barele
  sunt tratate ca **MID**, iar spreadul e dedus ca **un cost round-trip plat** din R (+ slippage
  advers la intrare/ieșire, gap-through-stop, comision/swap dacă ratele sunt setate). E o
  aproximare documentată, nu o simulare bid/ask. Nu folosim balance/equity ca PnL.
- Verificare **intrabar** SL/TP (M1 dacă e disponibil). Când SL și TP cad în același interval:
  raportează **bandă pesimist (SL-first) / optimist (TP-first)** + **rata de ambiguitate**; nu elimina cazurile.
- Separă **shadow online** (quote de intrare observat live după decizie; latență reală) de
  **replay istoric** (latență **modelată**, ex. fill la open-ul M1 următor + slippage).
- **`execution/` NU există**; reconcilierea shadow e în [shadow/reconciler.py](../shadow/reconciler.py):
  închide virtual pe bare post-intrare; calculează
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
- `database.repository.upsert_shadow_trade` (nu `record_shadow_trade` — a fost înlocuit) → tabelul
  `trades` (`mode='shadow'`, benzi + ambiguitate, idempotent pe `(decision_id, run_id)`, monoton).
Teste (curent): `tests/test_shadow.py` (23, incl. politica conservatoare pe bara parțială) +
`tests/test_shadow_runner.py` (7, incl. position gate, cap `--max-llm-calls`, fast==slow) +
`tests/test_repository.py` (30, incl. lanțul complet snapshot→eval→decizie→trade, upsert monoton,
rezervare atomică/recovery, feedback `as_of`-safe) — **resume real**: backtestul rulat de două ori
→ 0 apeluri LLM repetate, 0 duplicate.

**Livrat (post-audit):**
- **Backtest replay** — [shadow/runner.py](../shadow/runner.py): `backtest_over_windows` + spread modelat (`replay_spread_pct`); metrici de edge (win rate, expectancy R, bandă ambiguitate); `--persist` scrie lanțul complet (snapshot→eval→decizie→trade) cu `run_id`. Rulat real pe XTB. **Concluzia „nu are edge — toate sl_hit" NU se susține**: acea rulare era un
event-study fără position gate (până la 23 poziții suprapuse), pe o fereastră intraday îngustă, cu
manifestul de cost greșit. A fost superseded; edge-ul real nu e măsurat.
- **Shadow online continuu** — [shadow/online.py](../shadow/online.py): pe fiecare tick decide + (dacă aprobat) deschide trade `open`, iar la tick-urile următoare `reconcile_open_trades` închide ce a atins SL/TP (idempotent). Rulat live pe XTB (a deschis un SELL shadow, `observed_xtb`). `--once` sau buclă la fiecare M15.
- **Persistență completă** — `decisions`, `snapshot_evaluations`, `trades` (idempotent, run_id, benzi), `llm_calls` (orice apel incl. eșec + cost).

**Rămâne** (vezi și „Status onest" de sus): swap/comision reale (2 numere din xStation5) + model de
swap fin (long/short, DST, triple-swap); rularea shadow ca **serviciu monitorizat** ca să acumuleze
track record; **măsurarea edge-ului real** = rularea cu makerul LLM (`--maker claude`, plătit, amânat).
Paginarea Go pentru backtest adânc și position gate-ul executabil sunt **livrate** (nu mai sunt aici).

---

## Faza 4 — Istoric autoritativ (SUBPROIECT în trading_hands)

> **Stadiu: SPIKE, NU implementată.** Există doar endpoint-urile descoperite + encoding-ul
> (grpc-web-text) + o unealtă de captură — vezi [IPAX_CLOSED_POSITIONS.md](../../trading_hands/docs/IPAX_CLOSED_POSITIONS.md).
> Lipsesc: clientul gRPC-Web, auth refresh, schema răspunsului, backfill/paginare, persistență
> idempotentă, endpoint `/trades/closed`, corelarea `external_id`, un trade demo închis verificat
> end-to-end. Se deblochează abia când executăm live (contul demo n-are poziții închise de citit).

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

### Progres — livrat (partea deterministă, testată)

- **Feedback loop** — [database/feedback.py](../database/feedback.py): nivel 1 (statistici pe regim:
  win rate + expectancy) + nivel 2 (ultimele K trade-uri închise, verbatim-ish). **STRICT
  anti-look-ahead**: o decizie la `as_of` vede DOAR trade-uri al căror rezultat a fost **observat
  înainte** de `as_of` (`outcome_observed_at`, nu `closed_at` — anti-injecție după downtime; test
  dedicat). `FeedbackContext` e injectat în `DecisionInput` (parte din input hash), în calea online
  ȘI în backtest (`--feedback`), ca să fie rulabilă comparația cu-feedback vs fără sub `--maker claude`.
- **Temporal-fold report** — [shadow/evaluation.py](../shadow/evaluation.py): **baseline-uri** care
  chiar tranzacționează (confluence = fără-LLM; random seeded, confidence peste prag; flat=0);
  **bootstrap CI** pentru expectancy; **max drawdown** în R; **discriminare confidence** (win rate
  per bucket + monotonicitate + spread — NU ECE, fiindcă confidence e ordinal); **acoperire pe
  regimuri**; **folduri temporale** (o singură rulare continuă, output partiționat pe timp — NU
  walk-forward real). CLI: `python -m shadow.evaluation --count N --folds K`. Teste:
  `tests/test_evaluation.py` (funcții pure pe valori cunoscute + baseline-urile chiar deschid trade-uri).
- **Nivel 3 kNN/pgvector** + walk-forward real (train→OOS, calibrare probabilistică): amânate.

**Neîncă făcut (onest):** verdictul „LLM bate baseline-ul fără-LLM și fără-feedback, net de costuri"
cere o rulare **plătită** cu `--maker claude` (amânată de user). Framework-ul + baseline-urile
deterministe + wiring-ul de feedback + toate metricile există; lipsește doar rularea LLM.

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
