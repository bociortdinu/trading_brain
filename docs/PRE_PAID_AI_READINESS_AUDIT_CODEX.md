# Audit Codex: pregătirea proiectului înainte de teste AI contra cost

## 1. Baseline și scopul auditului

- Data auditului: **2026-07-20**
- Commit analizat: **`736b870`** (`feature/audit-fixes`)
- Raport comparat: `docs/PREFLIGHT_READINESS.md`
- Acțiuni interzise pe durata auditului: pornire servicii, ordine, apeluri Anthropic și
  afișarea secretelor.
- Acest document este un audit independent. Nu înlocuiește raportul lui Claude, pentru a putea
  compara ulterior afirmațiile și remedierile.

Scopul aplicației, așa cum reiese din cod și documentație, este:

1. să colecteze date XAUUSD/GOLD fără look-ahead;
2. să construiască features multi-timeframe și să decidă eligibilitatea;
3. să folosească un model AI numai pentru direcția BUY/SELL/NO_TRADE;
4. să aplice separat reguli deterministe de risc;
5. să măsoare rezultatul în shadow/replay, cu proveniență și audit;
6. să demonstreze un edge net de costuri înainte de orice execuție reală.

## 2. Verdict executiv

**NO-GO pentru orice test AI contra cost.**

Infrastructura principală există și are o suită automată mare, însă raportul curent este prea
optimist când afirmă că singurul blocant este lipsa datelor istorice reale și că „path-ul de
bani este sănătos”. Lipsa datelor este un blocant, dar nu este singurul.

Înainte de primul apel plătit trebuie închise două categorii de probleme:

- **siguranța financiară:** toate căile care pot apela Anthropic trebuie să treacă printr-un
  singur gate, cu default OFF, plafon pe încercări HTTP și USD, audit pre-attempt și fără căi CLI
  alternative neprotejate;
- **corectitudinea intrării și a auditului:** identitatea sursei nu este folosită complet în
  toate lookup-urile, reconcilierea nu poate drena corect trade-uri deschise de alt provider, iar
  detectorul de downtime confundă în anumite cazuri politica de poziție cu downtime-ul real.

Un apel de canary de un singur request este acceptabil numai după criteriile din secțiunea 9.

## 3. Dovezi verificate independent

| Verificare | Rezultat | Interpretare |
|---|---:|---|
| `pytest` fără DB de test accesibil | **291 passed, 45 skipped** | partea fără infrastructură este verde; cele 45 teste DB nu au fost reconfirmate de Codex |
| Total rezultat din colecție | **336 teste** | compatibil numeric cu raportul Claude: 291 + 45; nu este o dovadă că cele 45 au rulat aici |
| `compileall` și `pip check` | **PASS** | importuri/dependențe instalate coerent în mediul local |
| `docker compose ... config -q` | **PASS** | configurația se parsează; nu dovedește că stack-ul pornește live |
| Go, `go test -race ./...` și `go vet ./...` în `trading_hands` | **PASS** | verificare independentă pe HEAD existent, fără modificări Go |
| Node launcher, `npm test` | **8/8 PASS** | launcher-ul are teste verzi pe HEAD existent |
| fișiere `.env` | **0600** | permisiuni locale corecte; valorile nu au fost afișate |
| cheie Anthropic configurată | **DA** | crește riscul unui apel accidental din CLI-urile neprotejate |
| DB izolată `_test` configurată pentru acest audit | **NU** | suita DB și migrările de la zero trebuie rerulate înainte de GO |
| replay determinist sintetic gratuit | **PASS** | dovedește orchestrarea, nu edge/profit și nu calitatea pe date reale |
| XTB live / Compose live / CI remote pe HEAD | **NERULAT** | rămân porți operaționale |

Rezultatul replay-ului sintetic nu poate fi folosit ca dovadă de strategie: datele erau un trend
construit, deci rezultatele pozitive arată numai că lanțul poate produce și reconcilia trade-uri.

## 4. Auditul căilor care pot cheltui bani

În cod există **trei entry point-uri directe** către Anthropic, nu un singur gateway central.

### 4.1 `python -m app.decide`

Status: **P0 / nesigur pentru utilizare**.

- comportamentul implicit este maker-ul real; numai `--fake` îl dezactivează;
- dacă cheia există, comanda poate cheltui fără `--paid`, fără confirmare și fără plafon de USD;
- nu folosește rezervarea/fingerprint-ul runner-ului înainte de apel;
- rerularea aceleiași comenzi poate plăti din nou;
- clientul Anthropic nu este închis explicit pe această cale.

