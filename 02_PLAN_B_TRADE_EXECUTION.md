# Plan B – Live-Beobachtung, Paper-Trading und spätere Trade-Ausführung

**Verbindliche Grundlage:** `00_GESAMTPLAN.md`  
**Eigener Bereich:** `src/stream`, `src/paper`, `src/execution` und zugehörige Tests  
**Lieferziel:** Ein persistenter, idempotenter und fail-closed Ausführungspfad, der eine Plan-A-Selection direkt konsumiert und zunächst ausschließlich Paper-/Shadow-Ergebnisse erzeugt.

---

## 1. Auftrag und klare Nicht-Ziele

Plan B baut die Infrastruktur, die freigegebene Wallets beobachtet, deren echte Solana-Swaps normalisiert, daraus Copy-Intents erzeugt und diese kontrolliert ausführt.

Plan B umfasst:

- Live-Listener und Lückenwiederherstellung
- Live-Decoder in das gemeinsame `SwapEvent`-Format
- Selection-Lader
- Signalnormalisierung und Deduplikation
- Positions- und Risikozustand
- Paper-Broker
- Shadow-Modus mit realen Quotes
- austauschbare Quote-/Execution-Adapter
- spätere, explizit gesperrte Live-Signierung
- Telemetrie, Recovery und Reconciliation

Plan B:

- erfindet keinen eigenen Wallet-Score,
- zieht nicht automatisch die aktuellen Ranglistengewinner nach,
- entscheidet nicht, dass Backtest-Evidenz ausreichend ist,
- startet nicht im Live-Modus,
- sendet ohne explizite Go-Freigabe keine Transaktion.

---

## 2. Designprinzipien

1. **Selection ist Input:** Plan B konsumiert `TraderSelection` von Plan A.
2. **Event zuerst persistieren:** Ein erkanntes Event wird dauerhaft gespeichert, bevor daraus eine Aktion entsteht.
3. **Idempotenz:** Replay, Reconnect oder Neustart darf niemals einen Doppeltrade erzeugen.
4. **Fail-closed:** Unklare Daten, veraltete Quotes, unbekannte Tokens oder inkonsistenter State blockieren die Aktion.
5. **Adapter statt Anbieterbindung:** Datenquelle, Quote-Provider und Ausführungsweg sind austauschbar.
6. **Paper-first:** Derselbe Entscheidungs- und Risikopfad wird vor Live im Paper-/Shadow-Modus verwendet.
7. **Beobachtung und Entscheidung trennen:** Ein erkannter Trade ist nicht automatisch ein erlaubter Trade.

---

## 3. Komponenten

### `SelectionRepository`

- lädt eine versionierte `TraderSelection`
- validiert Schema, Checksumme, Freigabe und Ablaufdatum
- baut einen schnellen Wallet-Index
- aktiviert Updates atomar
- protokolliert aktive Selection-ID
- behält bei fehlerhaftem Update die letzte gültige Auswahl

### `ChainEventSource`

- empfängt Solana-Signaturen/Transaktionen
- liefert Slots und Empfangszeit
- erkennt Reconnects und Lücken
- kann fehlende Bereiche per RPC nachladen

Implementierungen können Webhooks, Standard-WebSockets oder später einen anderen Dienst verwenden. Der Domain-Code darf nicht von einem konkreten Anbieter abhängen.

### `SwapDecoder`

- dekodiert reale Transaktionen
- erzeugt ausschließlich kanonische `SwapEvent`s
- aggregiert Router-Hops nach gemeinsamer Regel
- unterscheidet Swap und Transfer
- protokolliert unbekannte Programme

### `SignalPolicy`

- prüft Quell-Wallet und aktive Selection
- verhindert doppelte Signale
- prüft Signalalter, Token, Liquidität und Position
- behandelt mehrere Wallets im selben Token
- erzeugt `CopyIntent` oder explizites `NO_TRADE`

### `RiskEngine`

- maximale Größe pro Trade
- maximale Position pro Token
- maximale Tagesexponierung und Tagesverlust
- maximale parallele Positionen
- Mindestreserve für Fees
- Token-/Mint-Risikoprüfungen
- Stale-Data- und Circuit-Breaker-Regeln
- Kill-Switch

