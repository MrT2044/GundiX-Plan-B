# GundiX Plan B – Beobachtung, Paper-Trading, gesperrte Ausführung

Verbindliche Grundlagen: `00_GESAMTPLAN.md`, `02_PLAN_B_TRADE_EXECUTION.md`,
`03_S0_INTEGRATIONSVERTRAG.md`. Der Schnittstellenstand für Plan A steht in
`INTERFACE_STATUS.md`, offene Vertragsänderungen unter `CCR/`.

**Live-Ausführung ist in diesem Build gesperrt.** Es gibt keinen Signer und keinen
Execution-Adapter, und `assert_live_allowed()` wirft bedingungslos.

---

## Reproduzierbarer Start

Python 3.12 (exakt), einmalig einrichten:

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
```

Datenbank anlegen und den Paper-Pfad gegen die aufgezeichneten Transaktionen laufen lassen:

```bash
.venv/Scripts/python -m alembic -c alembic.ini upgrade head
```

```bash
.venv/Scripts/python scripts/make_synthetic_selection.py --approve
```

```bash
GUNDIX_MODE=PAPER .venv/Scripts/python scripts/run_paper.py --once --config config/runtime.example.yaml
```

Der Modus kommt **ausschließlich** aus `GUNDIX_MODE`. Fehlt er oder ist er ungültig, bricht
Plan B den Start ab statt zu raten (S0 §8.2).

Alles prüfen:

```bash
.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy
```

---

## Was wo liegt

```text
contracts/                gemeinsam mit Plan A, Änderungen nur per CCR
  schemas/                JSON Schema, die Quelle der Wahrheit
  python/gundix_contracts/  die dagegen getesteten Pydantic-Modelle
  golden/<case_id>/       Golden Cases im S0-§6-Layout
  golden_raw/             unveränderte Aufzeichnungen, Rohmaterial für die Cases
  AGGREGATION.md          wie aus Instruktionen Netto-Swaps werden
src/
  common/                 Modus, Konfiguration, Uhr, DB, Logging, Composition Root
  stream/                 Quellen, Decoder, Pipeline
  paper/                  Selection, Policy, Risk, Positionen, Quotes, Paper-Broker, Export
  execution/              Shadow, Live-Gates, Reconciliation, Signer (importgesperrt)
artifacts/observations/   was Plan A von uns liest
CCR/                      offene Vertragsänderungen samt Messung
```

---

## Die drei Entscheidungen, die den Rest erklären

**1. Die SOL-Seite eines Swaps steht nicht in den Instruktionen.** Eine
Pump.fun-Bonding-Curve verschiebt Lamports mit `sub_lamports`/`add_lamports` direkt im
Programm – dazu existiert keine lesbare Instruktion, nur die Kontostände ändern sich. Der
Decoder liest deshalb die Token-Seite aus den Transfers (exakt) und die SOL-Seite aus
*Sphären-Buchhaltung*: Lamport-Änderung des Wallets plus seiner Tokenkonten, korrigiert um
Transaktionsgebühr, Miete und Transfers außerhalb des Swaps. Gegen das programmeigene
`TradeEvent` von Pump.fun stimmt das auf das Lamport überein
(`tests/stream/test_decoder_program_events.py`).

**2. Idempotenz kommt aus Identität, nicht aus Buchführung.** `event_id` und `intent_id`
sind deterministisch. Ein erneut verarbeiteter Vorgang erzeugt denselben Primärschlüssel,
und der zweite Schreibvorgang wird von der Datenbank abgelehnt – nicht von einer Prüfung,
die jemand vergessen kann. Zwei partielle Unique-Indizes tragen das:
`uq_copy_intents_event_execute` und `uq_execution_results_effective`.

**3. Alles zu einer Transaktion passiert in einer Datenbanktransaktion.** Ein an beliebiger
Stelle abgebrochener Prozess hat entweder alles getan oder nichts. Es gibt keinen Zustand,
in dem ein Intent ohne seine Entscheidung existiert oder eine Position sich bewegt hat ohne
das Ergebnis, das sie rechtfertigt.

---

## Betriebsmodi

| Modus | Decodiert | Erzeugt Intents | Führt aus | Signer ladbar |
|---|---|---|---|---|
| `RESEARCH` | ja | nein | nein | nein |
| `OBSERVE` | ja | nein | nein | nein |
| `PAPER` | ja | ja | simuliert | nein |
| `SHADOW` | ja | ja | simuliert gegen echte Quotes | nein |
| `LIVE` | – | – | **gesperrt** | – |

In `PAPER` und `SHADOW` ist `src/execution/signer.py` **nicht importierbar** – der Import
wirft, nicht erst der Aufruf (S0 §8.3, geprüft in `tests/execution/test_safety.py`).

## Not-Aus

Datei anlegen, Pfad aus `risk.kill_switch_file`:

```bash
echo "grund" > data/KILL_SWITCH
```

Absichtlich der dümmste denkbare Mechanismus: er wirkt auch, wenn der Prozess hängt und die
Datenbank gesperrt ist. Kein Codepfad entfernt die Datei je wieder.

---

## Grenzen dieses Builds

- Aus 10 echten Mainnet-Transaktionen entstehen unter der vertragskonformen Regel 9
  **null** verwertbare Events. Siehe `CCR/CCR-001-unknown-program-quarantine.md`.
- Kein Dauerbetrieb gegen echtes RPC, keine gemessene Live-Latenz.
- Der Jupiter-Quote-Adapter ist nie gegen die Live-API gelaufen.
- Kein WebSocket-Listener: auf dem öffentlichen Endpunkt nicht testbar, und ein Mock-Test
  wäre kein Nachweis.
