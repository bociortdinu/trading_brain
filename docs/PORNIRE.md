# Cum pornești sistemul, pas cu pas

Ghid pentru cineva care se așează la calculator și nu ține minte nimic din context.
Toate comenzile se dau din `~/WORKSPACE/XTB/trading_brain` dacă nu scrie altfel.

**Regula de aur:** doar comenzile marcate cu 💸 cheltuie bani. Restul sunt gratuite.
Nimic din sistem nu trimite ordine reale decât dacă adaugi explicit `--live`.

---

## Pasul 0 — Ce trebuie să fie pornit

Două lucruri, în ordinea asta.

### PostgreSQL

Rulează nativ pe portul **5433**. Verifici:

```bash
ss -ltn | grep 5433
```

Dacă vezi o linie, e pornit. Dacă nu:

```bash
sudo systemctl start postgresql
```

### trading_hands (legătura cu XTB)

Ăsta e serviciul Go care vorbește cu brokerul. Fără el nu ai date live și nu poți executa.

**Verifică ÎNTÂI dacă nu rulează deja:**

```bash
curl -s http://127.0.0.1:4000/status
```

Dacă primești un JSON cu `"connected":true`, **e deja pornit — sari peste pasul ăsta.**

> De ce contează: dacă dai `npm start` cât rulează o instanță, se face un login NOU la XTB
> (consumă un tichet), apoi serviciul găsește portul 4000 ocupat, scrie
> `bind: address already in use` și se închide singur. Nu strică nimic, dar pare că a eșuat
> pornirea când de fapt totul era în regulă.

Dacă NU răspunde nimic, atunci îl pornești:

```bash
cd ~/WORKSPACE/XTB/trading_hands/browser-auth
npm start
```

Ce face: deschide un browser invizibil, se loghează la XTB cu datele din
`trading_hands/config/.env`, ia un tichet de sesiune și pornește serviciul Go. Durează ~20 de
secunde. **Tichetul e de unică folosință** — dacă repornești, se face un login nou.

Îl lași să ruleze în terminalul lui — se închide dacă închizi terminalul. Ca să meargă
independent:

```bash
cd ~/WORKSPACE/XTB/trading_hands/browser-auth
setsid nohup npm start > /tmp/hands.log 2>&1 < /dev/null & disown
```

### Cum îl repornești corect

Dacă vrei să-l repornești (de exemplu după ce ai schimbat `TRADING_ENABLED`), oprește-l
întâi, altfel dai peste conflictul de port de mai sus:

```bash
pkill -f "launcher.mjs"                        # oprește lansatorul
pkill -f "exe/trading_hands"                   # oprește serviciul Go
sleep 3
ss -ltn | grep 4000 || echo "port liber"       # confirmă că s-a eliberat
```

Abia apoi `npm start` din nou.

> **Atenție la `pkill`:** nu folosi `pkill -f trading_hands`, fiindcă tiparul se potrivește și
> cu propriul tău shell dacă ești în directorul `trading_hands` — îți omori terminalul. Am
> pățit-o. Folosește tiparele exacte de mai sus.

### Verifici că merge

```bash
curl -s http://127.0.0.1:4000/status
```

Răspuns bun:

```json
{"account":"21842412","connected":true,"environment":"demo","trading_enabled":true}
```

- `connected: true` — sesiunea cu XTB e vie
- `environment: demo` — **bani falși**. Dacă vezi altceva, oprește-te și verifică.
- `trading_enabled` — dacă e `true`, sistemul POATE trimite ordine reale (pe demo).
  Îl schimbi în `trading_hands/config/.env`, apoi repornești serviciul.

---

## Pasul 1 — Adu date proaspete (opțional, dar recomandat)

Arhiva de bare e în `data/bars/`. XTB servește o fereastră care se rotește, așa că barele
vechi dispar definitiv dacă nu le salvezi. Harvest-ul le adaugă la ce ai deja.

```bash
python -m app.harvest --out data/bars --count 10000
```

Durează ~1 minut. La final îți spune câte bare sunt evaluabile pentru backtest. Dacă zice
`NOT backtest-ready`, mărește `--count`.

> Rulează asta din când în când (zilnic e ideal). E gratuit și e singura cale de a acumula
> istoric — ce nu salvezi azi nu mai poți lua mâine.

---

## Pasul 2 — Alege ce vrei să faci

Trei moduri. Începe cu primul.

### A. Backtest pe date istorice — GRATUIT

Rulează strategia peste barele salvate, fără să atingă piața. Cel mai bun mod de a testa o
schimbare.

```bash
BRAIN_MARKET_DATA_PROVIDER=csv BRAIN_CSV_DIR=data/bars \
  python -m shadow.runner --count 3917
```

Ce vezi la final:

```
[backtest] bars_evaluated=3718 prefiltered_out=1463 decided=2202 approved=4 trades_opened=4
[metrics] {'trades_total': 4, 'win_rate': 0.25, 'total_r': -1.331, ...}
```