### 4.2 `python -m app.llm_smoke`

Status: **P0 / nesigur pentru utilizare**.

- prezența cheii este suficientă pentru efectuarea imediată a unui apel;
- nu cere confirmare explicită și nu cere activarea unui kill switch;
- apelul nu este rezervat și auditat în DB înainte de request;
- nu are buget USD și nici protecție anti-repetare.

Faptul că este „doar un apel” limitează amploarea, dar nu transformă calea într-una controlată.

### 4.3 `python -m shadow.runner --maker claude`

Status: **cea mai sigură cale existentă, dar încă NO-GO**.

Puncte bune:

- maker-ul plătit este explicit;
- există confirmare și `--max-llm-calls`;
- prefiltrul poate evita apeluri;
- cu `--persist`, rezervarea/dedupe-ul reduc apelurile repetate;
- clientul este închis în `finally`.

Probleme rămase:

1. `--max-llm-calls` numără apeluri logice `decide()`, nu încercări HTTP. Clientul are implicit
   `max_retries=3`, deci o singură decizie poate genera până la **4 request-uri**.
2. Auditul `llm_calls` este agregat per apel logic. Nu există un rând pre-attempt pentru fiecare
   request, deci timeout-ul după acceptarea request-ului poate lăsa cost real fără dovadă locală.
3. Crash-ul după acceptarea request-ului de provider, dar înainte de commit, poate conduce la
   reapelare după expirarea lease-ului. Garanția este cel mult *at-most-one-concurrent*, nu
   exactly-once și nu „resume nu replătește” în orice scenariu.
4. `--persist` nu este obligatoriu pentru maker-ul plătit. Fără el nu există dedupe/resume.
5. plafonul implicit este 50, nu 0; `--yes` ocolește confirmarea umană.
6. estimarea numită „worst-case” nu este un worst-case valid:
   - presupune că inputul are cel mult `max_tokens`, deși `max_tokens` limitează outputul;
   - nu multiplică estimarea cu numărul maxim de retry-uri;
   - nu include separat costul de cache write;
   - folosește o tabelă statică de prețuri, care poate deveni stale.

Documentația oficială Anthropic facturează separat input, output, cache write și cache read; de
aceea bugetul trebuie calculat din usage-ul real și reconciliat cu consola providerului:
<https://platform.claude.com/docs/en/about-claude/pricing>.

### 4.4 `shadow.online`

Status: **comportament corect în prezent**.

Maker-ul Claude este refuzat fiindcă daemonul nu are încă un buget sigur. Această protecție nu
trebuie eliminată până când gateway-ul central și bugetele zilnic/lunar sunt implementate.

## 5. Probleme de corectitudine încă deschise

### P0-C1 — identitatea snapshotului nu este folosită complet

Migrarea 0025 impune cheia:
`(symbol, provider, provider_symbol, pipeline_version, bar_close)`. Este o îmbunătățire reală.

Totuși:

- `latest_snapshot_bar_close()` filtrează numai `symbol + provider`;
- `snapshot_spread_status()` filtrează numai `symbol + bar_close + provider`;
- schedulerul apelează aceste funcții fără `provider_symbol` și `pipeline_version`.

După schimbarea mapping-ului de simbol sau a versiunii pipeline-ului, un rând vechi poate face
schedulerul să creadă că versiunea curentă este deja procesată sau poate verifica spreadul
snapshotului greșit.

În plus, două CSV-uri diferite pot avea aceeași identitate logică dacă folosesc același provider,
simbol și pipeline. Pentru replay reproductibil este necesar un `dataset_id`/hash de dataset.

### P0-C2 — reconcilierea „per-provider” este doar un skip

Codul nu mai închide un trade Polygon cu bare XTB, ceea ce este corect. Dar când providerul
trade-ului diferă de providerul procesului curent, trade-ul este doar ignorat, nu reconciliat cu
sursa lui înghețată.

Consecință: poziția rămâne deschisă, iar position gate-ul global pe simbol poate bloca pe termen
nelimitat procesarea providerului curent. Remediul trebuie să fie unul explicit:

- reconciliere grupată pe identitatea sursei înghețate și fetch din providerul respectiv; sau
- procedură de drain/quarantine/migrare, fără a pretinde că trade-ul a fost reconciliat.

Trade-ul trebuie să înghețe cel puțin provider, provider_symbol, pipeline/dataset și versiunea de
calendar folosită, nu doar numele providerului.

### P0-C3 — downtime-ul poate fi raportat fals

