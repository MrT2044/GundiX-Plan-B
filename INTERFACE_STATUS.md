# INTERFACE_STATUS – Plan B

Stand: 2026-09-09
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
| B2 Live-Listener | **teilweise, jetzt live belegt** | 178 echte Transaktionen in 4 Minuten OBSERVE gegen das oeffentliche Mainnet-RPC eingesammelt und persistiert; Cursor laeuft, Backoff faengt HTTP 429 ab. **Aber:** der Poller faellt hinter die Kette zurueck (Befund 2). Kein Dauerbetrieb ueber Stunden. WebSocket bewusst nicht gebaut. |
| B3 Decoder | **teilweise** | Gegen echte Mainnet-Transaktionen und gegen das programmeigene Pump.fun-`TradeEvent` verifiziert. Blockiert durch CCR-001. |
| B4 Signal-/Kopierlogik | abgenommen | `tests/paper/test_policy_and_broker.py` – jeder `NO_TRADE`-Grund einzeln |
| B5 Persistenter Zustand | abgenommen | Alembic-Migration, partielle Unique-Indizes, Neustart-Test |
| B6 Paper-Broker | abgenommen | End-to-End mit echter Transaktion, deterministische Landing-Simulation |
| B7 Shadow-Modus | **Gerüst, Provider jetzt verifiziert** | `ShadowBroker` mit Quote-Drift-Messung; der Jupiter-Provider antwortet nachweislich live (Befund 3). Noch kein Shadow-Lauf ueber laengere Zeit. |
| B8 Execution-Adapter | **nur Interface** | Kein Anbieter festgelegt. Die von §B8 verlangte API-Verifikation ist fuer die **Quote**-Seite erbracht, fuer Build/Sign/Submit nicht. |
| B9 Live-Gates | Preflight fertig, Live gesperrt | `assert_live_allowed()` wirft unbedingt |
| B10 Reconciliation | abgenommen | `tests/execution/test_safety.py` |

**193 Tests gruen (plus 7 Integrationstests gegen die echte Jupiter-API). `ruff check`, `ruff format --check` und `mypy --strict` gruen.**

---

## Offene CCRs

| CCR | Betrifft | Wartet auf | Auswirkung, wenn ungelöst |
|---|---|---|---|
| **CCR-001** | S0 §5 Regel 9 (unbekanntes Programm → Quarantäne) | Stellungnahme A + Merge Mensch | **Blockierend.** Unter der Regel wie geschrieben erzeugt B aus 10 aufgezeichneten Mainnet-Transaktionen **0** verwertbare Events. Damit sind B3, I2 und I4 mit echten Daten nicht abnehmbar. |
| CCR-002 | Felder in `CopyIntent` / `ExecutionResult` | Stellungnahme A | `attempt` und die Paper-Annahmen fehlen im geteilten Artefakt; A kann A10 nicht reproduzierbar rechnen. B führt sie bis dahin nur lokal. |
| CCR-003 | `venue`-Enum (Multi-Venue-Routen, 4 fehlende Venues) | Stellungnahme A | Jupiter-Routen über mehrere Venues fallen auf `UNKNOWN` und sind nie verwertbar. Die Coverage-Metrik ist dadurch nicht interpretierbar. |

---

## Live-Befunde vom 2026-09-09

Vier Minuten OBSERVE gegen das oeffentliche Mainnet-RPC, 178 echte Transaktionen. Drei
Dinge kamen dabei heraus, die aus aufgezeichneten Fixtures nicht sichtbar waren.

### 1. Der Fee-Payer ist haeufig nicht der Haendler - **das betrifft dich direkt**

Ich hatte eine Watchlist aus Fee-Payern echter Swap-Transaktionen gebaut. Ergebnis nach
178 Transaktionen: **null** SwapEvents. Nachgemessen an 40 Stichproben:

| | Anzahl |
|---|---|
| Wallet ist Fee-Payer der Transaktion | 40 von 40 |
| Transaktion enthaelt ein Swap-Venue-Programm | 40 von 40 |
| **Tokenbestand der Wallet aendert sich** | **0 von 40** |
| SOL-Sphaerenfluss der Wallet | exakt 0 Lamports |

Die Adresse bezahlt fremde Trades. In denselben 40 Transaktionen stecken **14 Wallets, die
tatsaechlich gehandelt haben** - sichtbar ausschliesslich daran, dass sich ihr Tokenbestand
bewegt.

**Konsequenz fuer A1/A2:** Wenn ihr Kandidaten ueber Fee-Payer oder `accountKeys[0]`
gewinnt, analysiert ihr Relayer statt Haendler. Bei bot-vermittelten Transaktionen - und
das ist auf Pump.fun der Normalfall - ist der Fee-Payer strukturell die falsche Adresse.
Die Antwort darauf ist `wallets_that_traded()` in `src/stream/decoder.py`: Haendler ist,
wem sich der Bestand bewegt. Der Decoder hat korrekt nichts erzeugt; der Fehler lag allein
in der Wallet-Auswahl.

### 2. Der Poller kommt auf dem oeffentlichen RPC nicht hinterher

Blockzeit bis Empfang, 178 echte Beobachtungen:

| Fenster | n | p50 | p90 | min |
|---|---|---|---|---|
| erste Minute | 60 | 50,7 s | 85,9 s | 15,2 s |
| danach | 118 | **182,6 s** | 242,8 s | 95,3 s |

Die Verzoegerung **waechst**, statt sich einzupendeln. Ursache: rund 1 von 3 RPC-Aufrufen
wird mit HTTP 429 abgewiesen, und eine hochfrequente Wallet erzeugt mehr Transaktionen, als
sich unter dieser Drosselung abholen lassen. Der Rueckstand laeuft davon.

**Konsequenz fuer A8:** Deine Latenzszenarien sind 1/3/5/15/60 s. Keines davon deckt ab,
was Plan B auf einem oeffentlichen RPC derzeit erreicht. Entweder kommt ein bezahlter
Provider dazu, oder A8 braucht ein zusaetzliches Szenario in der Groessenordnung von
Minuten - sonst rechnet der Backtest eine Ausfuehrungsqualitaet, die es nicht gibt.

Ich habe daraufhin die Feed-Verzoegerung als Messgroesse verdrahtet und den
`stale_feed`-Circuit-Breaker daran angeschlossen. Der existierte vorher, wurde aber von
nichts gefuettert.

### 3. Die Jupiter-Quote-API war auf einem toten Endpunkt verdrahtet

`quote-api.jup.ag` loest nicht mehr auf. Live sind `lite-api.jup.ag/swap/v1` (ohne Key) und
`api.jup.ag/swap/v1`. Zusaetzlich korrigiert: es gibt **kein 404** fuer ein nicht routbares
Paar, sondern HTTP 400 mit maschinenlesbarem `errorCode` - mein 404-Zweig haette nie
ausgeloest und jeder Fehlschlag waere als generischer Provider-Fehler fehlklassifiziert
worden. Und `priceImpactPct` ist ein **Bruch**, kein Prozentwert: `"0.99"` heisst 99 %.

Verifiziert in `tests/integration/test_jupiter_quotes.py`, 7 Tests gegen die echte API.
Nicht verifiziert und deshalb nicht behauptet: Rate Limits, Nutzungsbedingungen, Eignung
des Keyless-Tiers fuer Dauerbetrieb.

---

## Was Plan A ueber mich wissen muss

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