### `QuoteProvider`

- liefert Route, erwarteten Output, Preisimpact, Fees und Quote-Ablauf
- erlaubt mehrere Implementierungen
- speichert Request und relevante Response-Felder für Audit
- eine Quote ist keine Fill-Garantie

### `Broker`

Gemeinsames Interface:

- `PaperBroker`
- `ShadowBroker`
- später `LiveBroker`

Alle liefern dasselbe `ExecutionResult`-Schema.

### `PositionRepository`

- persistiert Soll- und Istpositionen
- verarbeitet Nachkäufe und Teilverkäufe
- trennt Positionen nach Quell-Wallet, bietet aber aggregierte Tokenlimits
- ist transaktional und neustartsicher

---

## 4. Phasen und Arbeitspakete

## B0 – Gemeinsame Contracts und Skelett

### Aufgaben

- Gesamtplan, eigenen Plan und Contracts lesen.
- Gemeinsamen Python-Stack verwenden.
- Interfaces und Dependency Injection definieren.
- Keine echten Keys oder Live-Endpunkte für Tests benötigen.
- synthetische `TraderSelection` und Golden Events vorbereiten.

### Abnahme

- Test-Selection wird geladen.
- unbekannte Major-Version wird abgelehnt.
- ein Golden Event erreicht im `PAPER`-Modus den Broker.
- derselbe Event-Replay erzeugt kein zweites Ergebnis.

## B1 – Selection-Import

### Aufgaben

- Manifest und Inhalts-Checksumme prüfen.
- Wallet-Adressen validieren.
- manuelle Freigabe und Ablaufdatum verlangen.
- Wallet-Gewichte und Grenzen laden.
- aktive Version in jedem Intent speichern.
- atomaren Reload implementieren.

### Negative Fälle

- Datei teilweise geschrieben
- falsche Checksumme
- leere Auswahl
- doppeltes Wallet
- abgelaufene Auswahl
- fehlende Freigabe
- inkompatible Schema-Version

Eine leere Auswahl ist gültig für Beobachtung, erzeugt aber keine Copy-Intents.

## B2 – Live-Listener

### Anfangsumfang

Start mit einem Provider und einer kleinen Test-Watchlist. Die Zielpopulation hält Stunden bis Tage; eine robuste Sekundenlatenz ist wichtiger als voreilige Subsekunden-Infrastruktur.

### Aufgaben

- Verbindung, Authentifizierung und Subscription
- Heartbeat und Health State
- Reconnect mit begrenztem Exponential Backoff und Jitter
- Slot-basierte Lückenerkennung
- Nachladen verpasster Transaktionen
- persistenter Empfangscursor
- Rohereignis vor Verarbeitung speichern
- Backpressure und begrenzte Queues
- sauberes Herunterfahren

### Messungen

- Blockzeit zu Empfang
- Empfang zu Decode
- Decode zu Entscheidung
- Entscheidung zu Quote
- Quote zu simuliertem beziehungsweise echtem Submit
- p50/p90/p99 und Ausreißer
- Reconnects, Lücken, Nachladungen und Fehler

### Abnahme

- Neustart verliert keine bestätigten, bereits gespeicherten Events.
- Replay erzeugt keine Doppelverarbeitung.
- künstliche Verbindungslücke wird erkannt und aufgefüllt.

## B3 – Live-Decoder

### Priorität

Nicht nach Frontends, sondern nach tatsächlichen On-Chain-Programmen arbeiten. Anfangs werden die in der Teststichprobe häufigsten Pfade unterstützt, typischerweise:

- Pump.fun/PumpSwap
- Raydium LaunchLab/CPMM
- Routerpfade, insbesondere Jupiter

Meteora und weitere Venues werden erst nach Golden Tests oder gemessener Relevanz ergänzt. Unbekannte Venues bleiben sichtbar.

### Fallstricke

- Inner Instructions
- mehrere Swaps in einer Transaktion
- Multi-Hop-Routing
- Buy und Sell in derselben Transaktion
- Token-2022 und Transfer Fees
- Migration von Bonding Curve zu Pool
- fehlgeschlagene Transaktionen
- mehrere relevante Wallets in derselben Transaktion
- reine Transfers

