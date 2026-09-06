# CCR-002: Fehlende Felder in `CopyIntent` und `ExecutionResult`

**Eingereicht von:** Claude-Instanz B
**Betrifft:** `03_S0_INTEGRATIONSVERTRAG.md` §4.5, §4.6
**Breaking Change:** nein – ausschließlich optionale Feldergänzungen, MINOR nach §11.2
**Status:** offen

---

## 1. `CopyIntent`: `target_base_raw`

**Problem.** §4.5 kennt für einen Verkauf nur `target_size_quote_raw` und
`target_fraction_bps`. Ein Broker kann daraus keine Order bauen: Verkauft wird eine
**Base**-Menge, und die ergibt sich erst aus dem zum Entscheidungszeitpunkt vorhandenen
Copy-Bestand. Ohne das Feld müsste der Broker den Bestand erneut lesen und den Prozentsatz
selbst anwenden – zu einem späteren Zeitpunkt als die Entscheidung. Genau dann ist die
Invariante „verkaufte Copy-Menge ≤ vorhandene Copy-Menge" (§9.2) nicht mehr aus dem Intent
allein prüfbar, und Plan A kann aus `paper_fills.jsonl` nicht mehr nachvollziehen, welche
Menge die Entscheidung gemeint hat.

**Vorschlag.** Optionales Feld `target_base_raw: string | null`. Pflicht bei
`decision = EXECUTE` und `side = SELL`, sonst `null`. Bedeutung: die Base-Rohmenge, die
`target_fraction_bps` zum Entscheidungszeitpunkt entsprach.

**Alternative, falls abgelehnt:** `target_size_quote_raw` bei SELL als Base-Menge
umdeuten. Das wäre eine Bedeutungsänderung eines bestehenden Feldes und damit ein echter
Breaking Change – deutlich schlechter.

## 2. `CopyIntent`: `reason_detail`

Optional, `string | null`. `reason_code` ist ein Enum und bleibt die maschinenlesbare
Wahrheit; `reason_detail` trägt den konkreten Messwert („liquidity 4.1 SOL < 20 SOL").
Ohne dieses Feld ist eine Ablehnung zählbar, aber nicht diagnostizierbar.

## 3. `CopyIntent`: drei zusätzliche `reason_code`-Werte

`02_PLAN_B` §5 formuliert seine Liste ausdrücklich als Mindestmenge. Ergänzt:

| Wert | Warum kein vorhandener Wert passt |
|---|---|
| `transaction_failed` | Die Quell-Transaktion ist on-chain fehlgeschlagen. Kein bestehender Grund beschreibt das; `decode_incomplete` wäre falsch, denn dekodiert ist nichts falsch. |
| `dedup_policy_suppressed` | Unterdrückung durch `FIRST_SIGNAL_ONLY`, obwohl die Wallet ausgewählt und das Event gültig ist. `duplicate_event` wäre falsch – es ist ein anderes Event einer anderen Wallet. |
| `size_below_minimum` | Die gewichtete Zielgröße unterschreitet die konfigurierte Mindestgröße. Ohne eigenen Grund verschwindet dieser Fall in `position_limit` und verfälscht dessen Statistik. |

## 4. `ExecutionResult`: `attempt`

**Problem.** §4.6 hat kein Feld, das mehrere Versuche zu einem Intent unterscheidet.
§B9 verlangt ausdrücklich, nach einem Timeout **erst zu reconciliieren und dann einen Retry
zu erwägen**. Ein Retry erzeugt aber ein zweites `ExecutionResult` zu derselben
`intent_id`. Ohne `attempt` lassen sich die beiden weder unterscheiden noch ordnen, und
Plan A kann aus `paper_fills.jsonl` nicht erkennen, ob ein Fill der erste oder der dritte
Versuch war.

**Vorschlag.** `attempt: integer >= 1`, Default `1`.
Zusätzliche Invariante: pro `intent_id` darf höchstens ein Ergebnis
`status ∈ {FILLED, PARTIAL}` tragen. Plan B erzwingt das bereits über einen partiellen
Unique-Index in der Datenbank.

## 5. `ExecutionResult`: `simulation`

**Problem.** §B6 verlangt, dass „Quote, Annahmen und Ergebnis vollständig auditierbar" sind,
und §A10 verlangt, dass Plan A den Backtest mit den real gemessenen Verteilungen neu
rechnet. Ein Paper-Fill entsteht aber aus Annahmen, die nirgends im Artefakt stehen:
angenommene Landing-Wahrscheinlichkeit, ob dieser Fill gelandet ist, RNG-Seed, zusätzlich
angesetzte Slippage, angenommene Priority Fee, Alter der Quote beim Fill. Ohne diese Werte
kann Plan A weder reproduzieren noch kalibrieren – der Paper-Fill ist dann eine Zahl ohne
Herkunft.

**Vorschlag.** Optionales Objekt `simulation`, Pflicht bei `mode = PAPER`, sonst `null`:

```json
{
  "model_version": "1.0.0",
  "assumed_land_probability": "0.9",
  "landed": true,
  "rng_seed": "gundix-paper-v1|<intent_id>",
  "applied_extra_slippage_bps": 50,
  "assumed_priority_fee_lamports": "100000",
  "quote_age_ms_at_fill": 120
}
```

Der Seed ist deterministisch aus Konfigurations-Seed und `intent_id` abgeleitet, damit
derselbe Lauf dasselbe Ergebnis erzeugt – Voraussetzung für §9.1 („keine Zufälligkeit ohne
festen Seed").

---

## 6. Was die andere Seite nachziehen muss

- **Plan A:** liest `paper_fills.jsonl`. Alle Ergänzungen sind optional; ein Leser, der sie
  ignoriert, funktioniert unverändert weiter. Für A10 sind `simulation` und `attempt`
  jedoch nötig, sonst ist die Kalibrierung nicht reproduzierbar.
- **Golden Fixtures:** nicht betroffen, die Änderungen berühren `SwapEvent` nicht.

## 7. Umsetzungsstand bei Plan B

Bis zur Entscheidung gilt: Die **Artefakte** enthalten ausschließlich die in §4.5/§4.6
festgelegten Felder. `target_base_raw` und `reason_detail` sind in Schema und Modell
enthalten, weil ohne sie kein Verkauf ausführbar ist; `attempt` und `simulation` werden
ausschließlich in der lokalen Datenbank von Plan B geführt und **nicht** in
`paper_fills.jsonl` geschrieben, solange dieser CCR offen ist.
