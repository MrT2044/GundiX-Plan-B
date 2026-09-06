# GundiX – Gesamtplan und Integrationsvertrag

**Status:** Verbindliche Planungsgrundlage  
**Zweck:** Zwei Claude-Instanzen entwickeln parallel zwei Teile eines einzigen Solana-Copy-Trading-Systems, ohne inkompatible Datenmodelle, doppelte Logik oder eine riskante Live-Freischaltung zu erzeugen.

---

## 1. Ziel des Projekts

GundiX soll langfristig drei Aufgaben als ein zusammenhängendes Programm erfüllen:

1. Viele Solana-Wallets aus mehreren Kandidatenquellen finden.
2. Durch eigene, reproduzierbare Analyse diejenigen Wallets auswählen, deren Trades auch für einen zeitlich verzögerten Kopierer nach Gebühren und Slippage profitabel gewesen wären.
3. Die Trades einer freigegebenen Watchlist zunächst im Paper-Modus und erst nach bestandenen Sicherheits- und Wirtschaftlichkeitstests automatisch ausführen.

Das Projekt optimiert nicht auf den höchsten angezeigten Wallet-PnL. Die Kernfrage lautet:

> Welche Wallet wäre für GundiX nach realistischer Erkennungs- und Ausführungsverzögerung, Slippage, Preiswirkung, Gebühren und fehlgeschlagenen Transaktionen tatsächlich kopierbar gewesen?

Der spätere Vergleich alternativer Exit-Regeln ist eine eigene Folgephase. Phase 1 bewertet zunächst das Kopieren der beobachteten Ein- und Ausstiege.

---

## 2. Verbindliche Aufteilung

### Plan A – Wallet-Discovery und Selektion

Plan A besitzt:

- `src/discovery/`
- `src/research/`
- `src/backtest/`
- `tests/discovery/`
- `tests/research/`
- `tests/backtest/`

Plan A liefert versionierte Kandidaten-, Statistik- und Selektionsartefakte. Plan A sendet keine Orders und besitzt keinen privaten Schlüssel.

### Plan B – Beobachtung und Trade-Ausführung

Plan B besitzt:

- `src/stream/`
- `src/paper/`
- `src/execution/`
- `tests/stream/`
- `tests/paper/`
- `tests/execution/`

Plan B bewertet nicht eigenständig, welche Wallet wirtschaftlich gut ist. Es konsumiert ausschließlich eine durch Plan A erzeugte und durch einen Menschen freigegebene Watchlist.

### Gemeinsamer Bereich

Beide Teile verwenden:

- `src/contracts/` für generierte Python-Typen
- `contracts/schemas/` für JSON Schemas
- `contracts/golden/` für gemeinsam erwartete Testfälle
- `config/` für nicht geheime Konfiguration
- `artifacts/` für versionierte Übergaben
- `migrations/` für Datenbankmigrationen

Gemeinsame Verträge werden vor der parallelen Implementierung festgelegt. Danach darf keine Seite Schemafelder stillschweigend umdeuten, entfernen oder umbenennen.

---

## 3. Verbindlicher Technologie-Stack

Beide Pläne verwenden denselben Stack:

- **Sprache:** Python 3.12
- **Paketverwaltung:** `uv` mit einem gemeinsamen `pyproject.toml` und `uv.lock`
- **Validierung:** Pydantic v2 plus JSON Schema
- **HTTP/WebSocket:** `httpx` und eine gemeinsam ausgewählte Async-WebSocket-Bibliothek
- **Solana:** `solders` und, nur wo erforderlich, `solana-py`
- **Analytik:** Polars und DuckDB
- **Persistenz:** SQLite in Research/Paper; Datenzugriff über SQLAlchemy 2 und Alembic
- **Rohdaten:** Parquet, unveränderlich und mit Snapshot-ID
- **Tests:** pytest, pytest-asyncio, Hypothesis für kritische Invarianten
- **Qualität:** Ruff und mypy
- **Zeit:** UTC auf Speicher- und Vertragsebene; Darstellung darf lokalisiert werden
- **Geld und Tokenmengen:** niemals binäre Floats; rohe Integer, `Decimal` oder skalierte Integer

Kein zweites Backend, keine zweite Datenbankabstraktion und keine zweite Definition derselben Domain-Objekte innerhalb eines Teilplans.

---

## 4. Zielarchitektur

