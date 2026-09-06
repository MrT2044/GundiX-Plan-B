# INTERFACE_STATUS – Plan B

Stand: 2026-09-06
Contracts: lokal unter `contracts/`, noch **kein** eigenes Repo und kein Tag (S0 §14 Punkt 2 offen)
Modus dieses Builds: `PAPER` / `SHADOW` möglich, `LIVE` hart gesperrt

---

## Ich produziere

| Artefakt | Schema | Version | Status |
|---|---|---|---|
| `paper_fills_<ts>.jsonl` | `execution_result` | 1.0.0 | **LAUFFÄHIG** – aus Replay echter aufgezeichneter Transaktionen. Noch keine Live-Beobachtung. |
| `latency_<ts>.jsonl` | `latency_observation` | 1.0.0 | **LAUFFÄHIG**, aber die Werte stammen aus Replay (`source = REPLAY`). Für A10 unbrauchbar, bis OBSERVE gegen echtes RPC lief. |
| `decoder_coverage_<ts>.json` | `decoder_coverage` | 1.0.0 | **LAUFFÄHIG**. Enthält die Liste unbekannter Programme – das ist die Arbeitsliste für den Decoder. |
| `artifacts/selections/synthetic_dev.json` | `trader_selection` | 1.0.0 | **STUB, von B erzeugt.** Wallets stammen aus aufgezeichneten Transaktionen, nicht aus Analyse. Wird durch A ersetzt. |

`latency_observation` und `decoder_coverage` sind in S0 §4 nicht spezifiziert; §7.1 verlangt
die Dateien aber. Plan B hat die Schemas definiert und die Stufennamen unter `latencies_ms`
exakt aus §4.6 übernommen, damit sich beide Artefakte ohne Übersetzungstabelle joinen lassen.

## Ich konsumiere

| Artefakt | Schema | Version | Status |
|---|---|---|---|
| `artifacts/selections/<selection_id>.json` | `trader_selection` | 1.0.0 | **ERWARTET** – noch nichts von A erhalten. Der Ladepfad ist fertig und getestet, inklusive aller acht Ablehnungsregeln aus S0 §4.4. |

**Für Plan A wichtig:** Sobald ihr eine echte Selection liefert, ist der Integrationsschritt
das Ersetzen einer Datei – kein Code. Voraussetzung: Manifest daneben
(`<datei>.manifest.json`), `manual_approval.approved = true`, `expires_at_utc` in der
Zukunft, Summe der Gewichte ≤ 1. Alles andere lehnt B fail-closed ab und behält die zuvor
aktive Auswahl.

---

## Fertige Arbeitspakete

| Paket | Status | Nachweis |
|---|---|---|
| B0 Contracts und Skelett | abgenommen | `tests/contracts/` – 49 Tests, Modell/Schema-Feldgleichheit in beide Richtungen |
| B1 Selection-Import | abgenommen | `tests/paper/test_selection.py` – alle acht Ablehnungsregeln, atomarer Reload, letzte gültige Auswahl bleibt |
| B2 Live-Listener | **teilweise** | Replay- und RPC-Polling-Quelle implementiert; Cursor und Gap-Erkennung getestet. **Nicht** gegen echtes RPC im Dauerbetrieb verifiziert. WebSocket bewusst nicht gebaut (siehe unten). |
| B3 Decoder | **teilweise** | Gegen echte Mainnet-Transaktionen und gegen das programmeigene Pump.fun-`TradeEvent` verifiziert. Blockiert durch CCR-001. |
| B4 Signal-/Kopierlogik | abgenommen | `tests/paper/test_policy_and_broker.py` – jeder `NO_TRADE`-Grund einzeln |
| B5 Persistenter Zustand | abgenommen | Alembic-Migration, partielle Unique-Indizes, Neustart-Test |
| B6 Paper-Broker | abgenommen | End-to-End mit echter Transaktion, deterministische Landing-Simulation |
| B7 Shadow-Modus | **Gerüst** | `ShadowBroker` inklusive Quote-Drift-Messung vorhanden, aber ohne verifizierten echten Quote-Provider wertlos |
| B8 Execution-Adapter | **nur Interface** | Kein Anbieter festgelegt – §B8 verlangt Verifikation der offiziellen API zuerst |
| B9 Live-Gates | Preflight fertig, Live gesperrt | `assert_live_allowed()` wirft unbedingt |
| B10 Reconciliation | abgenommen | `tests/execution/test_safety.py` |

**186 Tests grün. `ruff check`, `ruff format --check` und `mypy --strict` grün.**

---

## Offene CCRs

