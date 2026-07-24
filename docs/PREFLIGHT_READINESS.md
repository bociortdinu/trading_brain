# Analiză de pregătire (pre-flight) înainte de rularea plătită cu Claude

**Data:** 2026-07-19 · **Commit:** `38b0046` · **Migrări:** 0001–0027 (27)
**Scop document:** o fotografie onestă a stării, ca să (a) știm dacă scopul aplicației e îndeplinit
și ce mai rămâne, și (b) NU pornim testele plătite cu Claude înainte să fim siguri că restul
lanțului funcționează — ca să nu pierdem bani și să descoperim ulterior că problema era în altă
parte. Reevaluează acest fișier la fiecare rundă și compară.

---

## ACTUALIZARE 2026-07-20 — corectare după auditul independent Codex

Un audit independent ([PRE_PAID_AI_READINESS_AUDIT_CODEX.md](PRE_PAID_AI_READINESS_AUDIT_CODEX.md))
a găsit că **acest raport a fost prea optimist**. Am **verificat fiecare constatare în cod** — toate
se confirmă. Retrag explicit următoarele afirmații de mai jos:

- **„Calea de bani e sănătoasă" — RETRAS.** Doar `shadow.runner --maker claude --persist` e rezonabil
  protejat, iar și acolo: plafonul numără **decizii logice, nu request-uri HTTP** (`max_retries=3` →
  până la 4 request-uri/decizie), `--persist` NU e obligatoriu, default cap 50 (nu 0), `--yes`
  ocolește confirmarea, iar estimarea „worst-case" nu e validă (folosește `max_tokens` și pentru
  input, nu multiplică cu retry-urile, nu include cache-write).
- **DOUĂ CLI-uri fac apeluri plătite aproape neprotejate:** `python -m app.decide` (default = maker
  REAL; doar `--fake` îl oprește; fără plafon USD/confirmare/rezervare) și `python -m app.llm_smoke`
  (apel imediat la prezența cheii). Cu cheia Anthropic configurată = risc real de cheltuială
  accidentală.
- **„Fiecare apel e în `llm_calls` în aceeași tranzacție" — RETRAS literal.** Doar rezultatul logic
  reușit e atomic cu decizia; încercările HTTP intermediare și crash-urile pre-commit nu sunt toate
  auditate (timeout după accept ⇒ cost fără dovadă locală).
- **„Resume nu replătește" — CALIFICAT.** Adevărat după persistare, DAR în fereastra
  provider-accept ↔ DB-necomis un crash + expirare lease poate re-apela (at-most-one-concurrent, nu
  exactly-once).
- **Blocante de corectitudine confirmate**, două fiind **regresii/incompletitudini în propria mea
  muncă R3**: **P0-C1** (lookup-urile scheduler nu folosesc `provider_symbol`/`pipeline_version`;
  fără `dataset_id`); **P0-C2** (R3-2: trade-ul cross-provider e doar *sărit* → rămâne open →
  position gate-ul pe simbol blochează la nesfârșit — `open_shadow_trades(symbol=…)` nu filtrează
  provider); **P0-C3** (R3-5: downtime măsurat față de ultima **decizie**, nu ultima **bară
  procesată** → barele sărite intenționat de position gate sunt raportate fals ca downtime).

**Verdict aliniat: NO-GO** pentru orice test AI contra cost până la remedierea gateway-ului financiar
central + P0-C1..C3 + blocantele de produs. Vezi criteriile de GO în raportul Codex §9–§11.
Secțiunile 3–6 de mai jos rămân ca istoric, dar trebuie citite prin filtrul acestei corectări.

### Stadiul remedierii (2026-07-20, post-audit)

**REZOLVAT în cod, cu teste (fără push/servicii/Claude):**
- Cost-safety pe toate CLI-urile: `BRAIN_PAID_AI_ENABLED=false` implicit; `app.decide` gratuit
  implicit; `app.llm_smoke` + `runner --maker claude` cu poartă (`555c713`).
- **P0-C2** deadlock: trade cross-provider → **quarantine**, nu blochează gate-ul (`7abc37c`).
- **P0-C3** downtime fals: ledger `processed_bars`, downtime față de ultima bară **procesată**
  (`8eaff72`).
