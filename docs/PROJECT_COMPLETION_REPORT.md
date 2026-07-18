# Raport de finalizare și utilizabilitate

**Data auditului:** 2026-07-18  
**Repo-uri analizate:** `trading_brain`, `trading_hands`  
**Verdict:** proiectul este un **MVP tehnic bun pentru colectare, replay și shadow trading**, dar
nu este încă un sistem autonom de trading utilizabil în siguranță nici măcar pe cont demo. Nu
trimite ordine din `trading_brain`, nu recuperează autoritativ rezultatele pozițiilor închise și nu
există încă dovada statistică a unui avantaj al strategiei.

## 1. Rezumat foarte scurt

`trading_hands` se autentifică în contul XTB demo, citește cotații și lumânări și expune un API HTTP.
`trading_brain` transformă barele M15/H1/H4/D1 în indicatori, cere sau simulează o decizie
BUY/SELL/NO_TRADE, aplică reguli deterministe de risc și salvează întregul traseu în PostgreSQL.
În prezent rezultatele sunt evaluate numai în **shadow mode**; dashboardul arată starea și auditul,
iar ordinele reale nu sunt conectate intenționat.

Fluxul actual:

```text
XTB demo -> trading_hands -> OHLCV/quote -> features -> prefiltru -> decizie
         -> Risk Engine -> DB -> trade virtual/reconciliere -> metrici/dashboard
```

Fluxul lipsă pentru automatizare:

```text
Risk Engine -> gate-uri stateful -> router live -> /purchase -> poziție XTB
            -> rezultat autoritativ ipax -> reconciliere -> kill-switch/alertare
```

## 2. Ce este deja funcțional și verificat

| Zonă | Stare | Dovezi |
|---|---|---|
| XTB demo: login, quote, instrumente, poziții, lumânări | Implementat | API `trading_hands`; candles M15/H1/H4/D1 validate live anterior |
| Sesiune CoreAPI | Implementat cu limită | keepalive și reconnect testate cu mock WebSocket; expirarea TGT cere reautentificare în browser |
| Colectare și features MTF | Implementat | bare închise, validare temporală, indicatori, quality/eligibility, calendare per provider |
| Persistență și audit | Implementat | migrări 0001–0023; snapshot, spread, evaluare, decizie, apel LLM, trade și manifest separate |
| Risk Engine | Implementat pentru shadow | fail-closed, spread/sesiune/confidence, SL/TP determinist; execuția rămâne explicit blocată |
| Backtest/shadow | Implementat ca motor | o poziție per run, resume, deduplicare, benzi pentru ambiguitate intrabar, metrici |
| Dashboard operator | Implementat | read-only, arată sănătatea, traseul deciziei, run-uri, cost/audit și alerte |
| Teste curente | Verzi | Python: **241 passed, 34 skipped** fără DB; Go: `go test -race ./...` și `go vet ./...`; launcher Node: **8 passed** |

Schema bazei active este la versiunea `0023_operational_observability` și corespunde codului.

## 3. Starea reală observată la audit

La momentul auditului:

- `trading_hands` nu rulează și dashboardul raportează `XTB_DISCONNECTED`;
- schedulerul și `shadow.online` nu rulează; ultimele heartbeat-uri sunt stale;
- baza are 57 snapshoturi, dintre care **51 sunt date de test**;
- există **12 trade-uri shadow legacy rămase open**, în run-uri neverificate;
- singurul run cu manifest verificat are o decizie și **zero trade-uri**;
- dashboardul raportează corect `NO_VERIFIED_TRACK_RECORD`;
- există o decizie Claude istorică fără rând corespondent în `llm_calls`; trebuie etichetată drept
  legacy/neauditabilă sau remediată printr-o regulă de migrare, nu inventat un audit;
- costurile curente sunt `commission_pct=0` și `swap_pct_per_night=0`, deci rezultatele nu sunt
  nete de costurile reale de finanțare;
- providerul este XTB în mod online, iar modelul configurat este `claude-haiku-4-5`, dar bucla
  online refuză în mod corect Claude deoarece nu are încă plafon de cost și lifecycle sigur.

Aceste fapte înseamnă că infrastructura poate fi demonstrată, dar nu există încă suficiente date
curate pentru a spune dacă strategia funcționează economic.

## 4. Ce lipsește pentru un MVP shadow utilizabil zilnic