`last_decision_bar_across_runs()` folosește ultima **decizie**, nu ultima bară procesată de buclă.
În timpul unei poziții deschise, position gate-ul sare intenționat peste decizii. La tick-ul
următor, acele bare pot fi numărate drept downtime deși serviciul a fost online și a respectat
politica de o singură poziție.

În plus, gap-ul este persistat înainte ca reconcilierea și decizia curentă să se finalizeze. Un
eșec ulterior poate lăsa în DB un eveniment de „resume” care nu s-a încheiat cu procesarea barei.

Este necesar un ledger separat pentru `processed/attempted bars`, cu outcome explicit
(`decided`, `position_open`, `ineligible`, `error`, etc.). Downtime-ul se calculează față de
ultima bară procesată cu succes, nu față de ultima decizie.

### P1-C4 — sursa completă trebuie propagată și în continuitate

Lookup-urile de continuitate/downtime filtrează numai providerul. `provider_symbol`, datasetul și
versiunea calendarului trebuie incluse pentru a nu uni istorice incompatibile.

## 6. Ce este într-adevăr solid acum

- snapshoturile au o identitate mult mai bună și câmpuri obligatorii;
- rolul app nu mai are DELETE general pe tabelele de fapte, iar retenția are rol separat;
- run manifest/fingerprint include mult mai mult din configurația executabilă;
- configul shadow este validat strict;
- reconcilierea nu mai consumă tăcut bare de la alt provider;
- restore-ul verifică checksum și are guard operațional;
- suitele fără infrastructură sunt verzi;
- calea online refuză în mod corect maker-ul plătit nelimitat;
- prefilter, risk engine, shadow broker și auditul tranzacțional al rezultatului logic există.

Aceste lucruri justifică continuarea proiectului. Nu justifică încă efectuarea de apeluri plătite.

## 7. Blocante de produs înainte de măsurarea edge-ului

1. **Date istorice reale și înghețate:** suficient XAUUSD/GOLD, toate timeframe-urile, provenance,
   hash/dataset ID și control de calitate.
2. **Aceeași intrare gratuită:** replay determinist pe exact datasetul și configurația ce vor fi
   folosite de Claude; lanț complet persistat și vizibil în dashboard.
3. **DB/CI:** migrări de la zero și toate cele 336 teste într-o bază `_test`, apoi CI remote verde.
4. **Runtime:** Compose live și un soak determinist XTB, fără Claude, cu reconnect și gap audit.
5. **Costuri GOLD reale:** unitatea ratelor, long/short, triple swap, comision și valuta trebuie
   confirmate din specificația contului. Altfel edge-ul „net” nu este net de costuri reale.
6. **Granularitate:** XTB M1 end-to-end sau acceptarea formală a ambiguității M15; ticks rămân
   necesari pentru realism mai fin al fill-ului/slippage-ului.
7. **Știri:** provider live implementat sau news exclus explicit din contractul strategiei v1.
8. **Track record:** suficient shadow continuu pe date reale. Un canary AI validează integrarea,
   nu profitabilitatea.

Execuția demo/ipax și state machine-ul ordinelor nu sunt obligatorii pentru primul experiment
shadow plătit, dar sunt obligatorii înainte ca produsul să fie considerat executabil.

## 8. Gateway financiar obligatoriu

Înainte de orice apel plătit, toate entry point-urile trebuie să folosească aceeași componentă cu
următoarele invariants:

- `BRAIN_PAID_AI_ENABLED=false` implicit;
- model allowlist și model efectiv afișat înainte de request;
- maker plătit permis numai cu persistare și `run_id` explicit;
- buget per rulare, pe zi și pe lună, exprimat în USD, cu default 0;
- plafon pe **încercări HTTP**, nu doar pe decizii logice;
- pentru primul canary: retry-uri 0;
- rezervare atomică de buget și rând `attempt_started` înainte de request;
- finalizare cu request ID, usage și cost; stări distincte pentru timeout/unknown outcome;
- reconciliere ulterioară cu usage/billing din consola Anthropic;
- estimare care include input, output, cache write/read și retry budget;
- kill switch central folosit de `app.decide`, `app.llm_smoke`, `shadow.runner` și orice viitor
  daemon;
- teste CLI care dovedesc că fiecare cale refuză apelul când gate-ul este OFF.

`app.decide` trebuie să fie gratuit implicit. `app.llm_smoke` nu trebuie să ocolească gateway-ul.
Pentru runner, `--maker claude` trebuie să impună `--persist`, `--run-id`, buget și confirmare.

## 9. Ordinea de validare fără risipă

### Etapa A — zero cost

