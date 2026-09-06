# Plan A – Wallet-Discovery, Filterung und belastbare Selektion

**Verbindliche Grundlage:** `00_GESAMTPLAN.md`  
**Eigener Bereich:** `src/discovery`, `src/research`, `src/backtest` und zugehörige Tests  
**Lieferziel:** Eine versionierte, nachvollziehbare und manuell freigegebene `TraderSelection`, die Plan B direkt laden kann.

---

## 1. Auftrag und klare Nicht-Ziele

Plan A findet Solana-Wallets, rekonstruiert deren tatsächliche Handelsleistung und prüft, ob ihre Trades für GundiX kopierbar gewesen wären.

Plan A:

- sammelt Kandidaten aus mehreren Quellen,
- verarbeitet historische On-Chain-Daten,
- normalisiert Swaps,
- rekonstruiert Positionen,
- entfernt ungeeignete Wallet-Typen,
- bewertet Stabilität und Kopierbarkeit,
- führt zeitlich getrennte Backtests durch,
- liefert eine Watchlist samt Gewichtung und Evidenz.

Plan A:

- sendet keine Orders,
- greift nicht auf private Keys zu,
- wählt keine Wallet allein wegen Followerzahl oder Ranglistenplatz,
- optimiert keine Exit-Strategie in der ersten Projektphase,
- betrachtet keine andere Blockchain als Solana, solange Solana nicht sauber abgeschlossen ist.

---

## 2. Forschungsfrage

Für jede Wallet werden drei verschiedene Fragen getrennt beantwortet:

1. **War die Wallet selbst profitabel?**
2. **War sie wiederholt und robust profitabel?**
3. **Wäre ein späterer Kopierer nach realistischen Kosten ebenfalls profitabel gewesen?**

Nur Frage 3 kann eine Wallet für die finale Selektion qualifizieren.

---

## 3. Kandidatenuniversum

### 3.1 Quellenklassen

Kandidaten werden aus mehreren unabhängigen Klassen als Union gesammelt:

- chainweite Solana-Trader-Ranglisten
- Wallet-PnL- beziehungsweise Smart-Money-Dienste
- Top-Trader einzelner Tokens
- Pump.fun-/Launchpad-Ranglisten
- bekannte KOL-/Callout-Wallets
- organisch aus On-Chain-Aktivität gefundene Wallets

Mögliche Adapter sind beispielsweise Solana Tracker, Vybe, Birdeye oder eine eigene Dune-/On-Chain-Abfrage. Anbieter werden hinter einem `CandidateSource`-Interface gekapselt. Ein Anbieterwechsel darf nachgelagerte Logik nicht verändern.

### 3.2 Warum keine Plattformbeschränkung gilt

Das Untersuchungsobjekt ist die Solana-Wallet. Pump.fun, PumpSwap, Raydium, Meteora, Orca, Jupiter oder Trading-Frontends sind Herkunfts- beziehungsweise Ausführungskontext. Ein Trader darf venueübergreifend handeln.

Andere Chains, beispielsweise BNB Chain, werden nicht in denselben Datensatz gemischt. Eine Wallet-Identität ist chainübergreifend nicht automatisch dieselbe Person, und Ausführungskosten sowie Datenmodelle unterscheiden sich.

### 3.3 Zielgröße und Trichter

Richtwerte, die anhand API-Kosten und Datenqualität angepasst werden dürfen:

1. 1.000–3.000 eindeutige Kandidaten sammeln.
2. Durch billige Vorfilter auf 200–500 Wallets reduzieren.
3. Für 100–200 Wallets vollständige Positions- und Copy-Rekonstruktion rechnen.
4. 30–50 Wallets für Shadow-Beobachtung vorschlagen.
5. Nach Live-Beobachtung voraussichtlich 10–20 robuste Wallets aktiv halten.

Diese Zahlen sind keine Erfolgsbehauptung. Wenn weniger Wallets die Kriterien bestehen, wird die Liste kleiner oder leer.

---

## 4. Phasen und Arbeitspakete