```text
Kandidatenquellen
    |
    v
Plan A: Discovery -> Normalisierung -> Wallet-Analyse -> OOS-Copy-Backtest
    |                                                    |
    | candidates.jsonl                                  | selection.json
    v                                                    v
Plan B: Live-Listener -> Swap-Normalisierung -> Policy/Guard -> PaperBroker
                                                           |
                                                           v
                                                    ExecutionAdapter
                                                           |
                                             Paper | Shadow | später Live

Plan B -> latency.jsonl + paper_fills.jsonl -> Plan A Re-Evaluation
```

Es gibt nur eine kanonische Repräsentation eines erkannten Swaps. Historische Parser und Live-Decoder müssen dieselben `SwapEvent`-Objekte erzeugen.

---

## 5. Gemeinsame Verträge vor Beginn der Fachimplementierung

Vor paralleler Arbeit werden mindestens folgende Schemas unter `contracts/schemas/` angelegt:

### 5.1 `SwapEvent`

Pflichtfelder:

- `schema_version`
- `event_id`
- `signature`
- `slot`
- `block_time_utc`
- `transaction_index`
- `instruction_path`
- `wallet`
- `base_mint`
- `quote_mint`
- `side`
- `base_amount_raw`
- `quote_amount_raw`
- `base_decimals`
- `quote_decimals`
- `venue`
- `pool`
- `router`
- `success`
- `source`
- `observed_at_utc`
- `finality`

Regeln:

- `event_id` wird deterministisch aus Chain, Signatur, Wallet, Instruktionspfad und Netto-Swap-Index gebildet.
- Router-Hops werden gespeichert, aber für Wallet-PnL zum Nettoeffekt aggregiert.
- Ein Transfer ist kein Swap.
- Eine fehlgeschlagene Transaktion ist kein ausgeführter Trade.
- Unbekannte Instruktionen werden messbar protokolliert und niemals still verworfen.

### 5.2 `WalletCandidate`

Enthält Wallet-Adresse, Quellen, Beobachtungsfenster, erste/letzte Sichtung, Labels und optionale Reichweiteninformationen. Ranglistenwerte sind Herkunftsdaten, keine Wahrheit.

### 5.3 `WalletStats`

Enthält unter anderem:

- Anzahl rekonstruierter und abgeschlossener Positionen
- aktive Tage und Trades pro Tag
- realisierter PnL in SOL und USD
- Winrate, Profit Factor, Median-Trade und Erwartungswert
- Drawdown
- Haltedauer p10/p50/p90
- PnL ohne beste 1 %, 5 % und besten Einzeltrade
- PnL nach Teilfenster
- Anteil schneller Trades
- typische Positionsgröße und Poolliquidität
- Datenabdeckung und Anteil unbekannter Trades
- Ausschlussgründe

### 5.4 `TraderSelection`

Enthält:

- `selection_id`
- Erstellungszeit und Daten-Snapshot
- Code-Commit und Parameter-Hash
- Auswahl- und Evaluationsfenster
- Score-Version
- Wallets mit Gewicht und Status
- angewendete Filter
- manuelle Freigabe mit Zeitpunkt
- Ablaufdatum der Auswahl

### 5.5 `CopyIntent`

Plan B erzeugt aus einem normalisierten Trade einen noch nicht ausgeführten Auftrag:

- eindeutige Intent-ID und Quell-Event-ID
- Quell-Wallet und Token
- Buy/Sell
- Zielgröße
- maximale Slippage
- Ablaufzeit
- Betriebsmodus
- Entscheidung und Begründung
- verwendete Selection-Version

### 5.6 `ExecutionResult`

Enthält Quote, erwarteten und realisierten Preis, Gebühren, Latenzen, Signatur, Status und maschinenlesbaren Fehlergrund. Ein Intent kann durch Idempotenz niemals doppelt ausgeführt werden.

### 5.7 Versionsregeln

- Jede Datei enthält `schema_version`.
- Breaking Changes erhöhen die Major-Version.
- Leser lehnen unbekannte Major-Versionen fail-closed ab.
- JSON Schema ist die Quelle der Wahrheit; Python-Modelle werden daraus erzeugt oder dagegen getestet.
- Zeitstempel sind ISO-8601 in UTC.
- Wallets und Mints werden nicht als frei formatierter Anzeigename behandelt.

---

## 6. Gemeinsame Repository-Struktur

```text
GundiX/
  00_GESAMTPLAN.md
  01_PLAN_A_WALLET_FILTER.md
  02_PLAN_B_TRADE_EXECUTION.md
  pyproject.toml
  uv.lock
  .env.example
  config/
    research.example.yaml
    runtime.example.yaml
  contracts/
    schemas/
    golden/
    AGGREGATION.md
  src/
    contracts/
    discovery/
    research/
    backtest/
    stream/
    paper/
    execution/
  artifacts/
    candidates/
    selections/
    fill_calibration/
    observations/
  data/                 # gitignored
  migrations/
  tests/
    contracts/
    discovery/
    research/
    backtest/
    stream/
    paper/
    execution/
    integration/
```

