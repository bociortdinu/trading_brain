# Trading Brain Dashboard

Consolă locală, read-only, pentru a vedea traseul real al sistemului:

`XTB → bară M15 → features → eligibilitate → decizie → risk → trade shadow → rezultat`

## Pornire

Din rădăcina `trading_brain`:

```bash
source .venv/bin/activate
python -m dashboard --open
```

Interfața pornește implicit pe <http://127.0.0.1:8080>. Port alternativ:

```bash
python -m dashboard --port 8090
```

Dashboardul folosește configurația `BRAIN_*` existentă și are nevoie de extra-ul DB:

```bash
pip install -e '.[db]'
```

## Garanții de siguranță

- serverul ascultă implicit numai pe loopback (`127.0.0.1`);
- nu există endpointuri de ordin, POST, PUT, PATCH sau DELETE;
- fiecare conexiune PostgreSQL rulează cu `SET TRANSACTION READ ONLY`;
- redactarea este **impusă de cod**, nu doar prin convenție: răspunsul `/status` al brokerului trece
  printr-un **whitelist** (`connected`, `environment`, `trading_enabled`, …) — numărul de cont NU
  ajunge în browser — iar întregul răspuns trece printr-un **redactor recursiv** care înlocuiește
  valorile de sub chei sensibile (`password`, `token`, `api_key`, `dsn`, `authorization`, `ticket`,
  `account`, `secret`, …) oriunde ar apărea, inclusiv în manifeste, `details`, rezultate de pipeline
  și mesaje de eroare;
- endpointurile trading_hands folosite sunt exclusiv `GET /status`, `GET /quote` și
  `GET /candles`.

## Organizare (tab-uri)

Deasupra e o **bară de sănătate mereu vizibilă** (conexiune XTB/piață/brain/DB + număr de alerte),
apoi **navigație pe tab-uri** (nu mai e un scroll unic haotic):

- **Prezentare** — carduri de stare, quote + grafic preț, metrici track record, traseul ultimei
  decizii (bară → features → eligibilitate → decizie → risk → trade) și lista de alerte.
- **Decizii** — timeline-ul M15 complet; click pe orice bară pentru datele brute.
- **Trade-uri** — poziții shadow deschise (mark-to-market **indicativ, brut**), trade-uri
  **quarantined** (deschise sub alt provider, scoase din gate) și **istoricul** celor închise.
- **Experimente** — run-uri persistate cu tip/validitate + manifest, plus **dataseturile înghețate**
  (hash de conținut) de care se leagă rulările pentru reproductibilitate.
- **AI & Cost** — bugetele plătite (**spend run/zi/lună vs plafon**, orfani, nereconciliate),
  ledgerul **`paid_attempts`** (per încercare HTTP: started → completed/timeout/error) și apelurile
  `llm_calls` auditate.
- **Operațional** — servicii + heartbeat, tick-uri (`pipeline_runs`), rezervări active,
  **goluri de downtime** și **ledgerul de bare procesate** (referința pentru downtime).
- **Bază de date** — versiunea schemei, numărul (aproximativ) de rânduri per tabel și conflictele
  de snapshot.

Metricile de track record folosesc implicit **doar run-uri cu manifest verificat și working tree
clean**; un run legacy/dirty poate fi selectat, dar e etichetat `NEVERIFICAT`. Datele se actualizează
la 10 secunde sau manual; filtrul `Experiment` izolează un `run_id`; tabul activ e reținut între
reîncărcări.

Dashboardul nu pornește procesele de calcul. Pentru heartbeat și date noi, pornește separat:

```bash
python -m app.jobs          # collector M15 continuu
python -m shadow.online     # shadow determinist, fără ordine și fără apeluri Claude
```

Ambele scriu în `service_heartbeats` și `pipeline_runs` (migrarea 0023). Dacă nu rulează,
dashboardul spune explicit acest lucru în loc să deducă greșit starea din vechimea unei bare.