## A0 – Verträge und reproduzierbare Umgebung

### Aufgaben

- Gesamtplan und gemeinsame Schemas lesen.
- Gemeinsames Projekt-Skelett verwenden.
- Datenquellen über Interfaces anbinden.
- Konfiguration von Code trennen.
- Rohdaten, normalisierte Daten und Ergebnisse strikt trennen.
- Datenprovenienz und Snapshot-IDs festlegen.

### Abnahme

- Ein Fixture-`SwapEvent` validiert gegen das gemeinsame Schema.
- Ungültige Wallets, Beträge, Versionen und Zeitstempel werden abgelehnt.
- Derselbe Input erzeugt deterministisch denselben Output und Hash.

## A1 – Kandidatenimport

### Aufgaben

- Pro Quelle einen Adapter implementieren.
- Antworten unverändert mit Abrufzeit und Quellenparametern archivieren.
- Wallet-Adressen validieren und deduplizieren.
- Mehrere Quellen pro Wallet erhalten.
- Rang, Zeitraum, angezeigten PnL und Labels nur als Rohmetadaten speichern.
- Pagination, Rate Limits, Timeouts und Wiederaufnahme unterstützen.
- Quellenausfall darf den gesamten Lauf nicht unbemerkt entwerten.

### Pflichtmetriken

- angefragte und erhaltene Seiten
- eindeutige Wallets pro Quelle
- Deduplikationsrate
- Fehler und Retries
- Alter und Zeitraum der Rangliste
- vollständige beziehungsweise unvollständige Quellen

### Abnahme

- Wiederholung desselben Imports erzeugt keine doppelten Kandidaten.
- Ein abgebrochener Lauf kann fortgesetzt werden.
- Quellenwerte sind nicht mit selbst berechneten Werten vermischt.

## A2 – Historischer Daten-Backfill

### Zeitfenster

Vor Datensichtung festlegen und einfrieren:

- **Discovery-/Trainingsfenster A:** beispielsweise 90 abgeschlossene Tage
- **Out-of-Sample-Fenster B:** anschließende 45 abgeschlossene Tage ohne Überlappung
- optional ein späteres finales Holdout-Fenster C

Zwischen Fenstergrenzen und aktuellem Zeitpunkt muss genügend Abstand für Finalität und vollständige Indexierung liegen.

### Aufgaben

- Historische Transaktionen paginiert je Wallet laden.
- Providerantworten unverändert in einem Rohdaten-Layer speichern.
- Block-/Slot-Grenzen statt unpräziser lokaler Datumsfilter verwenden.
- Erfolgreiche, fehlgeschlagene und unvollständig dekodierbare Transaktionen unterscheiden.
- Provider-Cursor und Fortschritt persistieren.
- Checksummen und Snapshot-Manifeste erzeugen.

### Abnahme

- Keine Lücke oder Pagination-Duplikation in Testfenstern.
- Derselbe Snapshot wird niemals still überschrieben.
- Providerwechsel ist als neuer Snapshot sichtbar.

## A3 – Swap-Normalisierung und Reconciliation

### Aufgaben

- Transaktionen in kanonische `SwapEvent`s umwandeln.
- Inner Instructions und verschachtelte Router-Routen auflösen.
- Wallet-Nettoeffekt berechnen.
- Transfers, LP-Aktionen und reine Accountbewegungen von Swaps trennen.
- Token-2022, Transfer Fees, fehlgeschlagene Transaktionen und unbekannte Venues behandeln.
- unbekannte Fälle mit Rohreferenz in Quarantäne speichern.

### Manuelle Verifikation

Mindestens drei Wallets über mehrere Venues werden transaktionsweise gegen einen unabhängigen Explorer oder zweite Datenquelle geprüft:

- Anzahl
- Richtung
- Token und Quote-Mint
- Rohbeträge
- Gebühren
- Zeit und Slot
- Router-Nettoeffekt

### Abnahme

- Contract Golden Tests grün.
- Historischer Parser A und Replay-Decoder B stimmen überein.
- Unbekannte Quote-/Venue-Fälle werden nicht als korrekte Trades ausgegeben.

