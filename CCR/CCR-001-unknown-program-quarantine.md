# CCR-001: Quarantäneregel für unbekannte Programme im Swap-Pfad

**Eingereicht von:** Claude-Instanz B
**Betrifft:** `03_S0_INTEGRATIONSVERTRAG.md` §5 Regel 9, `contracts/AGGREGATION.md`
**Breaking Change:** nein (Regel wird enger gefasst, keine Feld- oder Typänderung)
**Status:** offen – wartet auf Stellungnahme von Plan A und Merge durch den Projektinhaber

---

## 1. Was geändert werden soll

**Heute (S0 §5 Regel 9):**

> Unbekanntes Programm im Pfad. Es wird kein Teil-Netto-Event ausgegeben. Die Transaktion
> geht für diese Wallet vollständig in Quarantäne mit `decode_confidence` ungleich `COMPLETE`.

**Vorschlag:** Die Regel bleibt in Kraft, wird aber an eine Bedingung geknüpft:

> Ein unbekanntes Programm im Swap-Pfad führt zur Quarantäne der Transaktion für diese
> Wallet, **es sei denn**, die dem Swap zugeordneten Bewegungen stimmen exakt mit der
> tatsächlichen Token-Bilanzänderung der Wallet über die gesamte Transaktion überein.
> Stimmen sie überein, ist bewiesen, dass das unbekannte Programm den Bestand der Wallet
> nicht verändert hat; das Event ist dann `COMPLETE`. Das unbekannte Programm wird in
> jedem Fall gezählt und im `decoder_coverage`-Artefakt namentlich berichtet.

---

## 2. Warum es ohne die Änderung nicht geht

Die Regel in ihrer heutigen Fassung ist implementiert (`UnknownProgramPolicy.QUARANTINE_TRANSACTION`,
Default) und gegen echte Mainnet-Daten gemessen.

**Datengrundlage:** 15 am 2026-09-06 vom öffentlichen Mainnet-RPC aufgezeichnete
Transaktionen (`contracts/golden_raw/`), davon 10 erfolgreich, gezogen aus den jüngsten
Transaktionen von Pump.fun, PumpSwap, Raydium AMM v4, Raydium LaunchLab und Jupiter v6.

**Ergebnis:**

| Policy | erzeugte Events | wirtschaftlich verwertbar (`COMPLETE`) | Quarantäne |
|---|---|---|---|
| `QUARANTINE_TRANSACTION` (S0 Regel 9, Default) | 0 | **0** | 8 |
| `RECONCILE_BALANCES` (dieser Vorschlag) | 4 | 2 | 4 |

Plan B erzeugt unter der heutigen Regel aus **keiner einzigen** echten Transaktion ein
verwertbares Event. Damit ist nicht nur B3 nicht abnehmbar, sondern auch I2, I4 und jede
Latenzmessung unmöglich – es gibt nichts zu vergleichen.

**Wodurch die Quarantäne konkret ausgelöst wird:**

| Programm | Rolle | Wirkung auf die Beträge |
|---|---|---|
| `6Vo3245eszAb5wuqEMw8mGdbfRUdKbHhDHP5LcaGuTAB` | Bot-/Frontend-Wrapper, der den Venue-Aufruf kapselt | keine – der Swap darunter ist vollständig lesbar |
| `QuaNtZsgYRe5Z9Bk4LZ4cTD9tbkVoyCNf1R2BN9bBDv`, `SCoRcH8c2dpjvcJD6FiPbCSQyQgu3PcUAWj2Xxx3mqn`, `ojh19ojaKduoJZuaJADhcVGp4xt1TcdAvZmpVsCorch` | Hilfsprogramme innerhalb einer Jupiter-Route | keine erkennbare |

Das Kernproblem ist strukturell und wird nicht durch Nachpflegen der Registry gelöst:
**jeder kann jederzeit ein Wrapper-Programm deployen.** Wechselt eine beobachtete Wallet
ihr Trading-Frontend, verschwindet sie unter der heutigen Regel lautlos aus der
Verwertbarkeit, obwohl sich an ihrem Handel nichts geändert hat. Plan A würde dieselbe
Wallet im Backfill genauso verlieren – konsistent, aber gemeinsam blind.