---

## 7. Übergabe zwischen Plan A und Plan B

Plan A liefert:

- `artifacts/candidates/<timestamp>.jsonl`
- `artifacts/selections/<selection_id>.json`
- einen unveränderlichen Analysebericht mit Ausschlussgründen
- kalibrierte Annahmen für den historischen Copy-Backtest

Plan B liefert:

- `artifacts/observations/latency_<timestamp>.jsonl`
- `artifacts/observations/paper_fills_<timestamp>.jsonl`
- Decoder-Abdeckungsbericht
- Fehlerraten und Quote-zu-Paper-Fill-Abweichungen

Jedes Artefakt enthält mindestens:

- Schema-Version
- Erstellungszeit
- Producer-Komponente und Version
- Git-Commit
- Konfigurations-Hash
- Daten-Snapshot oder Beobachtungszeitraum
- Zeilenzahl beziehungsweise Record Count
- SHA-256 des Inhalts in einer Manifestdatei

Plan B lädt eine Selektion atomar: erst validieren, dann aktivieren. Eine ungültige, abgelaufene oder nicht manuell freigegebene Auswahl darf keine Intents erzeugen.

---

## 8. Parallelisierung ohne spätere Integrationskatastrophe

### Gemeinsamer Startpunkt S0

Beide Claude-Instanzen lesen vollständig:

1. `00_GESAMTPLAN.md`
2. den jeweils eigenen Teilplan
3. alle Dateien unter `contracts/`

Danach werden gemeinsam abgeschlossen:

- Python- und Paketversion
- Repo-Skelett
- JSON-Schemas
- Aggregationsregeln
- zehn bis zwanzig kleine Golden Fixtures
- CI für Schema- und Contract-Tests

Erst danach beginnen A und B unabhängig.

### Stub für Plan B

Plan B wartet nicht auf die echten Sieger-Wallets. Es verwendet eine synthetische `TraderSelection` und aufgezeichnete Golden Events. Sobald Plan A eine echte Selektion liefert, wird nur das Artefakt ausgetauscht, nicht die Architektur.

### Stub für Plan A

Plan A wartet nicht auf echte Live-Latenzen. Es verwendet klar als vorläufig markierte Latenzverteilungen. Sobald Plan B Messdaten liefert, wird derselbe Backtest mit den echten Parametern erneut ausgeführt.

### Verbotene Abkürzungen

- Kein direktes Importieren interner Klassen des anderen Teilplans.
- Keine duplizierten Pydantic-Modelle.
- Keine Interpretation von JSON ohne Schema-Validierung.
- Keine gemeinsame veränderliche Datei, die beide Prozesse gleichzeitig beschreiben.
- Keine Produktionslogik, die ausschließlich mit Mock-Daten getestet wurde.

---

## 9. Sicherheits- und Betriebsmodi

GundiX besitzt explizite Modi:

1. `RESEARCH`: nur historische Daten.
2. `OBSERVE`: Live-Daten erkennen, keine Trade-Intents.
3. `PAPER`: Intents und simulierte Fills, keine Signierung.
4. `SHADOW`: produktionsnahe Quotes und Entscheidungen, weiterhin keine Signierung.
5. `LIVE`: echte Transaktionen, separat freizuschalten.

Der Modus darf niemals aufgrund fehlender Konfiguration auf `LIVE` zurückfallen. Ungültige oder fehlende Werte bedeuten `RESEARCH` beziehungsweise Startabbruch.

Live erfordert gleichzeitig:

- explizite Nutzerauswahl `LIVE`
- gesonderte Live-Konfiguration
- geladene, nicht abgelaufene und freigegebene `TraderSelection`
- bestandenen Preflight
- ausreichendes, aber begrenztes Wallet-Guthaben
- vorhandene Maximalgrenzen pro Trade, Token und Tag
- funktionierenden Kill-Switch
- keine offene Zustandsabweichung

Private Keys erscheinen niemals in Repo, Logs, Artefakten oder Test-Fixtures.

---

## 10. Wissenschaftliche Mindeststandards

- Auswahlfenster und Evaluationsfenster sind zeitlich getrennt.
- Evaluationsdaten werden nicht zur Parameterauswahl verwendet.
- Kandidatenquelle bleibt als Dimension erhalten.
- Ranking-PnL wird nicht ungeprüft übernommen.
- Copy-Ergebnisse verwenden den Preis nach simulierter Verzögerung, nicht den Preis des Traders.
- Alle Kosten, fehlgeschlagenen Fills und illiquiden Exits werden einbezogen.
- Ergebnisse werden mit Konfidenzintervallen und Stichprobengröße berichtet.
- Mehrfachtests werden begrenzt und transparent dokumentiert.
- Ein profitabler Gesamtsaldo genügt nicht; Robustheit über Wallets, Zeitblöcke und Marktphasen ist erforderlich.