## A4 – Positionen und PnL rekonstruieren

### Buchungslogik

- Position pro Wallet und Token/Mint.
- FIFO als anfänglich verbindliche Kostenbasis; Methode im Run speichern.
- Nachkäufe, Teilverkäufe und Restpositionen unterstützen.
- PnL primär in SOL und zusätzlich mit zeitgenauem USD-Referenzwert.
- Netzwerk-, Priority-, Tip-, Venue- und Transfer-Gebühren zuordnen.
- Bestände ohne beobachteten Kauf nicht mit erfundener Kostenbasis bewerten.

### Sonderfälle

- Token-Transfers hinein und hinaus
- Airdrops
- Wallet-interne Umbuchungen
- Wrapped SOL
- Vorbestand vor Fensterbeginn
- offene Position am Fensterende
- geschlossener oder leerer Pool
- Token mit Mint-/Freeze-Risiken
- Wash-Trades im selben Slot oder derselben Transaktion

### Abnahme

- Invariante: Bestand = Käufe + Transfers hinein − Verkäufe − Transfers hinaus, soweit Datenabdeckung reicht.
- Realisierter PnL wird nicht mit unrealisiertem PnL vermischt.
- Unklare Kostenbasis senkt Coverage und wird nicht als Gewinn gewertet.

## A5 – Ausschluss ungeeigneter Wallets

### Harte Ausschlussgruppen

- MEV-/Arbitrage-/Market-Making-Muster
- extrem hohe Transaktionsfrequenz
- Launch-Block-/Same-Slot-Sniper, sofern nicht realistisch kopierbar
- Dev-/Insider-/Pre-Launch-Supply-Verdacht
- Wash-Trading
- zu geringe Datenabdeckung
- zu wenige abgeschlossene Trades oder aktive Tage
- vorwiegend illiquide oder nicht verkäufliche Positionen
- Ergebnis hauptsächlich aus Transfers/Airdrops/unbekannter Kostenbasis

Jeder Ausschluss hat einen maschinenlesbaren Reason Code, Evidenzfelder und Schwellenwertversion.

### Keine voreiligen Schwellenwerte

Grenzen werden zunächst als Vorschlag im Report gezeigt. Bevor sie verbindlich werden, sind Verteilung, Stichprobengröße und erwarteter Effekt zu prüfen. Schwellen werden nur anhand Fenster A festgelegt.

## A6 – Robustheitsmetriken

Mindestens berechnen:

- PnL, ROI und Erwartungswert pro abgeschlossenem Trade
- Median statt nur Mittelwert
- Profit Factor
- Winrate mit Trade-Anzahl
- maximaler Drawdown und Erholungszeit
- profitable Tage/Wochen/Teilfenster
- p10/p50/p90 der Haltedauer
- PnL ohne besten Trade
- PnL ohne beste 1 % und 5 %
- Konzentration auf einzelne Tokens
- Konzentration auf einzelne Marktphasen
- typische und maximale Positionsgröße
- typische Liquidität zum Einstieg und Ausstieg
- Anteil PnL aus Trades unter 5 und 10 Minuten
- durchschnittliche Zeit zwischen Kauf und erstem Teilverkauf

Metriken werden mit Stichprobengröße und, wo sinnvoll, Bootstrap-Konfidenzintervallen berichtet.

## A7 – Reichweite, Callouts und Einfluss

### Zweck

Reichweite wird nicht als Qualitätsscore verwendet, sondern als Hypothese zur Erklärung des Edges und eines möglichen Exit-Liquiditätsrisikos.

### Daten

- belegte Zuordnung Wallet <-> Account mit Quelle und Vertrauensgrad
- historische Followerzahl, sofern verfügbar
- Post-/Callout-Zeitpunkt in UTC
- erwähnter Mint, nicht nur Ticker
- Wallet-Kauf und -Verkauf relativ zum Callout
- Preis, Volumen und Liquidität vor/nach Callout

### Kohorten