### Abnahme

- Decoder stimmt mit Plan A auf Golden Transaktionen überein.
- unbekannte Instruktionen erhöhen eine Coverage-Metrik und gehen in Quarantäne.
- keine Tradeentscheidung auf Basis nur teilweise dekodierter Nettoeffekte.

## B4 – Signal- und Kopierlogik

### Grundregel

Ein `SwapEvent` erzeugt nur dann einen `CopyIntent`, wenn alle Bedingungen erfüllt sind. Andernfalls wird ein `NO_TRADE` mit maschinenlesbarem Grund gespeichert.

### Buy-Verhalten

Konfigurierbar, aber versioniert:

- feste kleine Copy-Größe oder gewichtete Größe
- niemals blind dieselbe absolute Trader-Größe
- Mindestliquidität
- maximaler Preisimpact
- maximales Signalalter
- kein zweiter Buy bei bereits ausgeschöpftem Tokenlimit
- Regeln für mehrere Wallets, die denselben Token kaufen

### Sell-Verhalten in Phase 1

- Trader verkauft X % seines nachvollziehbaren Bestands -> GundiX verkauft X % des zugeordneten Copy-Bestands.
- Verkauf kann nie mehr als tatsächlich vorhandenen Copy-Bestand betragen.
- Transfer des Traders ist kein Sell.
- Ein Sell ohne zugeordneten Copy-Bestand wird protokolliert, nicht ausgeführt.
- Bei nicht verkäufbarem Token bleibt der Fehler sichtbar; kein fiktiver Fill.

### Deduplikation mehrerer Wallets

Wenn mehrere beobachtete Wallets denselben Token kaufen, muss eine explizite Policy gelten:

- `FIRST_SIGNAL_ONLY`
- `CONSENSUS_REQUIRED`
- `INCREMENT_WITH_CAP`

Die Policy und ihre Version werden im Intent gespeichert. Phase 1 sollte konservativ mit `FIRST_SIGNAL_ONLY` beginnen und Konsens später separat testen.

## B5 – Persistenter Zustand

### Tabellen beziehungsweise Aggregate

- raw chain events
- normalized swap events
- signal decisions
- copy intents
- quotes
- execution attempts
- execution results
- source-wallet positions
- aggregate token exposure
- daily risk ledger
- active selection
- service checkpoints

### Invarianten

- eine Event-ID -> höchstens ein aktiver Intent
- eine Intent-ID -> höchstens ein wirtschaftlich wirksamer Fill
- verkaufte Copy-Menge <= vorhandene Copy-Menge
- Exposure entspricht den bestätigten Ergebnissen, nicht gesendeten Versuchen
- Tageslimits bleiben über Neustarts erhalten

## B6 – Paper-Broker

### Zweck

Der Paper-Broker testet die gesamte Produktionsentscheidung ohne Signatur oder Broadcast.

### Verhalten

- Quote zum realistischen Entscheidungszeitpunkt anfragen
- Quote-Ablauf und Slippage berücksichtigen
- simulierte Landing-/Fail-Wahrscheinlichkeit anwenden
- Gebühren abziehen
- Buy und Sell unterschiedlich behandeln
- bei keiner Route oder unzureichendem Output einen fehlgeschlagenen Fill schreiben
- Position ausschließlich nach erfolgreichem simulierten Fill aktualisieren

### Abnahme

- Plan A kann `paper_fills.jsonl` direkt einlesen.
- Quote, Annahmen und Ergebnis sind vollständig auditierbar.
- Neustart während eines Intents erzeugt weder Verlust noch Doppel-Fill.

## B7 – Shadow-Modus

Shadow verwendet aktuelle produktionsnahe Daten, baut bei Bedarf Transaktionsentwürfe, signiert oder sendet aber nichts.

### Zu erfassen

- reale Quote und Routen
- Preisimpact und Fees
- Simulationsergebnis, sofern sicher ohne Signatur möglich
- Latenzverteilung
- Quote-Drift bei erneuter Abfrage
- fehlende Routen und Tokenrestriktionen
- erwartete versus nachträglich beobachtbare Ausführungsqualität