Baselines:

- SOL Buy-and-Hold im selben Zeitraum
- zufällige Wallets mit vergleichbarer Aktivität
- blindes Kaufen geeigneter neuer Tokens nach einer festen Regel
- ungefiltertes Kopieren der ursprünglichen Ranglisten-Top-Wallets

---

## 11. Integrations- und Abnahmepunkte

### I1 – Contract Freeze

- Schemas vorhanden und dokumentiert
- Python-Typen identisch
- Schema-Tests grün
- Aggregationsregeln für Router, Transfers und Teilverkäufe beschlossen

### I2 – Parser-Reconciliation

- Historischer Parser A und Live-/Replay-Decoder B erzeugen auf denselben Transaktionen dieselben Netto-`SwapEvent`s.
- Unterschiede werden nicht toleriert oder durch Rundung versteckt.

### I3 – Artefakt-Handshake

- B kann eine von A erzeugte Test-Selektion ohne Sondercode laden.
- Ungültige Versionen, Checksummen und Wallet-Adressen werden abgelehnt.
- Hot Reload ist atomar und behält bei Fehlern die letzte gültige Auswahl.

### I4 – End-to-End Paper

Aufgezeichnete Transaktion -> Decoder -> `SwapEvent` -> Watchlist-Abgleich -> `CopyIntent` -> Paper-Fill -> `ExecutionResult` -> Auswertung durch A.

Der Test prüft Buy, Teilverkauf, vollständigen Exit, Duplikat, verspätetes Event, illiquiden Token und Neustart.

### I5 – Shadow-Kalibrierung

- mindestens mehrere Wochen Beobachtungsdaten
- Parser-Abdeckung und Latenz quantifiziert
- Backtest mit real gemessenen Verteilungen erneut gerechnet
- Paper-Fills und modellierte Fills ausreichend nah beieinander

### I6 – Live-Go/No-Go

Live bleibt gesperrt, solange auch nur eines gilt:

- Out-of-Sample Copy-PnL nach Kosten nicht robust positiv
- Ergebnis hängt am besten Einzeltrade oder den besten 5 %
- relevante Decoder-Lücken
- ungetestete Wiederanlauf- oder Idempotenzlogik
- fehlende Exit-Liquidität im Modell
- keine belegte Paper-/Shadow-Stabilität
- ungeklärte Positionsabweichungen

---

## 12. Anweisung für beide Claude-Instanzen

> Du entwickelst einen Teil eines gemeinsamen GundiX-Systems. Lies `00_GESAMTPLAN.md`, deinen Teilplan und alle Contracts vollständig. Halte dich an Verzeichnisgrenzen und gemeinsame Schemas. Erfinde keine parallelen Datenmodelle. Implementiere in kleinen, testbaren Schritten. Prüfe nach jedem Schritt den realen Produktionspfad, einen positiven Fall und relevante Negativfälle. Behebe Abweichungen sofort, bevor du fortfährst. Behaupte keine Funktions- oder Live-Bereitschaft ohne entsprechende End-to-End-Evidenz. Live-Ausführung bleibt gesperrt, bis die gemeinsamen Go/No-Go-Kriterien nachweislich erfüllt sind.

---

## 13. Aufgabe der abschließenden Integrations-Instanz

Die spätere Integrations-Instanz soll nicht beide Systeme neu schreiben. Sie soll:

1. Commits und offene Änderungen beider Zweige sichern.
2. Contract-Versionen und Lockfile vergleichen.
3. Plan A und Plan B zusammenführen.
4. Migrationen in definierter Reihenfolge anwenden.
5. alle Contract- und Unit-Tests ausführen.
6. I2 bis I4 als echte End-to-End-Tests ausführen.
7. Fehlerursachen an der zuständigen Komponente beheben, nicht mit Adaptern kaschieren.
8. einen reproduzierbaren lokalen Startbefehl dokumentieren.
9. einen Abnahmebericht mit bestandenen, fehlgeschlagenen und noch nicht bewiesenen Punkten erstellen.

Integration bedeutet erst dann Erfolg, wenn eine echte aufgezeichnete Solana-Transaktion durch den gesamten Paper-Pfad läuft und die Ausgabe anschließend wieder von Plan A ausgewertet werden kann.

