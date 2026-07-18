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
- DSN-ul, cheile API, parola DB și ticketul XTB nu sunt returnate către browser;
- endpointurile trading_hands folosite sunt exclusiv `GET /status`, `GET /quote` și
  `GET /candles`.

## Ce afișează

- conectivitate XTB demo, quote și ultimele lumânări;
- indicator explicit `trading_enabled` (alertă critică dacă ordinele reale sunt permise);
- versiunea DB, commitul Git și avertisment pentru working tree dirty;
- heartbeat separat pentru collector/Shadow Online și auditul fiecărui tick (succes/eroare/durată);
- lag feed XTB ↔ ultim snapshot brain;
- alerte pentru date stale, trade-uri shadow vechi, rezervări expirate și audit LLM incomplet;
- cronologia ultimelor bare cu features, eligibilitate, decizie, risk și outcome;
- poziții shadow deschise cu mark-to-market **indicativ, brut** și timeout estimat;
- experimente clasificate (`shadow_online`, `executable_backtest`, `event_study`, `legacy`, `test`);
- expectancy implicit numai din run-uri cu manifest verificat **și working tree clean**; un run legacy/dirty poate fi selectat,
  dar este etichetat explicit `NEVERIFICAT`;
- fiecare apel LLM auditat, tokeni, cache, retry, latență și cost.

Datele se actualizează la 10 secunde sau manual. Filtrul `Experiment` izolează un `run_id`.

Dashboardul nu pornește procesele de calcul. Pentru heartbeat și date noi, pornește separat:

```bash
python -m app.jobs          # collector M15 continuu
python -m shadow.online     # shadow determinist, fără ordine și fără apeluri Claude
```

Ambele scriu în `service_heartbeats` și `pipeline_runs` (migrarea 0023). Dacă nu rulează,
dashboardul spune explicit acest lucru în loc să deducă greșit starea din vechimea unei bare.
