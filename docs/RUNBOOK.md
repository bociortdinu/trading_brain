# Runbook — stack operabil `trading_brain`

Un singur mod documentat de a porni și supraveghea `trading_brain`. Stackul rulează în Docker
Compose: **PostgreSQL + migrate (o singură dată) + dashboard read-only** ca nucleu, plus o
**buclă de date opțională** (collector + `shadow.online`) care are nevoie de o sursă de piață.

> Nu se trimite niciun ordin. `execution_ready` este mereu `False`; `trading_hands` e folosit
> doar pentru citire (date). Vezi [PROJECT_COMPLETION_REPORT.md](PROJECT_COMPLETION_REPORT.md).

## Cerințe

- Docker + Docker Compose v2/v5.
- (Pentru bucla de date pe `xtb`) un `trading_hands` care rulează pe host, autentificat în XTB.

## Prima pornire

```bash
cd trading_brain
cp .env.compose.example .env.compose     # apoi editează parolele (POSTGRES_PASSWORD, BRAIN_APP_PASSWORD)
make up                                   # build + porneste db, migrate, dashboard
make ps                                   # migrate trebuie să fie Exited(0); dashboard healthy
```

Dashboard: <http://127.0.0.1:8080>. La prima pornire va arăta corect `NO_VERIFIED_TRACK_RECORD`
(nu există încă un run cu manifest verificat) și `XTB_DISCONNECTED` (bucla de date nu rulează încă).

`migrate` este one-shot: creează rolul de aplicație cu privilegii minime + baza runtime (ca admin),
apoi aplică migrările de la zero. Restul serviciilor pornesc doar după ce `migrate` reușește.

## Pornirea buclei de date (opțional)

Bucla (collector + `shadow.online`) are nevoie de o sursă de piață. Alege în `.env.compose`:

- `BRAIN_MARKET_DATA_PROVIDER=xtb` + `BRAIN_TRADING_HANDS_URL` către `trading_hands` (implicit
  `http://host.docker.internal:4000`) — recomandat, aceeași sursă ca execuția;
- `polygon` + `BRAIN_POLYGON_API_KEY`;
- `csv` + `BRAIN_CSV_DIR` (montează directorul).

```bash
make up-data     # porneste nucleul + collector + online
make logs        # urmarește
```

Fără o sursă accesibilă, collector/online reîncearcă (erori tranzitorii de provider) și
dashboardul rămâne `XTB_DISCONNECTED` — nucleul rămâne sănătos.

## Reautentificare XTB (expirarea TGT)

`trading_hands` deține sesiunea XTB; TGT-ul expiră periodic și cere login din browser. Procedură:

1. În `trading_hands/browser-auth`: `GO_BINARY="$(command -v go)" npm start` (relogin în browser).
2. Nu trebuie repornit nimic în `trading_brain`: `collector`/`online` reîncearcă providerul și își
   revin singure când `trading_hands` redevine disponibil.
3. Verifică în dashboard că `XTB_DISCONNECTED` dispare.

## Operare zilnică

```bash
make ps           # stare
make logs         # loguri live
make down         # oprește (păstrează datele)
make backup       # dump custom-format în backups/
make up           # repornește
```

## Backup / restore

```bash
make backup                              # backups/trading_brain_YYYYMMDD_HHMMSS.dump
make restore FILE=backups/<nume>.dump    # restore în baza runtime (--clean --if-exists)
```

Programează `make backup` periodic (cron/systemd timer) și **testează** un restore înainte să te
bazezi pe el.

## Oprire / reset

```bash
make down          # oprește containerele, PĂSTREAZĂ volumul bazei
make down-clean    # DISTRUCTIV: șterge și volumul bazei (pierzi toate datele)
```

## Depanare

| Simptom | Cauză probabilă | Acțiune |
|---|---|---|
| `Missing .env.compose` | nu ai copiat template-ul | `cp .env.compose.example .env.compose` |
| `migrate` iese cu eroare de auth | parole nepotrivite între admin și app | verifică `POSTGRES_PASSWORD` / `BRAIN_APP_PASSWORD` |
| Conflict de port pe 5433 | rulează și Postgres-ul din `trading_hands` | folosește UN singur Postgres, sau schimbă `BRAIN_DB_PORT` |
| `XTB_DISCONNECTED` persistă | `trading_hands` nu rulează / TGT expirat | pornește/reautentifică `trading_hands` (vezi mai sus) |
| `NO_VERIFIED_TRACK_RECORD` | niciun run cu manifest verificat încă | așteptat până rulează `online` suficient cu manifest curat |

## Notă despre Postgres partajat

`trading_hands` și `trading_brain` folosesc aceeași instanță Postgres (portul 5433). Nu porni ambele
compose-uri cu propriul `db` pe același port — rulează un singur Postgres, sau schimbă `BRAIN_DB_PORT`
și fă ambele servicii să indice spre aceeași bază.