- `high_reach`
- `mid_reach`
- `low_reach`
- `unknown`

### Fragen

- Kauft die Wallet systematisch vor öffentlichen Callouts?
- Kommt der Kursimpuls erst nach dem Callout?
- Verkauft die Wallet in den Kaufdruck ihrer Follower?
- Bleibt ein Copy-Entry nach Callout noch profitabel?
- Sind unbekannte Wallets bei gleicher Methodik kopierbarer?

Fehlende oder unsichere Social-Daten dürfen keinen Wallet-PnL erfinden. Diese Analyse bleibt optional und getrennt von der primären On-Chain-Evidenz.

## A8 – Realistischer Copy-Backtest

### Signalzeit

Der Kopierer darf frühestens nach dem Zeitpunkt handeln, an dem Plan B das Event realistisch erkennen und entscheiden könnte. Es werden mindestens Latenzszenarien für 1, 3, 5, 15 und 60 Sekunden getestet. Später werden diese durch Plan-B-Messdaten ersetzt.

### Fill-Modell

Berücksichtigen:

- Preisentwicklung nach Trader-Trade
- Poolliquidität und eigener Preisimpact
- Buy-/Sell-Asymmetrie
- Venue- und Routinggebühren
- Netzwerkgebühr, Priority Fee und Tip
- Slippagegrenze
- Quote-Ablauf
- Fail-Rate
- Token-Restriktionen
- fehlende Exit-Liquidität

Ein ausgelöster Exit ist noch kein Fill. Wenn zum angenommenen Zeitpunkt kein realistischer Sell möglich ist, wird der Verlust nicht durch den letzten angezeigten Preis beschönigt.

### Portfolio-Simulation

Explizit festlegen:

- Startkapital
- maximale Position pro Token
- maximale Gesamtexponierung
- gleichzeitige Signale
- mehrere Wallets im selben Token
- doppelte Events
- Teilverkäufe
- proportionaler versus fester Einsatz
- nicht ausreichendes freies Kapital
- Tagesgrenzen

Die Baseline in Phase 1 kopiert den Trader-Exit proportional. Alternative Exit-Optimierung wird erst nach Auswahl der kopierbaren Entry-Signale gestartet.

## A9 – Selektion ohne Look-ahead

### Verfahren

1. Filter und Score nur mit Fenster A entwickeln.
2. Score einfrieren und hashen.
3. Wallets anhand A auswählen.
4. Auswahl unverändert auf Fenster B testen.
5. B nur einmal als primäres Out-of-Sample-Ergebnis auswerten.
6. Parameter nach Sichtung von B nicht still nachoptimieren.

### Mindestanforderungen an einen Kandidaten

Konkrete Grenzwerte werden anhand der Daten vorab festgelegt. Kategorien:

- ausreichende Datenabdeckung
- ausreichende Zahl abgeschlossener Trades und aktiver Tage
- mehrere profitable Teilfenster
- akzeptabler Drawdown
- positive Copy-Erwartung nach Kosten
- positive oder zumindest robuste Leistung ohne Ausreißer
- Haltedauer kompatibel mit gemessener Latenz
- ausreichend liquide Ein- und Ausstiege

### Ausgabe

Die `TraderSelection` enthält nicht nur Gewinner, sondern pro Wallet:

- Score und Rang
- relevante Kennzahlen
- Copy-PnL nach Latenzszenario
- Gewichtsobergrenze
- Reason Codes
- bekannte Risiken
- Daten-Coverage
- Gültigkeitszeitraum

Eine menschliche Freigabe ist notwendig. Automatisches wöchentliches Nachziehen der neuesten Gewinner ist verboten.

## A10 – Rückkopplung aus Plan B

Nach ersten Live-Beobachtungen liest Plan A:

- echte Erkennungs- und Entscheidungslatenzen
- Quote-/Fill-Abweichungen
- verpasste und unbekannte Trades
- Paper-Failures
- tatsächliche Signalhäufigkeit