- **P0-C1** identitate în lookup-uri: `provider_symbol` + `pipeline_version` (`cb41d91`).
- **Gateway financiar central** (Codex §8): bugete USD run/zi/lună (default 0), cap pe încercări
  HTTP (audit per-încercare), allowlist de model, ledger `paid_attempts` cu rezervare atomică +
  audit pre-attempt, cablat pe toate cele 3 entry point-uri; + unelte de reconciliere/orfani
  (`app.paid_report`) (`318226b`, `058c819`, `d888858`).
  Vezi [FINANCIAL_GATEWAY_DESIGN.md](FINANCIAL_GATEWAY_DESIGN.md).
- **Freeze de dataset reproductibil**: tabel `datasets` (hash de conținut + provenance) +
  `dataset_id` pe snapshot (doar la replay); `app.freeze_dataset`; runul persistat își pinuiește
  snapshoturile pe dataset (`65e9471`, `7d365b5`).
- Teste: fără-DB **298 passed**; DB **362 passed**.

**ÎNCĂ blocant pentru GO (majoritar mediu/operator, NU cod):** date istorice XAUUSD **reale**
(mecanismul de freeze există acum — lipsesc datele reale de înghețat; pipeline-ul e blocat pe CoreAPI
chart command în `trading_hands`); migrări de la zero + toate testele DB + CI remote verde; Compose
live + soak XTB; rate reale GOLD; apoi canary de UN request cu buget mic setat explicit. Deci calea de
bani + reproductibilitatea sunt acum **la nivel de cod gata**, dar rămâne **NO-GO** până se închid
blocantele de produs de mai sus.

---

## ACTUALIZARE 2026-07-21 — arhiva de bare pe disc (pasul 0 devine executabil)

