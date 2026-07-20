# Propunere de design: gateway financiar central pentru apeluri AI plătite

**Status:** PROPUNERE (de aprobat înainte de implementare) · **Data:** 2026-07-20
**Context:** auditul Codex (§8) cere ca TOATE căile care pot cheltui la Anthropic să treacă printr-un
singur gateway cu invariants stricte. Seed-ul e deja livrat (commit `555c713`): `BRAIN_PAID_AI_ENABLED`
(default OFF), `require_paid_ai_enabled()` + `confirm_paid_call()`, `app.decide` free-by-default,
`app.llm_smoke` + `shadow.runner --maker claude` cu poartă, și teste care dovedesc refuzul când
poarta e OFF. Acest document propune restul: **bugete USD, cap pe încercări HTTP, audit pre-attempt,
stări timeout/unknown, allowlist de model, reconciliere cu consola**.

## 1. Invariants țintă (din Codex §8)

1. default OFF; cheia singură nu cheltuie — **LIVRAT**.
2. allowlist de model + model efectiv afișat înainte de request.
3. maker plătit permis DOAR cu persistare + `run_id` explicit.
4. buget per rulare / zi / lună, în USD, default **0** (nimic permis până nu setezi).
5. cap pe **încercări HTTP**, nu doar pe decizii logice; canary = retry 0.
6. **rezervare atomică de buget + rând `attempt_started` ÎNAINTE de request**.
7. finalizare cu request ID, usage, cost; **stări distincte pentru timeout / unknown outcome**.
8. reconciliere ulterioară cu usage/billing din consola Anthropic.
9. estimare care include input + output + cache write/read + retry budget.
10. kill switch central folosit de `app.decide`, `app.llm_smoke`, `shadow.runner` și orice daemon.
11. teste care dovedesc refuzul pe fiecare cale când poarta e OFF — **LIVRAT pentru poarta master**.

## 2. Componente propuse

### 2.1 Setări noi (`config/settings.py`)

| Setare | Default | Rol |
|---|---:|---|
| `paid_ai_enabled` | `false` | kill switch master (**există deja**) |
| `paid_ai_model_allowlist` | `["claude-haiku-4-5"]` | modelul efectiv trebuie să fie în listă; altfel refuz |
| `paid_max_http_attempts` | `1` | cap DUR pe request-uri HTTP per decizie logică (1 = retry 0, pentru canary) |
| `paid_budget_run_usd` | `0.0` | plafon USD pe o rulare (0 = nimic permis) |
| `paid_budget_day_usd` | `0.0` | plafon USD pe zi (UTC) |
| `paid_budget_month_usd` | `0.0` | plafon USD pe lună (UTC) |

Regula: un apel e permis doar dacă `enabled` ȘI modelul e în allowlist ȘI toate bugetele au marjă
pentru estimarea worst-case a acelui apel.

### 2.2 Tabel nou `paid_attempts` (ledger PER ÎNCERCARE HTTP)

Rezolvă „timeout după accept ⇒ cost fără dovadă". Un rând scris **înainte** de request, actualizat
după:

```
paid_attempts(
  id, run_id, context, model, input_hash,
  attempt_no,                      -- a câta încercare HTTP pentru aceeași decizie logică
  status,                          -- 'started' | 'completed' | 'timeout' | 'error' | 'unknown'
  request_id, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
  est_cost_usd, actual_cost_usd,   -- est la rezervare; actual la finalizare
  reconciled_console boolean default false,
  started_at, finished_at,
  UNIQUE(run_id, input_hash, attempt_no)
)
```