- [ ] Repară P0-C1, P0-C2 și P0-C3.
- [ ] Rulează migrările de la zero și toate testele DB într-o bază `_test` izolată.
- [ ] Rulează CI remote și backup/restore cu sentinelă.
- [ ] Pornește Compose live și verifică health/readiness/dashboard.
- [ ] Încarcă și îngheață datasetul real cu hash și provenance.
- [ ] Rulează replay determinist cu persistare pe aceleași date/config.
- [ ] Verifică manual în dashboard snapshot → evaluation → decision → trade → outcome.
- [ ] Rulează soak XTB determinist; verifică reconnect, downtime ledger și lipsa duplicatelor.
- [ ] Configurează costurile GOLD reale sau marchează rezultatul neeligibil pentru edge net.

### Etapa B — implementarea siguranței financiare

- [ ] Centralizează toate apelurile prin gateway-ul din secțiunea 8.
- [ ] Testează cu un fake transport: succes, timeout după send, 429, 5xx, crash înainte/după commit,
      restart, cap epuizat și concurență.
- [ ] Dovedește că `paid=false` produce zero request-uri din toate CLI-urile.

### Etapa C — canary plătit

- [ ] Cheie cu limită mică și modelul **Haiku** confirmat în config dump redactat.
- [ ] Run ID unic, persistare obligatorie, `max_http_attempts=1`, retry=0 și plafon USD mic.
- [ ] Un singur input deja validat gratuit și înghețat.
- [ ] După apel: verifică attempt, `llm_calls`, decision, request ID, tokenii și costul.
- [ ] Compară usage-ul local cu consola Anthropic înainte de al doilea apel.

### Etapa D — experiment limitat

Numai după canary: un lot mic, plafonat în request-uri și USD. Creșterea lotului se face doar dacă
auditul este complet, datele sunt reale, costurile sunt valide și rezultatul rămâne reproductibil.

## 10. Comparație cu `PREFLIGHT_READINESS.md`

| Afirmație din raportul existent | Verdict Codex |
|---|---|
| „Singurul blocant este lipsa datelor reale” | **Respinsă.** Mai există blocante P0 de cost control și corectitudine. |
| „Calea de bani este sănătoasă” | **Prea puternică.** Runner-ul are controale utile, celelalte două CLI-uri nu. |
| „Plafon dur `max_llm_calls`” | **Calificat.** Plafonează decizii logice, nu request-uri/retry-uri. |
| „Resume nu replătește” | **Calificat.** După persistare da; în fereastra provider-acceptat/DB-necomis, nu. |
| „Fiecare apel este în `llm_calls` în aceeași tranzacție” | **Respinsă literal.** Rezultatul logic reușit este atomic cu decizia; încercările HTTP și crash-urile pre-commit nu sunt toate auditate. |
| R3-1 identitate completă | **Parțial.** Constrângerea este bună, lookup-urile nu folosesc cheia completă și lipsește dataset ID. |
| R3-2 reconciliere cu provider înghețat | **Parțial.** Previne contaminarea, dar doar sare trade-ul incompatibil și îl poate lăsa blocant. |
| R3-5 downtime persistent | **Implementat ca tabel, semantic incomplet.** Poate raporta fals bare sărite de position gate. |
| Append-only/retention | **Îmbunătățire reală.** Job-ul de retenție rămâne follow-up, nu blocant pentru canary. |

Raportul existent trebuie actualizat după remedierea acestor puncte și trebuie să folosească drept
baseline commitul pe care au fost executate efectiv toate verificările.

## 11. Definiția de GO pentru primul dolar

Statusul poate deveni **GO pentru un singur canary**, nu GO pentru backtest plătit amplu, numai
dacă toate condițiile sunt simultan adevărate:

1. zero teste locale/DB/CI eșuate sau sărite din lipsă de infrastructură;
2. P0-C1..C3 reparate și acoperite de regresii;
3. dataset real înghețat, verificat și rulat gratuit end-to-end;
4. dashboardul reproduce lanțul complet fără inconsistențe;
5. gateway-ul financiar este unic, default OFF și testat pe toate entry point-urile;
6. primul canary are exact un request, retry 0 și plafon USD explicit;
7. modelul efectiv este Haiku, nu defaultul Sonnet din cod;
8. costul local este reconciliat cu consola providerului înainte de continuare.

Până atunci, cheia Anthropic configurată trebuie considerată un risc operațional: comenzile care
pot face apeluri trebuie evitate sau cheia scoasă temporar din mediul folosit pentru testele
gratuite.