**Constatare nouă (verificată în cod, nu presupusă):** „reproducibilitatea" era **unidirecțională**.
`freeze_dataset` calculează hash-ul barelor, dar **nu le stochează** ([database/repository.py:115](../database/repository.py#L115),
[0031_datasets.sql](../database/migrations/0031_datasets.sql)), iar `shadow.runner` cere barele de la
`build_provider(settings)` la **fiecare** rulare ([shadow/runner.py:526-531](../shadow/runner.py#L526-L531)).
Cum XTB CoreAPI servește o **fereastră rulantă** pe o sesiune autentificată, un `dataset_id` dovedea
*că* datele s-au schimbat, nu *care* erau — după ce fereastra se rotea, octeții exacți deveneau
irecuperabili. În plus, orice backtest cerea `trading_hands` viu (deci nimic în weekend).

**REZOLVAT:** `python -m app.harvest` ([app/harvest.py](../app/harvest.py)) scrie barele pe disc în
exact formatul citit de `CsvMarketDataProvider` și **face merge** cu arhiva existentă, deci harvest-uri
repetate **acumulează** istoric dincolo de fereastra furnizorului. După un harvest, tot lanțul de
backtest rulează **offline, gratuit, reproductibil**. Conflictele sunt **fail-closed** (o bară care
diferă de cea arhivată oprește rularea și e numită; `--on-conflict keep|replace` e scăpărea
deliberată), scrierea e atomică (tmp + `os.replace`).

**Capcană descoperită empiric, acum raportată explicit:** un backtest pe o arhivă cu mii de bare M15
dar cu **daily scurt** returnează tăcut `bars_evaluated=0` — identic cu un pipeline stricat. Cauza:
`MIN_BARS=200` se cere în **fiecare** timeframe la fiecare `as_of`, iar `runner` cere `count` bare
uniform pentru toate tf-urile. `app.harvest` calculează acum barele efectiv evaluabile, numește
timeframe-ul care leagă (de regulă `1day`) și **iese cu cod 1** dacă arhiva nu e gata.

**Verificat cap-coadă local (date sintetice, fără XTB):** harvest → arhivă → `shadow.runner` offline
= `bars_evaluated=2301`, 186 trade-uri. (Win-rate-ul mare e artefact de trend sintetic — **nu** e
dovadă de edge; dovada cere date XAUUSD reale.)

**Stare teste:** fără-DB **324 passed** / 69 skipped; cu DB **393 passed**, 0 eșecuri.
**Migrări:** 0001–0031 aplică **curat de la zero** (verificat pe o bază scratch, apoi ștearsă).

**⚠ Constatare de mediu:** baza de date de PRODUCȚIE e la migrarea **0023**, codul livrează **0031** —
**8 migrări neaplicate**, printre care `paid_attempts` (registrul gateway-ului financiar) și
`datasets`. Deci controalele de buget pe calea plătită **nu au tabelele lor** în baza reală. De
aplicat `python -m database.migrate` înainte de orice test plătit (0025 face backfill onest cu
santinele `unknown`/`legacy`, deci cele 108 snapshoturi existente supraviețuiesc).

### Execuție 2026-07-21 (aceeași sesiune) — pașii 0 și 1 sunt ACUM ÎNDEPLINIȚI

- **Baza de producție migrată 0023 → 0031.** Backup logic JSON luat înainte (576 rânduri, 13 tabele,
  în `backups/prod_backup_pre_0031`, 0600, fiindcă `pg_dump` nu e instalat). După migrare: toate
  cele 576 de rânduri intacte, 0 identități NULL, 31 migrări. Santinelele `unknown`/`legacy` nu au
  fost necesare — toate cele 108 snapshoturi aveau deja provider real (xtb 55, csv 51, polygon 2).
- **`trading_hands` pornit și conectat** (cont demo `21842412`, `trading_enabled=false`, loopback).
  Testele Go: pass (necachate), `go vet` curat, build curat. Testele launcher-ului Node: 8/8 pass.
  Toate erau marcate „NErulate" în raportul anterior.
- **PASUL 0 ÎNDEPLINIT — date XAUUSD REALE.** `app.harvest --count 10000` → **21.151 bare** în
  `data/bars/`: `1day` 10.000 bare **din 1985-08-27**, `4h` 3.166, `1h` 3.869, `15min` 4.116.
- **PASUL 1 ÎNDEPLINIT — lanțul gratuit cap-coadă pe date reale.** `shadow.runner --count 3917`
  offline (provider csv): `bars_evaluated=3718`, `trades_opened=4`. Rulare persistată
  `run-id=real-gold-det-2026-07-21`, `dataset_id=c17968e747079f12`; re-freeze pe aceeași arhivă dă
  **același** dataset_id → reproductibilitatea e acum **reală**, nu doar detectabilă.
- **Rezultatul nu spune nimic despre edge:** 4 trade-uri (win 25%, expectancy −0.333R, total
  −1.331R). Eșantion mult prea mic pentru orice concluzie, în ambele direcții.

### ⚠ CONSTATARE DE COST — estimarea anterioară era subevaluată de ~3×

`decision_maker.decide()` se apelează pentru **fiecare** bară care trece prefiltrul
([decision/pipeline.py:104-111](../decision/pipeline.py#L104-L111)), iar **position gate-ul se
aplică DUPĂ** ([shadow/runner.py:266-272](../shadow/runner.py#L266-L272)). Deci barele blocate de
poziția deschisă sunt **plătite și apoi aruncate**.

Măsurat, nu estimat: rularea de mai sus a persistat **2255 decizii** (= 3718 − 1463 filtrate). Cu
Claude, exact acestea ar fi fost apeluri plătite:

| Model | $/decizie | 2255 decizii | Note |
|---|---|---|---|
| `claude-haiku-4-5` | ~0.0032 | **~$7.2** | modelul din `.env` |
| `claude-sonnet-5` | ~0.0096 | **~$21.6** | default-ul din cod |

Plus retry-uri (până la 4 încercări HTTP/decizie în worst case). Nota din memorie „~800 apeluri,
$2–3" **nu se aplică** acestei ferestre. Din cele 2255, **44 au fost blocate de position gate** —
cost pur pierdut, evitabil dacă gate-ul s-ar verifica înaintea makerului.

**Recomandare înainte de orice rulare plătită:** (a) mută verificarea position gate ÎNAINTE de
`decide()` (economie directă), (b) pornește cu `--max-llm-calls 1` ca smoke, (c) setează bugetul
USD explicit în gateway (default 0 = blocat).

### ACTUALIZARE 2026-07-21 (b) — gate-ul mutat + re-estimare pe tokeni MĂSURAȚI

**Gate-ul e mutat** înaintea makerului ([shadow/runner.py](../shadow/runner.py), commit ulterior).
Măsurat pe aceleași date: `decided` 2255 → **2202** (`position_gated=53`), iar **metricile sunt
identice** (4 trade-uri, win 25%, total −1.331R) — barele gated nu deschideau oricum trade.
Economia reală e **53 de apeluri, nu 44**: vechea numărătoare `blocked` cerea `rec.risk_approved`,
deci 9 bare erau plătite fără să fie măcar candidate.

Semantica s-a schimbat onest: nu mai putem ști dacă o bară gated *ar fi fost* aprobată, deci
`approved == trades_opened`, iar barele sărite se raportează separat ca `position_gated`. A pretinde
„aprobat dar blocat" ar fi inventat un răspuns pe care nu l-am cerut niciodată.

**Costuri pe tokeni măsurați** (`messages.count_tokens`, endpoint **gratuit** — 0 tokeni facturați),
pe un DecisionInput real de GOLD: **612 tokeni input/decizie** pe `claude-haiku-4-5`, **818** pe
`claude-sonnet-5` (tokenizer nou). Output: tipic ~200 tokeni, plafon dur `max_tokens=1024`.

| Model | Backtest (2202 decizii), tipic | Worst-case | Live (~58 apeluri/zi) |
|---|---|---|---|
| `claude-haiku-4-5` ($1/$5) | **$3.55** | $12.62 | ~$2.80/lună |
| `claude-sonnet-5` intro ($2/$10, până 2026-08-31) | **$8.01** | $26.15 | ~$6.33/lună |
| `claude-sonnet-5` standard ($3/$15) | **$12.01** | $39.23 | ~$9.49/lună |

**Mutarea gate-ului economisește doar $0.09–$0.29 per backtest — corectă, dar NU e levierul.**
Nu o supravinde. Levierul real e **prefiltrul**: trec **60.7%** din barele evaluate (2255/3718).
La M15 asta înseamnă **~58 apeluri plătite/zi** în bucla live, nu „10–20/zi" cum presupunea nota
anterioară. Dacă vrei costul jos, strânge prefiltrul — nu mai umbla la gate.

**Prompt caching-ul e cod mort, nu doar „marginal".** `cache_control: ephemeral` pe `SYSTEM_RULES`
([decision/llm_client.py:135](../decision/llm_client.py#L135)) nu poate crea NICIODATĂ o intrare de
cache: prefixul minim cacheabil e 4096 tokeni pe Haiku 4.5, iar **promptul întreg** are 612. Și pe
Sonnet 5 (818 tokeni) e sub orice prag minim documentat. Nu costă nimic (sub prag pur și simplu nu
se cachează), dar nu economisește nimic — nu-l trece la „controale de cost".

Rămâne **NO-GO** pentru rularea plătită, dar din motive mult mai puține: rate reale GOLD
swap/comision (`unset` → R nu e net de finanțare), Compose live + soak, CI remote verde, și
decizia de buget de mai sus.

---

## 1. Scopul aplicației

Un sistem algoritmic de **shadow-trading pe aur (XAUUSD)** pe XTB, în care:
- matematica (date + indicatori multi-timeframe) e în Python, determinist;
- **Claude** (LLM comercial) este, *prin design*, singurul care ia decizia direcțională
  (BUY/SELL/NO_TRADE);
- un **Risk Engine** rigid, fail-closed, filtrează fiecare decizie și calculează SL/TP determinist;
- totul rulează în **shadow mode** (fără ordine reale) până se **dovedește statistic un avantaj
  (edge)** al strategiei.

Ținta lui v1 NU este execuția live, ci: *infrastructură reproductibilă + track record shadow
auditabil care arată dacă strategia (cu Claude ca decident) are edge — înainte de orice bani reali.*

## 2. Este scopul îndeplinit?

**Parțial — infrastructura DA, dovada edge-ului NU.**

| Componentă a scopului | Stare |
|---|---|
| Colectare → features MTF → prefiltru → decizie → risc → trade shadow → reconciliere → metrici → audit | **Construit și testat** (336 teste) |
| Reproductibilitate / audit (manifest de execuție, run_id din config, append-only, identitate sursă) | **Construit și testat** (rundele R2/R3) |
| **Edge-ul real măsurat cu Claude** | **NU** — nicio rulare plătită încă |
| Track record shadow LIVE cu Claude (bucla online) | **NU** — online rulează doar makerul determinist; Claude e ținut oprit până are plafon de cost + lifecycle sigur |
| Execuție live (ordine reale) | **Neînceput, intenționat** (`execution_ready` mereu `False`) |

Concluzie: se poate **demonstra** cap-coadă, dar încă **nu există dovada că strategia funcționează
economic**. Pasul care produce acea dovadă este exact **rularea plătită** pe care o de-riscăm aici.

## 3. Ce funcționează și e verificat (această sesiune, local)

**Teste, pe categorii (cerute separat):**
- **fără-DB:** 291 passed, 45 skipped
- **DB (`_test` izolat):** 336 passed — cere `BRAIN_TEST_DB_DSN` + `BRAIN_TEST_ADMIN_DB_DSN`
- **Compose (validare config client-side):** 4 passed; **`compose up` live NErulat** (fără daemon Docker)
- **Go (`trading_hands`) / launcher Node:** **NErulate** în această sesiune (neatinse)

**Controale de COST pe calea plătită (toate prezente + testate):**
1. **Prefiltru gratuit înainte de LLM** ([decision/prefilter.py](../decision/prefilter.py)): sare
   barele cu date insuficiente, spread > `0.10`, sau regim netranzacționat (`choppy`) — o bară
   respinsă nu costă nimic.
2. **Plafon dur `--max-llm-calls`** verificat **ÎNAINTE** de orice rezervare/claim, fail-closed
   ([shadow/runner.py](../shadow/runner.py) `stage="llm_cap_reached"`) — bucla se oprește înainte
   să depășească bugetul.
3. **Confirmare cu estimare worst-case** (`_confirm_paid_run`): tipărește costul maxim și cere `y`
   explicit (sau `--yes`); nu cheltuie niciodată tăcut.
4. **Resume fără re-plată**: după crash, lanțul se reconstruiește din decizia PERSISTATĂ, fără a
   re-apela makerul (`_rebuild_*`, `load_decided_outcome`).
5. **Decizie + audit plătit într-o SINGURĂ tranzacție** (`llm_calls`) — un eșec de audit nu poate
   lăsa o decizie comisă cu un apel plătit pierdut.
6. **Prompt caching** pe `SYSTEM_RULES` (cache ephemeral) → `cache_read` la repetări = mai ieftin.
7. **Model configurat ieftin**: `.env` `BRAIN_DECISION_MODEL=claude-haiku-4-5` ($1/$5 la 1M).

**Corectitudinea căii de bani, verificată OFFLINE (fără apel plătit):**
- SDK `anthropic==0.116.0`: `messages.parse(output_format=DecisionOutput)` există și acceptă
  `output_format`; rezultatul expune `parsed_output`. Deci semnătura din
  [decision/llm_client.py](../decision/llm_client.py) e validă pe SDK-ul pinuit.
- Fail-closed pe fiecare mod de eșec: `refusal`, `max_tokens` (trunchiat), `no_parsed_output`,
  eroare de validare, retry tranzitoriu epuizat — niciodată o decizie fabricată.

## 4. Ce lipsește pentru scop (onest)

1. **Sursă reală de date istorice XAUUSD** pentru backtest. În sandbox NU există fișiere CSV
   `{SYMBOL}_{tf}.csv` și nici `trading_hands` live; backtestul (`shadow.runner`) fetch-uiește prin
   `build_provider(settings)`. **Fără date reale și suficiente, o rulare plătită = bani pe date
   proaste.** (Testele folosesc bare sintetice — dovedesc lanțul, nu edge-ul.)
2. **Rularea plătită cu Claude** propriu-zisă (pasul care măsoară edge-ul).
3. **Bucla online cu Claude** — azi rulează doar determinist; Claude rămâne gated până are plafon
   de cost zilnic/lunar + lifecycle sigur în daemon.
4. **Rate reale GOLD swap/comision** — modelul e făcut, dar ratele sunt `unset` (R nu e net de
   finanțare până le setezi din specificația contului).
5. **Follow-up-uri R3 rămase**: hash-uri (`--hash`) în `requirements.lock` + baza Docker
   digest-pinned (R3-8); un **job** care chiar rulează pe rolul `trading_brain_retention`; fixtures
   live de calendar iarnă/DST; știri live sau scoaterea din DoD v1.
6. **Live neверificat de mediu**: `compose up`, XTB live, CI remote verde — cer mașina operatorului.

## 5. PRE-FLIGHT: pași ÎNAINTE de prima rulare plătită (ca să NU pierzi bani)

Ordinea contează — fiecare pas de-riscă următorul. Calea plătită e **structural identică** cu cea
determinist-gratuită; se schimbă doar makerul. Deci întâi dovedești lanțul gratis, apoi plătești
minim, apoi scalezi.

- [ ] **0. Date reale.** Asigură bare istorice XAUUSD suficiente (CSV `{SYMBOL}_{tf}.csv` pentru
  toate timeframe-urile, sau un provider funcțional). Verifică `--count` ≤ barele disponibile.
- [ ] **1. Rulează întâi calea GRATUITĂ, cap-coadă, pe ACELEAȘI date + config:**
  `python -m shadow.runner --count N --persist --run-id smoke-det` → confirmă că tot lanțul merge și
  produce metrici la **cost zero**. Dacă pică aici, ai găsit problema fără să plătești.
- [ ] **2. Confirmă modelul intenționat**: tipărește settings și verifică
  `BRAIN_DECISION_MODEL=claude-haiku-4-5` (default-ul din cod e `claude-sonnet-5` = 3× mai scump;
  dacă `.env` nu se încarcă, cazi pe sonnet).
- [ ] **3. Smoke plătit de UN apel**:
  `python -m shadow.runner --maker claude --max-llm-calls 1 --count <mic> --persist --run-id smoke-claude --yes`
  → UN apel real (~fracțiune de cent). Verifică: rândul din `llm_calls` (request_id, stop_reason,
  tokeni, `estimated_cost_usd`), că decizia a aterizat, că estimarea de cost e rezonabilă.
- [ ] **4. Abia apoi scalează** `--max-llm-calls` la rularea completă, tot cu plafon, tot
  `--persist --run-id <nou>` (resumabil + auditabil).
- [ ] **5. Urmărește rata de trecere a prefiltrului** — dacă prea multe bare trec, costul crește
  liniar; ajustează `max_spread_pct` / regimuri dacă e cazul.
- [ ] **6. run_id proaspăt** pentru fiecare config: o schimbare de config forțează un run nou
  (execution manifest), deci nu amesteci experimente și `assert_run_manifest` te oprește la mismatch.

## 6. Riscuri concrete de bani și cum le eliminăm

| Risc | De ce | Mitigare (deja în cod / pas pre-flight) |
|---|---|---|
| Plătești, apoi lanțul pică „în altă parte" | bug în persist/audit/reconcile | Pasul 1 (rulare determinist-gratuită identică structural) + 336 teste |
| Apel reușit dar răspuns inutilizabil | drift SDK / schema | Verificat offline: `parse(output_format)`→`parsed_output`; fail-closed pe `no_parsed_output`/validare |
| Depășire de buget | buclă scăpată | plafon dur verificat înainte de claim; confirmare worst-case |
| Dublă plată la resume | re-apel după crash | resume reconstruiește din decizia persistată, fără re-apel |
| Model greșit (scump) | default `sonnet-5` vs `.env` haiku | Pasul 2 (confirmă modelul încărcat) |
| Date proaste/insuficiente | fără sursă reală în mediu | Pasul 0 (date reale) — **blocant acum** |

## 7. Snapshot pentru comparare ulterioară

- Commit: `38b0046`; migrări 0001–0027; versiuni: features `1.2.0`, decision-schema `2026.3`,
  prompt `2026.1`, strategy `2026.1`, risk `2026.2`.
- Teste: fără-DB **291/45**; DB **336**; Compose **4** (config); Go/Node **nerulate**.
- Model configurat: `claude-haiku-4-5`; benchmark: `claude-opus-4-8`; `max_tokens=1024`.
- Provider configurat: `xtb` (online, cere `trading_hands`); pentru backtest folosește CSV/provider.
- Blocant pentru rularea plătită: **sursă reală de date istorice XAUUSD** (pasul 0).