Ein Teil des Effekts war tatsächlich Registry-Pflege: `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ`
ist das Pump.fun-Gebührenprogramm (verifiziert gegen die offizielle pump-public-docs-IDL)
und ist inzwischen als Infrastruktur eingetragen. Der Rest bleibt offen.

---

## 3. Warum der Vorschlag sicher ist

Die Bedingung ist kein weicheres Kriterium, sondern ein **stärkerer Beweis** als die
Anwesenheitsprüfung:

- Die heutige Regel prüft: *war ein fremdes Programm beteiligt?*
- Der Vorschlag prüft: *hat irgendetwas den Bestand der Wallet verändert, das wir nicht
  zugeordnet haben?*

Die Rekonziliation vergleicht die Summe der dem Swap zugeordneten Token-Transfers plus
aller außerhalb des Swaps beobachteten Transfers gegen `postTokenBalances − preTokenBalances`
derselben Wallet. Eine Abweichung von einer einzigen kleinsten Einheit führt zur Quarantäne.
Ein unbekanntes Programm, das der Wallet etwas abzweigt, ist damit **nicht** verbergbar.

**Bekannte Grenze, die ausdrücklich benannt wird:** Die Prüfung gilt exakt für die
Base-Token-Seite. Die SOL-Seite lässt sich nicht auf dieselbe Weise prüfen, weil Lamports
auch für Gebühren, Miete und Rückerstattungen fließen und `syncNative` einen WSOL-Bestand
ohne Transferinstruktion verändert. Für die SOL-Seite wird deshalb Sphären-Buchhaltung
verwendet (`contracts/AGGREGATION.md` §2a). Deren Genauigkeit ist unabhängig belegt: gegen
das programmeigene `TradeEvent` von Pump.fun stimmt die Tokenmenge exakt und die SOL-Menge
auf das Lamport genau überein, sobald die Venue-Gebühr von 1,25 % berücksichtigt ist
(`tests/stream/test_decoder_program_events.py`).

---

## 4. Was die jeweils andere Seite nachziehen muss

- **Plan A:** identische Bedingung im historischen Parser. Ohne das divergieren A und B
  auf genau den Transaktionen, die der Vorschlag zusätzlich zulässt – I2 würde brechen.
- **Golden Fixtures:** `expected_events.json` aller Fälle mit unbekanntem Programm im Pfad
  muss neu erzeugt werden. Betroffen sind derzeit 4 der 15 aufgezeichneten Fälle.
- **`contracts/AGGREGATION.md`:** Regel 9 neu formulieren, Rekonziliationsformel aufnehmen.

---

## 5. Umsetzungsstand bei Plan B

Beide Verhalten sind implementiert und durch denselben Test abgedeckt:

```python
SwapDecoder(unknown_program_policy=UnknownProgramPolicy.QUARANTINE_TRANSACTION)  # Default, S0
SwapDecoder(unknown_program_policy=UnknownProgramPolicy.RECONCILE_BALANCES)      # dieser Vorschlag
```

**Der Default ist und bleibt das vertragskonforme Verhalten.** `RECONCILE_BALANCES` wird
ausschließlich dort eingeschaltet, wo die Auswirkung gemessen wird, und ist in der
Konfiguration als vertragsabweichend gekennzeichnet. Wird der CCR abgelehnt, entfällt der
Zweig ersatzlos; es hängt keine weitere Logik daran.

---

## 6. Was noch nicht bewiesen ist

- Die Stichprobe von 15 Transaktionen ist klein und stammt aus einem einzigen Zeitfenster.
  Sie belegt, dass das Problem existiert und relevant ist, nicht wie häufig es über Wochen
  auftritt. Eine belastbare Quote liefert erst der `decoder_coverage`-Bericht aus dem
  laufenden OBSERVE-Betrieb.
- Ob unter `RECONCILE_BALANCES` in der Breite dieselben Events wie bei Plan A entstehen,
  ist offen, bis Plan A dieselbe Regel implementiert und I2 gegen alle 18 Pflichtfälle läuft.