Aceste puncte sunt prioritare chiar dacă nu se va trimite nicio ordine.

### P0 — operare repetabilă

1. **Pornire și supraveghere durabilă.** **Livrat parțial:** `Dockerfile` + `docker-compose.yaml`
   + `Makefile` + [RUNBOOK.md](RUNBOOK.md). `make up` pornește nucleul (PostgreSQL → `migrate`
   one-shot → dashboard) cu `restart: unless-stopped`, healthchecks (pg_isready + `/api/state`)
   și readiness prin `depends_on` (condition). Bucla de date (collector + `shadow.online`) e sub
   profilul `data` (`make up-data`), fiindcă are nevoie de o sursă externă (`trading_hands`/Polygon/CSV).
   Config-ul e validat cu `docker compose config`; **rularea live `up` nu a fost făcută în sandbox**
   (daemon Docker indisponibil) — de rulat pe mașina operatorului. **Rămâne:** rotația logurilor
   (acum default json-file al Docker) și supervizarea peste restart-ul componentelor.
2. **Lifecycle-ul autentificării XTB.** Alertă înainte/după expirarea TGT, procedură clară de
   re-login și verificare că serviciile brain își revin după reautentificare. Loginul din browser
   rămâne o intervenție de operator.
3. **Igiena bazei.** Mecanismul de izolare a bazei de test **este implementat**
   (`bootstrap_test.py`, guard-ul care refuză o bază al cărei nume nu se termină în `_test`,
   `BRAIN_TEST_DB_DSN`); rămâne de **configurat în CI și folosit consecvent**, iar cele **51 de
   snapshoturi de test vechi** trebuie curățate din baza operațională. Separat: clasificarea/închiderea
   controlată a celor 12 trade-uri legacy, marcarea datelor istorice neverificate și o politică de
   retenție. Metricile oficiale trebuie să consume exclusiv run-uri cu manifest verificat.
4. **CI pentru `trading_brain`.** Adăugat `.github/workflows/ci.yaml` cu două job-uri: `no-db`
   (suită fără infrastructură) și `db` (PostgreSQL izolat + `bootstrap` → `migrate` de la zero →
   verificare de idempotență → `bootstrap_test` → suita completă). **Pending prima rulare pe GitHub**
   pentru confirmare verde. Rămâne de adăugat un **test de upgrade** propriu-zis (migrare pe o bază
   pre-existentă, nu doar de la zero).
5. **Backup și restore.** **Livrat:** `scripts/db_backup.sh` (pg_dump custom-format + retenție) +
   `scripts/db_restore.sh` (cu guard `_test`/`--force`), unitate systemd + timer în `deploy/systemd/`
   (și exemplu cron), plus `make backup`/`restore` prin container. **Restore testat** în CI
   (`backup-restore`): dump → restore într-o bază nouă → compară numărul de migrări. Logica
   scripturilor e unit-testată (`tests/test_backup.py`, cu pg_dump/pg_restore mock). Rămâne ca
   operatorul să seteze DSN-ul admin + programul pe mașina reală.

**DoD:** stackul pornește repetabil, rulează cel puțin 72 h fără intervenție în afară de
reauth-ul documentat, se autorecuperează după restartul componentelor, alertele dispar/reapar corect,
iar testele nu mai scriu în baza operațională.

### P0 — realismul măsurării

1. **Costuri reale GOLD.** **Structura e livrată** ([shadow/virtual_broker.py](../shadow/virtual_broker.py)
   + [shadow/reconciler.py](../shadow/reconciler.py)): swap **long/short** separat, **ziua de
   triple-swap** (×3), **weekend-urile sărite** (fără swap Sâmbătă/Duminică), rollover **DST-aware**
   (oră locală într-un IANA tz), **valuta** și **versiunea termenilor** persistate, totul **înghețat
   la deschiderea trade-ului** (`cost_manifest` → `shadow_config_from_costs`, parte din execution
   hash; teste dedicate). **Rămâne / de validat:** (a) *citirea specificației reale a contului* și
   setarea ratelor (default 0/`unset` → manifestul spune onest că R nu e net de finanțare); (b)
   **normalizarea unității/semnului** — modelul presupune **% din notional/preț**, dar XTB poate cota
   swap-ul în **puncte/valută de cont** (de convertit înainte de a te încrede în expectancy);
   `swap_currency` e doar informativ, nu există import automat din specificație.
