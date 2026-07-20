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