Danach wird derselbe eingefrorene Backtest erneut mit realen Verteilungen ausgeführt. Verschlechterungen werden berichtet, nicht wegoptimiert.

---

## 5. Tests

### Unit-Tests

- Cursor/Pagination und Deduplikation
- Decimal-/Raw-Amount-Arithmetik
- FIFO mit Nachkauf und Teilverkauf
- Fenstergrenzen
- Score-Determinismus
- Reason Codes
- Portfolio-Kapitalbindung

### Property-Tests

- kein negativer Tokenbestand ohne markierte Datenlücke
- keine doppelte Signatur-/Event-Verarbeitung
- Summe der Teilverkäufe überschreitet nicht kopierbaren Bestand
- zufällige Reihenfolge identischer Daten verändert Ergebnis nicht
- Zukunftsdaten verändern Auswahlfenster-A-Score nicht

### Integrations-Tests

- API-Fixture -> Kandidat -> Rohtransaktion -> Swap -> Position -> Stats
- echter kleiner Snapshot mit Wiederanlauf
- Export der `TraderSelection` und Import durch Contract-Test von B

### Negative Tests

- Rate Limit und Provider-Ausfall
- abgeschnittene Pagination
- falsche Decimals
- Transfer als vermeintlicher Sell
- Jupiter-Multi-Hop
- Airdrop ohne Entry
- offener Bestand vor Fensterstart
- Rug ohne Exit-Liquidität
- manipulierte oder inkompatible Artefaktversion

---

## 6. Berichte und Ehrlichkeitsregeln

Jeder Run erzeugt:

- Parameter und Schwellenwerte
- Code-Commit und Daten-Snapshot
- Provider und Datenabdeckung
- Funnel von Kandidaten bis Auswahl
- Ausschlusszahlen pro Grund
- In-Sample- und Out-of-Sample-Ergebnisse klar getrennt
- Baseline-Vergleich
- Latenz-Sweep
- Sensitivitätsanalyse
- unbekannte beziehungsweise nicht bewiesene Punkte

Verbotene Aussagen ohne Evidenz:

- „beste Wallets“
- „konstant profitabel“
- „kopierbar“
- „live-ready“

Stattdessen werden Zeitraum, Stichprobe, Kostenannahmen und Unsicherheit genannt.

---

## 7. Definition of Done für Plan A

Plan A ist fertig, wenn:

- mehrere Kandidatenquellen reproduzierbar importiert wurden,
- historische Daten mit dokumentierter Coverage vorliegen,
- Parser gegen unabhängige Stichproben und B-Replay reconciled ist,
- Positionen und PnL ohne bekannte Bilanzfehler rekonstruiert werden,
- ungeeignete Wallettypen mit Reason Codes herausgefiltert werden,
- ein eingefrorener Score auf einem unberührten Zeitraum getestet wurde,
- realistische Copy-Fills statt Trader-Preise verwendet wurden,
- Baselines und Robustheit ausgewertet sind,
- die Selection-Datei dem gemeinsamen Schema entspricht,
- Plan B sie unverändert laden kann,
- alle Tests und der End-to-End-Paper-Handshake grün sind.

Eine leere Selektion ist ein zulässiges Ergebnis. Das System darf schlechte Evidenz nicht durch weichere Filter in ein positives Resultat verwandeln.

---

## 8. Arbeitsanweisung für Claude A

> Lies zuerst `00_GESAMTPLAN.md`, danach diesen Plan und alle gemeinsamen Contracts. Arbeite ausschließlich in deinem Eigentumsbereich, außer eine gemeinsame Contract-Änderung wurde ausdrücklich abgestimmt. Implementiere A0 bis A10 in Reihenfolge. Nach jedem Arbeitspaket prüfst du den realen Datenpfad, positive und negative Fälle sowie die Kompatibilität zu den Contracts. Behebe Fehler sofort. Verwende Ranglisten nur zur Kandidatengenerierung und rekonstruiere die Leistung selbst. Melde Datenlücken und Unsicherheit offen. Sende niemals Orders und greife niemals auf Wallet-Schlüssel zu.

