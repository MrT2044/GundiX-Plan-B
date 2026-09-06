# Aggregationsregeln (verbindlich für Plan A und Plan B)

**Status:** Entwurf von Plan B, offen für Gegenzeichnung durch Plan A.
**Zweck:** Der historische Parser (Plan A) und der Live-/Replay-Decoder (Plan B) müssen auf
derselben Transaktion **byte-identische** `SwapEvent`s erzeugen (Integrationspunkt I2). Dieses
Dokument ist die einzige Quelle der Wahrheit dafür, wie aus Roh-Instruktionen Netto-Swaps werden.

Wenn eine Regel hier unklar ist, ist das ein Contract-Bug und wird hier behoben – nicht in einer
der beiden Implementierungen.

---

## 1. Begriffe

- **Leg (Roh-Swap):** Eine einzelne erkannte Swap-Instruktion eines bekannten Venue-Programms, die
  Tokenkonten der beobachteten Wallet berührt. Ein Leg hat `in_mint`, `in_amount_raw`, `out_mint`,
  `out_amount_raw`, `venue`, `pool` und `instruction_path`.
- **Netto-Swap:** Das wirtschaftliche Ergebnis einer zusammenhängenden Kette von Legs für **eine**
  Wallet in **einer** Transaktion. Nur Netto-Swaps werden als `SwapEvent` veröffentlicht.
- **instruction_path:** Punktgetrennter Indexpfad, `"3"` für die vierte Top-Level-Instruktion,
  `"3.1.0"` für deren verschachtelte Inner Instruction. Immer 0-basiert, immer die Reihenfolge, in
  der der RPC die Instruktionen liefert.

---

## 2. Ablauf

Solana zeigt die beiden Seiten eines Swaps **nicht auf dieselbe Weise**:

- Die SPL-Token-Seite ist immer eine explizite `transfer`/`transferChecked`-Instruktion
  und damit exakt lesbar.
- Die SOL-Seite oft nicht. Eine Pump.fun-Bonding-Curve verschiebt Lamports per
  `sub_lamports`/`add_lamports` direkt im Programm. Dazu existiert **keine** Instruktion,
  nur die Kontostaende aendern sich.

Daraus folgt der verbindliche Ablauf:

1. **Swap-Instruktionen bestimmen.** Eine Top-Level-Instruktion gilt als Swap-Instruktion,
   wenn sie selbst oder irgendeine ihrer Inner Instructions ein Venue- oder Router-Programm
   aufruft. Reale Transaktionen kapseln den Venue-Aufruf regelmaessig in ein Bot- oder
   Aggregator-Programm; wer nur die Top-Level-Programm-ID prueft, sieht diese Swaps nicht.
2. **Eine Top-Level-Instruktion ist ein Netto-Swap.** Eine Jupiter-Multi-Hop-Route liegt
   vollstaendig innerhalb einer Top-Level-Instruktion; ihre Zwischen-Mints saldieren sich
   dort von selbst auf null. Zwei unabhaengige Kaeufe in einer Transaktion bleiben dagegen
   zwei Events und werden nicht zu einem unsinnigen Drei-Mint-Swap verschmolzen.
3. **Token-Seite aus den Transfers.** Alle Transfers der beobachteten Wallet unterhalb
   dieser Instruktion werden je Mint saldiert. Mints mit Saldo 0 entfallen.
4. **SOL-Seite aus Sphaeren-Buchhaltung.** Siehe Abschnitt 2a.
5. **Base/Quote bestimmen.** Siehe Abschnitt 3.
6. **Gegenpruefung.** Die zugeordnete Base-Token-Bewegung muss der tatsaechlichen
   Token-Bilanzaenderung der Wallet entsprechen. Weicht sie ab, ist `decode_complete=false`.
7. **Sortieren und indizieren.** Siehe Abschnitt 4.

### 2a. Sphaeren-Buchhaltung fuer die SOL-Seite

```
sol_flow = (post_lamports(wallet) - pre_lamports(wallet))
         + Summe der Lamport-Aenderungen aller Tokenkonten der Wallet
         + transaction_fee, falls die Wallet Fee-Payer ist
         - Summe der System-Transfers der Wallet ausserhalb der Swap-Instruktion
```

Begruendung der Terme:

- Tokenkonten der Wallet gehoeren zur wirtschaftlichen Sphaere der Wallet. Wrapping
  verschiebt Wert nur zwischen Hauptkonto und WSOL-Konto und wird dadurch unsichtbar.
- Bei Nicht-WSOL-Tokenkonten ist die Lamport-Aenderung reine Miete (Rent). Sie wird
  zurueckaddiert, damit das Anlegen eines ATA nicht wie Swap-Kosten aussieht.