2. **Reconciliere M1 sau ticks.** **Suportat end-to-end:** `reconcile_timeframe` în `ShadowConfig`
   (default `15min`, setabil `1min`) — reconciliatorul ordonează atingerile SL/TP la M1 și **rezolvă
   banda de ambiguitate**; timeout-ul e o **durată** (invariant la granularitate, nu 96 minute pe M1);
   M1 se folosește doar dacă **acoperă continuu** trade-ul de la intrare (altfel un SL din gol ar fi
   ratat → fallback la M15, marcat per trade). **Limitare reală:** **XTB nu servește M1 azi**
   ([xtb.py](../data_collector/providers/xtb.py) + trading_hands acceptă doar M15/H1/H4/D1), deci pe
   calea online XTB **cade mereu pe M15** — funcțional doar cu un provider care servește M1 (ex.
   Polygon) sau după ce se adaugă M1 în trading_hands. Backtest-ul rulează pe M15; *ticks* pentru
   latență/slippage rămân viitor.
3. **Track record shadow verificat.** Rulează continuu pe date XTB, cu manifest curat, în mai multe
   regimuri de piață. Raportează număr de trade-uri, expectancy și drawdown cu intervale de
   încredere; nu promova pe baza unui singur punct estimat.
4. **Decizie privind maker-ul.** Online există doar strategia deterministă. Pentru produsul „AI”,
   adaugă Claude online cu plafon zilnic/lunar, confirmare/config de buget, audit per încercare și
   închiderea clientului; alternativ implementează un backend local și îl compară pe aceleași inputuri.
5. **Știrile live.** Fie se alege un provider point-in-time cu identitate/revizii/ingestion timestamp,
   fie se declară explicit că v1 nu folosește știri. Skeletonul HTTP nu este o integrare live.

**DoD:** costurile nu mai apar `not_modeled`, reconcilierea folosește M1/ticks, există un run
verificat suficient de lung și raportul arată separat rezultatul determinist, AI, fără feedback și
flat/random. Calendarul necesar este de ordinul săptămânilor, nu doar timpul de programare.

## 5. Ce lipsește pentru automatizare sigură pe cont demo

### P0 — rezultat autoritativ al brokerului (Faza 4)

Spike-ul ipax documentează RPC-urile, dar nu există integrarea de producție. Sunt necesare:

- client gRPC-Web pentru `GetClosedPositions`, cu auth refresh, timeout, retry și redacția secretelor;
- schemă protobuf validată, pagination/backfill la startup și după downtime;
- UPSERT idempotent al rezultatului final (`close_price`, `profit`, `close_time`, comision/swap);
- confirmarea reală `positionId <-> external_id`, înainte ca acel câmp să devină unic;
- endpoint read-only `GET /trades/closed` în `trading_hands`;
- test E2E cu o poziție demo deschisă și închisă, inclusiv recuperare după downtime.

Fără această piesă, sistemul nu poate demonstra ce s-a executat și cu ce rezultat final.

### P0 — router și control de execuție (Faza 6)

`app.decide` declară explicit „NO order execution”, iar Risk Engine setează mereu
`execution_ready=False`. Trebuie construite:

- gate stateful pentru poziție deja deschisă, cooldown/frecvență și idempotența ordinului;
- state machine `proposed -> reserved -> submitted -> accepted/rejected -> reconciled`;
- router demo care mapează o decizie aprobată la `/purchase`, cu cheie idempotentă și corelare;
- verificare înainte de submit: cont demo, simbol, volum, sesiune, spread, quote proaspăt și
  `TRADING_ENABLED` explicit;
- kill-switch manual și automat: pierdere conexiune, stale feed, spread anormal, drawdown zilnic,
  ordine/reconciliere inconsistente;
- prevenirea dublului submit la crash/retry și recovery după răspuns pierdut;
- alertare externă pentru ordine, rejecturi și reconciliere restantă;
- test E2E demo cu volum minim și scenarii de crash/restart.

**DoD:** un ordin demo poate fi trimis o singură dată, apare în pozițiile XTB, se închide, rezultatul
ipax este corelat și sistemul reconstruiește aceeași stare după restart. Orice incertitudine blochează
o ordine nouă.

## 6. Ce lipsește înainte de bani reali