- `bars_evaluated` — câte bare a analizat
- `prefiltered_out` — câte au fost respinse gratuit, înainte de model
- `decided` — câte au ajuns la decident (astea ar costa bani cu Claude)
- `trades_opened` — câte au devenit tranzacții
- `total_r` — rezultatul, în multipli de risc

Opțiuni utile:

```bash
--stride 7      # decide la fiecare a 7-a bară (eșantion întins pe toată fereastra)
--persist --run-id numele-meu    # salvează în baza de date, ca să poți compara
```

### B. Urmărire în timp real, fără costuri — GRATUIT

Decide pe barele M15 pe măsură ce se închid, cu strategia deterministă (nu Claude).
Tranzacțiile sunt virtuale.

```bash
python -m shadow.online --maker deterministic
```

Se trezește la fiecare închidere de bară (din 15 în 15 minute). Oprești cu `Ctrl+C`.

### C. Cu Claude — 💸 COSTĂ BANI

Aici intră modelul real. Are nevoie de trei lucruri setate explicit, altfel refuză să pornească.

```bash
BRAIN_MARKET_DATA_PROVIDER=xtb \
BRAIN_PAID_AI_ENABLED=true \
BRAIN_DECISION_MODEL=claude-sonnet-5 \
BRAIN_PAID_AI_MODEL_ALLOWLIST='["claude-sonnet-5"]' \
BRAIN_PAID_BUDGET_RUN_USD=0.50 \
BRAIN_PAID_BUDGET_DAY_USD=2.00 \
BRAIN_PAID_BUDGET_MONTH_USD=3.00 \
  python -m shadow.online --maker claude --max-llm-calls 10 --minutes 60 \
    --run-id proba-$(date +%Y%m%d-%H%M) --yes
```

Ce înseamnă fiecare bucată:

| Parametru | Rol |
|---|---|
| `BRAIN_PAID_AI_ENABLED=true` | comutatorul general. Fără el, nimic nu cheltuie. |
| `BRAIN_PAID_BUDGET_*_USD` | plafoane în dolari. Implicit sunt **0**, adică blocat. |
| `--max-llm-calls 10` | plafon dur de apeluri. **Obligatoriu** cu `--maker claude`. |
| `--minutes 60` | oprește după o oră |
| `--run-id` | numele experimentului. Folosește unul **nou** de fiecare dată. |
| `--yes` | sare peste confirmarea de cost |

Cost orientativ: **~$0.008 per decizie** pe Sonnet 5. O oră ≈ 4 bare ≈ 3 cenți.

### D. Ordine reale pe demo — 💸 COSTĂ BANI (dar tranzacțiile sunt pe demo)

Adaugi `--live` la comanda de mai sus. Atunci o decizie aprobată devine ordin real la XTB.

```bash
  ... python -m shadow.online --live --maker claude --max-llm-calls 10 --minutes 60 \
        --run-id live-$(date +%Y%m%d-%H%M) --yes
```

Ca să meargă, **ambele** trebuie să fie adevărate:
- `trading_enabled: true` în `/status` (se schimbă în configul lui trading_hands)
- flagul `--live`

Protecții active automat: maximum o poziție deschisă, pauză de 15 minute între ordine,
refuz dacă contul nu e demo, refuz dacă volumul brokerului depășește plafonul.

---

## Pasul 3 — Vezi ce s-a întâmplat

Panou local, doar citire:

```bash
python -m dashboard --open
```

Deschide `http://127.0.0.1:8080`. Arată starea XTB și a bazei, deciziile, tranzacțiile,
alertele și costul apelurilor plătite. Nu poate trimite ordine.

---

## Pasul 4 — Cum oprești tot

```bash
pkill -f "shadow.online"                 # bucla de decizii
pkill -f "node launcher.mjs"             # trading_hands
```

Verifici că nu a rămas nicio poziție deschisă:

```bash
curl -s http://127.0.0.1:4000/positions
```

Trebuie să răspundă `[]`. Dacă nu, ai o poziție deschisă la broker — o închizi din aplicația
XTB sau cu `curl -X POST http://127.0.0.1:4000/close/ID`.

---

## Lucruri de știut înainte să te lovești de ele

**Nu comite în git cât rulează ceva.** Fiecare rulare își fixează amprenta codului. Dacă
schimbi codul la mijloc, rularea moare cu `RunConfigMismatch`. E o protecție, nu un bug:
rezultatele trebuie să aparțină unei singure versiuni de cod. Folosește un `run-id` nou după
fiecare commit.

**Un `run-id` = o configurație.** Dacă schimbi orice parametru, folosește alt nume, altfel
sistemul refuză să amestece.

**Dacă nu iese niciun trade, nu e neapărat stricat.** Sistemul refuză barele în care imaginea
e contradictorie, iar aia e starea normală a pieței în cam două treimi din timp.

**Bugetele sunt 0 implicit.** Asta e intenționat: o cheie API configurată nu trebuie să fie
niciodată suficientă ca să cheltuie.