| CCR | Betrifft | Wartet auf | Auswirkung, wenn ungelöst |
|---|---|---|---|
| **CCR-001** | S0 §5 Regel 9 (unbekanntes Programm → Quarantäne) | Stellungnahme A + Merge Mensch | **Blockierend.** Unter der Regel wie geschrieben erzeugt B aus 10 echten Mainnet-Transaktionen **0** verwertbare Events. Damit sind B3, I2 und I4 mit echten Daten nicht abnehmbar. |
| CCR-002 | Felder in `CopyIntent` / `ExecutionResult` | Stellungnahme A | `attempt` und die Paper-Annahmen fehlen im geteilten Artefakt; A kann A10 nicht reproduzierbar rechnen. B führt sie bis dahin nur lokal. |
| CCR-003 | `venue`-Enum (Multi-Venue-Routen, 4 fehlende Venues) | Stellungnahme A | Jupiter-Routen über mehrere Venues fallen auf `UNKNOWN` und sind nie verwertbar. Die Coverage-Metrik ist dadurch nicht interpretierbar. |

---

## Was Plan A über mich wissen muss

1. **Die Selection unter `artifacts/selections/synthetic_dev.json` ist synthetisch.** Sie
   trägt `risks: ["NOT_A_RESEARCH_RESULT"]`. Sie existiert nur, damit der Pfad läuft.

2. **CCR-001 ist der Engpass.** Ich habe Regel 9 wörtlich implementiert (Default) und die
   Alternative als explizit einzuschaltende, geloggte Abweichung danebengestellt. Die
   Messung steht in `CCR/CCR-001-...md`. Bitte prüft, ob ihr die vorgeschlagene
   Bilanz-Rekonziliation im historischen Parser identisch umsetzen könnt – sonst bricht I2
   genau auf den Transaktionen, die der Vorschlag zusätzlich zulässt.

3. **Die SOL-Seite eines Swaps kommt aus Sphären-Buchhaltung, nicht aus Instruktionen.**
   Das ist keine Bequemlichkeit: eine Pump.fun-Bonding-Curve verschiebt Lamports per
   `sub_lamports` direkt im Programm, dazu existiert keine Instruktion. Die Regel steht in
   `contracts/AGGREGATION.md` §2a. Gegen das programmeigene `TradeEvent` stimmt das Ergebnis
   auf das Lamport überein. **Wenn ihr die SOL-Seite anders berechnet, weichen unsere
   Beträge systematisch um die Venue-Gebühr ab** (bei Pump.fun 1,25 %).

4. **`instruction_path` ist bei uns immer der Index der äußersten Top-Level-Instruktion**
   (S0 §4.1.2), also `"5"`, nie `"5.2"`. Bei abweichender Wahl unterscheiden sich alle
   `event_id`s.

5. **Golden Cases liegen unter `contracts/golden/<case_id>/`** im S0-§6-Layout, erzeugt aus
   echten Mainnet-Transaktionen. `expected_events.json` ist mit den **vertragskonformen**
   Decoder-Einstellungen erzeugt, nie mit einer CCR-Variante. Von den 18 Pflichtfällen sind
   7 belegt; die fehlenden brauchen Transaktionen, die ich nicht gezielt finden konnte
   (siehe unten).

6. **Fehlgeschlagene Transaktionen erzeugen bei mir gar kein `SwapEvent`.** S0 §4.1.7 lässt
   `success = false` in der Rohebene zu; da eine fehlgeschlagene Transaktion aber keine
   Bilanzänderung hat, wäre jeder Betrag erfunden. Sie werden als
   `transactions_failed_onchain` gezählt und die Rohtransaktion bleibt gespeichert. Falls
   ihr `success=false`-Events erwartet, ist das ein CCR.

---

## Was ich nicht behaupte

- **Kein Live-Betrieb.** Kein Signer, kein Execution-Adapter, keine Go/No-Go-Evidenz.
  `assert_live_allowed()` wirft bedingungslos.
- **Keine gemessene Live-Latenz.** Alle bisherigen Latenzwerte stammen aus Replay und sind
  als `source = REPLAY` markiert. Sie sind für A10 **nicht** verwendbar.
- **Kein verifizierter Quote-Provider.** Der Jupiter-Adapter ist geschrieben, aber nie gegen
  die Live-API gelaufen. In PAPER läuft ein klar als Modell gekennzeichneter simulierter
  Provider.
- **Kein WebSocket-Listener.** `logsSubscribe` ist auf dem öffentlichen Endpunkt nicht
  nutzbar; der Code wäre nur gegen einen Mock testbar, und ein bestandener Mock-Test ist
  laut S0 §9.3 kein Nachweis. `stream.source=websocket` wirft mit dieser Begründung.
- **Kein Dauerbetrieb.** Der RPC-Polling-Pfad ist implementiert und einzeln getestet, aber
  nie über Stunden gegen das echte Mainnet gelaufen.
- **Fehlende Golden-Pflichtfälle:** 3 (Raydium CPMM), 7 (zwei beobachtete Wallets), 8
  (Token-2022 Transfer Fee), 9 (Wrap + Swap), 11 (reiner SPL-Transfer), 12 (LP Add), 13
  (Airdrop), 14 (Migration), 15 (unbekanntes Programm), 17 (Teilverkauf 30 %), 18
  (Wash-Trade). Die Fälle 11, 12 und 17 kann Plan A aus dem Backfill gezielt liefern –
  einzelne passende Transaktionen aus dem laufenden Mainnet-Strom zu fischen ist deutlich
  aufwendiger als sie in einem Backfill zu suchen.