Shadow-Daten werden an Plan A zur Kalibrierung zurückgegeben.

## B8 – Execution-Adapter für späteres Live

### Anbieterneutralität

Ob später Jupiter, ein Pump-spezifischer Dienst oder direkter Transaktionsbau verwendet wird, entscheidet ein Adapter. Vor Implementierung muss die aktuelle offizielle API, Kostenstruktur, Authentifizierung und Nutzungsbedingung verifiziert werden.

Interface-Fähigkeiten:

- Quote
- Build
- lokale Validierung
- Simulation
- Signierung über getrennten Signer
- Submit
- Confirmation/Finality
- Status-Reconciliation

Plan B darf sich nicht auf einen nicht verifizierten Namen wie „Pumpan“ festlegen. Zuerst wird geklärt, welcher Dienst gemeint ist und ob er für die benötigten Solana-Venues geeignet ist.

### Signer-Isolation

- Schlüssel außerhalb des Repos
- keine Secrets in Exceptions oder strukturierten Logs
- getrennte, begrenzt finanzierte Wallet
- minimaler Prozesszugriff
- Signer nicht für Research- und Paper-Modus laden

## B9 – Live-Sicherheitsgates

### Preflight vor jedem Start

- Modus explizit `LIVE`
- Selection gültig und freigegeben
- Chain und RPC korrekt
- Wallet-Adresse entspricht erwarteter Live-Wallet
- Limits geladen und innerhalb absoluter Code-Obergrenzen
- Kill-Switch nicht aktiv
- Systemzeit plausibel
- DB-Migration aktuell
- keine offenen inkonsistenten Intents
- RPC und Quote-Provider erreichbar

### Vor jedem Intent

- Event frisch und final genug
- Wallet noch in aktiver Selection
- Quote frisch
- Mindestoutput festgelegt
- Limits und Liquidität ausreichend
- Token nicht blockiert
- aktuelle Position reconciled

### Nach Submit

- Status bis zu definierter Terminalbedingung verfolgen
- Timeout nicht automatisch als Fehlschlag und erneute Order behandeln
- erst Chain-Status reconciliieren, dann Retry erwägen
- partielle beziehungsweise unklare Ergebnisse blockieren Folgeaktionen

### Kill-Switch und Circuit Breaker

Auslöser mindestens:

- manuelle Kill-Switch-Datei oder UI-Aktion
- Tagesverlustlimit
- zu viele aufeinanderfolgende Fehler
- ungewöhnliche Slippage
- Datenfeed veraltet
- Decoder-Coverage bricht ein
- State-/Chain-Abweichung
- RPC- oder Quote-Provider-Divergenz

## B10 – Reconciliation und Recovery

Beim Start und regelmäßig:

- lokale Signaturen gegen Chain-Status prüfen
- Wallet-Balances mit lokalem Positionszustand vergleichen
- unbekannte externe Wallet-Aktivität erkennen
- hängende Intents klassifizieren
- Abweichungen in einen sicheren Terminal-/Review-Zustand setzen

Automatische Reparatur ist nur erlaubt, wenn sie deterministisch und getestet ist. Unklare Abweichungen sperren neue Trades und bewahren alle Diagnosedaten.

---

## 5. `NO_TRADE`-Gründe

Mindestens:

- `wallet_not_selected`
- `selection_invalid`
- `selection_expired`
- `duplicate_event`
- `event_too_old`
- `event_not_final`
- `decode_incomplete`
- `unknown_venue`
- `transfer_not_swap`
- `unsupported_token`
- `no_route`
- `quote_stale`
- `slippage_too_high`
- `liquidity_too_low`
- `position_limit`
- `daily_limit`
- `insufficient_balance`
- `kill_switch_active`
- `state_mismatch`
- `no_copy_position_to_sell`
- `mode_disallows_execution`

Ein verworfener Trade bleibt dadurch erklärbar und statistisch auswertbar.

---

## 6. Tests

### Unit-Tests