Aceasta nu este doar o fază de implementare. Sunt necesare toate DoD-urile anterioare, plus:

- dovadă out-of-sample că modelul ales bate baseline-ul fără LLM **net de costuri reale**;
- număr suficient de evenimente în trend/range și perioade volatile, cu intervale de încredere;
- limită de risc pe trade/zi/cont, dimensionare verificată și reconciliere de equity;
- paritate shadow-demo măsurată pentru fill, slippage și motive de exit;
- soak test lung, exerciții de incident și restore, alertare livrată în afara dashboardului local;
- secret management, rotație controlată, audit de dependențe și revizuirea condițiilor brokerului;
- aprobare umană explicită pentru schimbarea din demo în real.

Nu recomand definirea unei date pentru bani reali înainte ca track record-ul shadow și demo să
existe. Codul verde demonstrează consistență software, nu profitabilitate.

## 7. Ordinea recomandată de execuție

1. Curăță/separă baza de test și rezolvă trade-urile legacy rămase open.
2. Adaugă CI cu PostgreSQL (**făcut** — `.github/workflows/ci.yaml`, pending prima rulare) și
   backup/restore testat (**făcut** — scripturi + systemd + job CI `backup-restore`).
3. Livrează un stack operabil printr-o singură comandă, cu supervisor și runbook de reauth XTB.
   (**livrat parțial** — `make up` pornește DB + migrate + dashboard cu healthchecks/restart +
   [RUNBOOK.md](RUNBOOK.md); `trading_hands` și login-ul din browser rămân externe/manuale,
   collector/online n-au healthcheck Docker, iar **rularea live Compose nu a fost efectuată** —
   config validat doar cu `docker compose config`).
4. Rulează Shadow MVP continuu; folosește dashboardul pentru heartbeat și audit.
5. Adaugă M1/ticks și termenii reali GOLD; repetă măsurarea.
6. Decide providerul de știri sau scoate știrile din DoD-ul v1.
7. Decide maker-ul online și bugetul; produce comparația AI vs baseline, fără feedback vs feedback.
8. Numai dacă rezultatele justifică: implementează clientul ipax și `/trades/closed`.
9. Implementează state machine-ul de execuție, gate-urile stateful și kill-switch-urile.
10. Rulează demo E2E și soak; reevaluează separat dacă merită trecerea la real.

Estimare orientativă pentru un dezvoltator familiar cu proiectul: **5–10 zile de lucru** până la un
Shadow MVP operabil, apoi **2–6 săptămâni de observație**; încă **10–20 zile de lucru** pentru ipax,
routerul demo, kill-switch-uri și testele de recovery. Necunoscutele de auth/protobuf ipax pot mări
estimarea. Trecerea la bani reali nu se poate estima onest înaintea măsurătorilor.

## 8. Definiția verdictului actual

- **Utilizabil acum:** dezvoltare, demonstrație, colectare/replay, dashboard și experimente shadow
  pornite manual.
- **Nu este terminat pentru:** rulare shadow nesupravegheată pe termen lung, validarea unui edge,
  ordine autonome demo sau bani reali.
- **Cea mai bună următoare țintă:** un **Shadow MVP operabil și reproductibil**, nu Faza 6. Abia
  datele curate produse de el pot decide dacă merită construirea execuției live.

## 9. Discrepanțe de documentație

**Rezolvate** (în `README` + acest raport):

- ✅ `README` nu mai numește raportul temporal „walk-forward”; spune corect **temporal-fold report**
  (maker-ul nu este antrenabil per fold).
- ✅ Introducerea din `README` clarifică: Claude este decidentul direcțional **prin design**, dar
  runtime bucla online rulează strategia deterministă, iar Claude e gated off în bucla nelimitată.
- ✅ `README` spune explicit că **nu se trimite niciun ordin** (fără router live, `execution_ready=False`;
  Risk Engine aprobă doar eligibilitatea shadow).
- ✅ `README` (secțiunea dashboard) indică explicit că **alertele** pot include run-uri legacy, în timp
  ce **metricile oficiale** filtrează doar manifestele verificate.

**Rămase deschise:**

- `EXECUTION_PLAN.md` conține o cronologie lungă de corecții și cifre istorice de teste; statusul
  curent ar trebui separat de jurnal, ca operatorul să nu confunde afirmații vechi cu starea actuală.