- Die Transaktionsgebuehr ist Kosten des Transagierens, nicht Teil des Swap-Preises.
- Tips und Bot-Gebuehren ausserhalb der Swap-Instruktion verfaelschen sonst den
  ausgefuehrten Preis.

`sol_flow > 0` bedeutet: die Wallet hat SOL erhalten (Verkauf).

Die Sphaeren-Buchhaltung gilt fuer die **gesamte Transaktion**. Enthaelt eine Transaktion
mehr als einen Netto-Swap, laesst sie sich nicht aufteilen; dann wird die SOL-Seite nur
aus sichtbaren Transfers gebildet, und wo diese fehlen, entsteht ein Quarantaenefall.

**Verifiziert:** Fuer Pump.fun laesst sich das Ergebnis gegen das programmeigene
`TradeEvent` im Transaktionslog pruefen. Auf den aufgezeichneten Fixtures stimmt die
Token-Menge exakt ueberein, und die SOL-Menge entspricht dem Programmwert plus/minus der
Venue-Gebuehr von 1,25 % - auf das Lamport genau. Der Test dazu ist
`tests/stream/test_decoder_program_events.py`.

## 3. Base und Quote

Quote-Mint-Priorität (absteigend). Der erste Treffer in dieser Liste ist der Quote-Mint:

| Rang | Mint | Symbol |
|---|---|---|
| 1 | `So11111111111111111111111111111111111111112` | WSOL |
| 2 | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` | USDC |
| 3 | `Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB` | USDT |

Regeln:

- **Natives SOL und WSOL sind dasselbe Asset.** Ein Bonding-Curve-Kauf, der Lamports direkt vom
  System-Account abzieht, wird auf den WSOL-Mint abgebildet. `quote_decimals = 9`.
- Enthält ein Paar keinen Mint aus der Liste (Token/Token-Swap), gilt: `quote_mint` ist der Mint mit
  der lexikografisch kleineren Base58-Adresse, `base_mint` der andere. Solche Events sind gültig,
  erzeugen in Phase 1 aber `NO_TRADE` mit `unsupported_token`, weil GundiX nur gegen SOL kopiert.
- Enthält ein Paar zwei Mints aus der Liste (z. B. SOL/USDC), gewinnt der höhere Rang als Quote.
- `side = "buy"`, wenn `net[base_mint] > 0` (Wallet erhält den Base-Token), sonst `"sell"`.
- `base_amount_raw = |net[base_mint]|`, `quote_amount_raw = |net[quote_mint]|`. Immer positiv,
  immer Rohinteger als String.

---

## 4. Reihenfolge und `net_index`

- Netto-Swaps einer Wallet in einer Transaktion werden nach dem Index ihrer
  Top-Level-Instruktion aufsteigend sortiert.
- `net_index` ist die 0-basierte Position in dieser Sortierung.
- `instruction_path` ist der Pfad der ersten beitragenden Instruktion. Pfadformat:
  `"<i>"` fuer die i-te Top-Level-Instruktion, `"<i>.<j>"` fuer den j-ten Eintrag der
  zugehoerigen `innerInstructions`-Liste in RPC-Reihenfolge. Verglichen wird
  komponentenweise numerisch (`"3.1" < "3.10" < "4"`), nicht als Zeichenkette.
- `venue` ist die Venue aller Legs, wenn sie uebereinstimmen, sonst `"mixed"`.
- `pool` ist der Pool, wenn genau einer beteiligt war, sonst `null`.
- `router` wird gesetzt, wenn die Legs unter einem bekannten Aggregator-Programm liefen.
- `hop_count` ist die Anzahl der Venue-Programmaufrufe innerhalb der Instruktion.

Kauf **und** Verkauf in derselben Transaktion ergeben zwei Top-Level-Swap-Instruktionen
und damit zwei Events mit `net_index` 0 und 1.

## 5. `event_id`

```
event_id = sha256_hex(
    "gundix.swap.v1|solana|" + signature + "|" + wallet + "|" + instruction_path + "|" + str(net_index)
)
```

- UTF-8, keine Leerzeichen, keine Normalisierung der Adressen (Base58 wie von der Chain geliefert).
- `source` und `observed_at_utc` gehen **nicht** ein: Plan A und Plan B müssen für dieselbe
  Transaktion dieselbe `event_id` erzeugen.
- Die Referenzimplementierung ist `src/contracts/ids.py::compute_event_id`. Beide Pläne benutzen
  diese Funktion, sie wird nicht nachgebaut.

---

## 6. Was **kein** Swap ist

| Fall | Behandlung |
|---|---|
| SPL `Transfer` / `TransferChecked` ohne zugehöriges Swap-Leg | Kein `SwapEvent`. Plan A bucht es in der Positionsrekonstruktion, Plan B erzeugt `NO_TRADE` mit `transfer_not_swap`. |
| Wrap/Unwrap von SOL (`SyncNative`, `CloseAccount`) | Kein Swap. Nur Kontobewegung. |
| LP-Aktionen (Add/Remove Liquidity), Staking, Vesting | Kein Swap. `decode_complete` bleibt `true`, wenn das Programm bekannt ist. |
| Airdrop / Mint an die Wallet ohne Gegenleistung | Kein Swap. Erzeugt in Plan A Bestand ohne Kostenbasis. |
| Fehlgeschlagene Transaktion | Erzeugt **kein** `SwapEvent`. Eine fehlgeschlagene Transaktion hat keine Bilanzaenderung; jeder aus ihren Instruktionen abgeleitete Betrag waere erfunden. Sie wird als Beobachtung gezaehlt (`failed_transactions_total`) und die Rohtransaktion bleibt gespeichert. Plan B protokolliert `NO_TRADE` mit `transaction_failed`, sobald sie einer beobachteten Wallet zugeordnet ist. |

---

## 7. Quarantäne und Coverage

Ein Netto-Swap geht in Quarantäne (`decode_complete = false`), wenn mindestens eines gilt:

- Nach Schritt 4 bleiben mehr als zwei Mints mit `net != 0` in einer Komponente.
- Ein Programm, das Tokenkonten der Wallet bewegt, ist unbekannt (`unknown_program_ids` nicht leer).
- Die Decimals eines beteiligten Mints sind nicht ermittelbar.
- Die Summe der Token-Balance-Deltas der Wallet stimmt nicht mit der Summe der Legs überein.

Quarantänefälle:

- werden **vollständig gespeichert**, inklusive Rohtransaktion, niemals stillschweigend verworfen,
- erhöhen den Zähler `decoder_unknown_total` bzw. senken `decoder_coverage`,
- führen in Plan B immer zu `NO_TRADE` mit `decode_incomplete` bzw. `unknown_venue`,
- sind in Plan A ein Coverage-Abzug für die betroffene Wallet, kein Gewinn und kein Verlust.

---

## 8. Gebühren

- `fee_lamports` ist die **Transaktionsgebühr inklusive Priority Fee**, also `meta.fee`.
- `tip_lamports` ist ein erkannter Out-of-Protocol-Tip (Transfer an eine bekannte Tip-Adresse).
- Beide gelten für die **gesamte Transaktion**. Enthält eine Transaktion mehrere Netto-Swaps, steht
  derselbe Betrag auf jedem Event. Konsumenten aggregieren Gebühren **pro Signatur**, nicht pro
  Event. Wer sie pro Event summiert, zählt doppelt.
- Venue-Gebühren stecken bereits im Nettobetrag, den die Wallet erhalten hat, und werden nicht
  zusätzlich abgezogen.

---

## 9. Token-2022

- Transfer Fees werden **nicht** herausgerechnet. `base_amount_raw` ist der Betrag, der dem
  Tokenkonto der Wallet tatsächlich gutgeschrieben bzw. belastet wurde.
- Mints mit aktivem Freeze- oder Transfer-Hook werden dekodiert, aber in Plan B über
  `unsupported_token` blockiert, bis der Fall explizit getestet ist.

---

## 10. Beträge und Zeit

- Alle Rohbeträge sind **Dezimalstrings nicht-negativer Integer**. Niemals JSON-Floats, niemals
  wissenschaftliche Notation, keine führenden Nullen.
- `block_time_utc` kommt aus `blockTime` der Chain (Sekundenauflösung) und wird als
  `...T00:00:00Z`-Form mit Sekundengenauigkeit serialisiert. Millisekunden werden nicht erfunden.
- `observed_at_utc` ist lokale Wanduhrzeit des Producers. Für Latenzstatistik ist ausschließlich
  `observed_at_utc − block_time_utc` von Plan B im Live-Betrieb zulässig; Plan A darf daraus nichts
  ableiten.

---

## 11. Änderungen an diesem Dokument

Eine Änderung an Abschnitt 2–5 ist ein **Breaking Change**: Sie ändert `event_id` oder den
Nettoeffekt und erfordert eine Major-Version in `swap_event.json` sowie eine erneute
Reconciliation (I2). Änderungen in Abschnitt 6–10 erfordern mindestens einen neuen Golden Case.