- Selection-Validierung und atomarer Reload
- Event-/Intent-Idempotenz
- Buy-/Sell-Sizing
- Teilverkäufe
- Tages- und Positionslimits
- Quote-Ablauf
- Zustandsübergänge
- `NO_TRADE`-Gründe

### Integrations-Tests

- aufgezeichnete Transaktion -> Swap -> Intent -> Paper-Fill
- Selection von Plan A ohne Transformationsskript laden
- WebSocket-Abbruch mit Gap Recovery
- DB-Neustart zwischen Intent und Ergebnis
- Quote-Provider Timeout
- Confirmation nach lokalem Timeout

### Negative und Chaos-Fälle

- doppeltes Webhook-Event
- Events außer Reihenfolge
- falscher Slot beziehungsweise veraltetes Event
- unbekannte Instruktion
- Multi-Hop-Doppelzählung
- Transfer als Sell
- Rug/kein Sell-Quote
- RPC meldet uneindeutigen Status
- Prozessabbruch unmittelbar vor/nach Submit
- beschädigte Selection-Datei
- volles Laufwerk oder DB-Lock
- Systemzeitabweichung

### Sicherheits-Tests

- Paper-/Shadow-Modus kann technisch keinen Signer laden
- Logs und Fehler enthalten keine Secrets
- fehlende Modusvariable aktiviert niemals Live
- Kill-Switch blockiert vor Build/Sign/Submit
- absolute Obergrenzen können nicht durch Konfiguration überschritten werden

---

## 7. Telemetrie und Betrieb

Metriken:

- Feed-Alter
- letzter verarbeiteter Slot
- Eventrate
- Decoder-Coverage
- unbekannte Programme
- Deduplikate
- Queue-Tiefe
- p50/p90/p99 je Latenzstufe
- Intents und `NO_TRADE` nach Grund
- Quote- und Fill-Failrate
- offene Positionen und Exposure
- Reconciliation-Abweichungen
- aktiver Modus und Selection-ID

Logs sind strukturiert, enthalten Korrelations-IDs und keine privaten Schlüssel oder vollständigen geheimen Providerwerte.

Ein Healthcheck unterscheidet mindestens:

- `healthy`
- `degraded_observation`
- `execution_blocked`
- `fatal`

---

## 8. Definition of Done für Plan B

### Paper-/Shadow-fertig

- Selection von Plan A wird unverändert und atomar geladen.
- Listener ist reconnect- und neustartsicher.
- Decoder reconciled mit Plan A.
- jeder Eventpfad ist idempotent.
- Positionen und Limits sind persistent.
- Paper- und Shadow-Ergebnisse entsprechen gemeinsamen Schemas.
- Plan A kann die Beobachtungsartefakte direkt auswerten.
- positive, negative und Recovery-End-to-End-Tests sind grün.

### Live-fertig

Zusätzlich erforderlich:

- Gesamtplan-Go/No-Go bestanden
- aktuelle Anbieter-API verifiziert
- isolierter Signer implementiert und geprüft
- Submit/Confirmation/Reconciliation mit kontrollierten Kleinsttests belegt
- Kill-Switch und Circuit Breaker praktisch getestet
- dokumentierter manueller Freigabeprozess
- keine offenen kritischen Findings

Codeexistenz oder ein erfolgreicher Mock-Test ist kein Beweis für Live-Bereitschaft.

---

## 9. Arbeitsanweisung für Claude B

> Lies zuerst `00_GESAMTPLAN.md`, danach diesen Plan und alle gemeinsamen Contracts. Arbeite ausschließlich in deinem Eigentumsbereich, außer eine gemeinsame Contract-Änderung wurde ausdrücklich abgestimmt. Verwende zunächst synthetische Selections und Golden-Replay-Daten, damit du parallel zu Plan A arbeiten kannst. Implementiere B0 bis B10 in Reihenfolge. Nach jedem Punkt prüfst du den echten Produktionspfad, Idempotenz, Neustartverhalten und relevante Negativfälle. Behebe Abweichungen sofort. Baue keine zweite Scoringlogik. Halte Paper und Shadow technisch von Signierung getrennt. Behaupte keine Live-Bereitschaft ohne bestandene End-to-End-, Recovery- und Sicherheitsnachweise.