- `status='started'` scris ÎNAINTE de HTTP (append-only fact; app INSERT).
- după răspuns: UPDATE la 'completed' + usage/cost/request_id; la timeout → 'timeout' (cost NECUNOSCUT,
  de reconciliat); la excepție → 'error'; dacă procesul moare între send și update → rândul rămâne
  'started' = semnal de investigat (nu „zero apeluri").
- `llm_calls` rămâne ledgerul rezultatului LOGIC; `paid_attempts` e granularitatea HTTP de sub el.

### 2.3 Contabilitate de buget

Două opțiuni (de ales — vezi §5):
- **(A) derivată din `paid_attempts`**: bugetul cheltuit = `SUM(actual_cost_usd)` pe fereastra
  run/zi/lună, calculat la fiecare apel. Simplu, o singură sursă de adevăr, dar cursa concurentă cere
  o rezervare (vezi mai jos).
- **(B) tabel `paid_budget_ledger` dedicat** cu rezervări + reconcilieri explicite. Mai mult cod, dar
  rezervare atomică curată.

Rezervare atomică (fail-closed): înainte de request, într-o tranzacție, `INSERT attempt 'started'` cu
`est_cost_usd` (worst-case) și verifică `SUM(est peste ferestre) <= plafon`; dacă nu, refuză fără să
apelezi. După răspuns, `actual_cost_usd` din usage-ul real.

### 2.4 Componenta `PaidAiGateway` (`decision/paid_gateway.py`)

Înfășoară `AnthropicDecisionMaker`. API-ul respectă Protocolul `decide()`, deci `app.decide`,
`shadow.runner` și `app.llm_smoke` îl folosesc identic:

```
gateway = PaidAiGateway(settings, run_id=..., persist_dsn=...)   # cere run_id + dsn
await gateway.decide(inp)   # per apel:
  1. require_paid_ai_enabled + model in allowlist + effective model afișat
  2. estimare worst-case (input real ~ len(prompt), output = max_tokens, × attempts, + cache)
  3. rezervare atomică buget + INSERT paid_attempts('started')   [ÎNAINTE de HTTP]
  4. call cu max_retries = paid_max_http_attempts-1 (canary: 0)
  5. UPDATE paid_attempts (completed/timeout/error + usage/cost/request_id)
  6. dacă buget depășit la actual -> oprește rularea (fail-closed), nu mai apelează
```

Kill switch: dacă `paid_ai_enabled` devine false între apeluri (sau un plafon e atins), următorul
`decide()` refuză.

### 2.5 Estimare corectă (înlocuiește „worst-case"-ul actual)

`estimate_cost(model, prompt_tokens, max_tokens, attempts)` = `attempts × (prompt_tokens×pin +
max_tokens×pout) + cache_write` — `prompt_tokens` din `count_tokens` real (nu `max_tokens`). Prețurile
rămân o tabelă, dar marcată cu data și verificată la pornire față de un prag.

## 3. Modificări pe entry point-uri

- `shadow.runner --maker claude`: impune `--persist` + `--run-id` (refuz altfel); folosește
  `PaidAiGateway` în loc de `_CountingMaker`; `--max-llm-calls` rămâne cap pe decizii logice, dar
  bugetul USD + `paid_max_http_attempts` sunt plafoanele reale.
- `app.decide --paid`: impune `--run-id` + persistare; folosește gateway-ul.
- `app.llm_smoke`: rulează prin gateway cu `run_id='smoke'`, `paid_max_http_attempts=1`, buget mic.

## 4. Plan de implementare (Codex Etapa B) + teste

1. Setări + migrare `paid_attempts` (+ retention grant).
2. `estimate_cost` real (cu `count_tokens`), înlocuiește estimarea din runner.
3. `PaidAiGateway` + rezervare atomică de buget.
4. Cablare pe cele 3 entry point-uri (persist + run_id obligatorii pe calea plătită).
5. Teste cu **fake transport** (fără apel real): succes, timeout-după-send, 429, 5xx, crash
   înainte/după commit, restart, buget epuizat, concurență — dovedind rezervarea atomică, stările
   timeout/unknown și că un `attempt('started')` orfan e vizibil.
6. Test: `paid=false` ⇒ zero request-uri din toate CLI-urile (extinde `test_paid_guard`).

## 5. Decizii deschise (am nevoie de confirmarea ta)

1. **Contabilitate buget: (A) derivată din `paid_attempts` [recomand — o sursă de adevăr] vs (B)
   ledger dedicat.**
2. **Reconciliere cu consola Anthropic: manuală [recomand pentru început — marchezi
   `reconciled_console=true` după ce compari] vs automată prin API-ul de usage (dacă îl vrei).**
3. **Plafoane implicite concrete** (ex. run=$0.50, zi=$2, lună=$10) sau le lași 0 și le setezi doar la
   canary?
4. **`paid_ai_model_allowlist`**: doar `claude-haiku-4-5` la început?

Nimic din acestea nu se implementează până nu confirmi — deocamdată e doar propunerea.
