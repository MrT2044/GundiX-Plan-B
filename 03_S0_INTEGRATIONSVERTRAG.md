# GundiX – S0 Integrationsvertrag und verbindlicher Arbeitsauftrag

**Version:** 1.0.0
**Status:** Vorschlag zur Freigabe durch den Projektinhaber
**Verfasst von:** Claude-Instanz A (Plan A)
**Gilt für:** Claude-Instanz A, Claude-Instanz B und die spätere Integrations-Instanz
**Übergeordnet:** `00_GESAMTPLAN.md` – dieses Dokument widerspricht ihm nicht, sondern macht ihn implementierbar.

---

## 0. Wie dieses Dokument zu benutzen ist

`00_GESAMTPLAN.md` legt fest **was** gebaut wird. Er lässt an entscheidenden Stellen offen **wie genau** – zum Beispiel welchen konkreten Typ `base_amount_raw` hat, wie `event_id` exakt berechnet wird oder wem Transaktionsgebühren zugeordnet werden. Genau an diesen Stellen entsteht später der Integrationsbruch, den §8 des Gesamtplans verhindern will.

Dieses Dokument schließt diese Lücken. Es ist gleichzeitig:

1. **Vertrag** zwischen Plan A und Plan B.
2. **Prompt** für beide Claude-Instanzen. Abschnitt 13 enthält den Text, den jede Instanz zu Beginn erhält.
3. **Entscheidungsvorlage** für den Menschen. Abschnitt 14 listet, was noch offen ist.

**Änderungen an diesem Dokument sind nur über den CCR-Prozess in Abschnitt 11 erlaubt.** Keine Instanz darf eine Festlegung hier stillschweigend anders implementieren. Wenn eine Instanz eine Festlegung für falsch hält, meldet sie das und arbeitet weiter nach dem Dokument, bis der Mensch entschieden hat.

**Legende:**

- **[FEST]** – entschieden, gilt ab sofort.
- **[OFFEN]** – der Mensch muss entscheiden, siehe Abschnitt 14.

---

## 1. Repository-Aufbau und Berechtigungen

### 1.1 Entscheidung: drei Repositories [FEST]

| Repo | Schreibrecht | Leserecht | Inhalt |
|---|---|---|---|
| `gundix-contracts` | Mensch (Merge), A und B nur per CCR-PR | A, B | JSON-Schemas, generierte Python-Typen, Golden Fixtures, Aggregationsregeln |
| `gundix-plan-a` | Claude A | Claude B | `src/discovery`, `src/research`, `src/backtest`, zugehörige Tests |
| `gundix-plan-b` | Claude B | Claude A | `src/stream`, `src/paper`, `src/execution`, zugehörige Tests |

### 1.2 Warum drei und nicht zwei

Die naheliegende Variante wäre: zwei Repos, jeder liest den anderen. Das Problem ist nicht der Code – das Problem sind die gemeinsamen Datenmodelle.

`SwapEvent` muss in Plan A und Plan B **bitgenau dasselbe** bedeuten, sonst scheitert Integrationspunkt I2 („Historischer Parser A und Live-Decoder B erzeugen dieselben Netto-SwapEvents"). Wenn diese Definition in zwei Repos liegt, gibt es zwangsläufig zwei Kopien. Zwei Kopien driften auseinander – nicht aus Nachlässigkeit, sondern weil beide Seiten legitime kleine Anpassungen machen. Der Bruch fällt dann erst bei der Integration auf, also zum teuersten Zeitpunkt.

Mit `gundix-contracts` als eigenem Repo, das beide als **Git-Submodule auf einen exakten Commit gepinnt** einbinden, ist das strukturell unmöglich. Es gibt genau eine Definition. Eine Änderung ist ein sichtbarer Commit, den beide Seiten bewusst nachziehen müssen.

Das dritte Repo ist damit weniger Arbeit, nicht mehr: es ersetzt eine dauerhafte manuelle Abgleichpflicht durch einen einmaligen Setup-Schritt.

### 1.3 Einbindung

```bash
git submodule add https://github.com/<org>/gundix-contracts.git contracts
git -C contracts checkout <exakter-tag>
git add contracts .gitmodules
git commit -m "Pin contracts to <exakter-tag>"
```

**Regeln:**

- Das Submodule wird **immer auf einen Tag gepinnt**, nie auf einen beweglichen Branch.
- Ein Versionssprung des Submodules ist ein eigener Commit mit Begründung im Text.
- Keine Instanz editiert Dateien innerhalb von `contracts/` in ihrem eigenen Repo. Änderungen laufen über CCR (Abschnitt 11).
- Beide Instanzen haben Leserecht auf das jeweils andere Plan-Repo. Dieses Leserecht dient dem Verständnis und der Reconciliation – **nicht dem Import**. Siehe Abschnitt 10.3.

### 1.4 Verzeichnisstruktur je Plan-Repo [FEST]

```text
gundix-plan-a/                    gundix-plan-b/
  contracts/     <- submodule       contracts/     <- submodule
  src/                              src/
    discovery/                        stream/
    research/                         paper/
    backtest/                         execution/
    common/                           common/
  tests/                            tests/
    contracts/                        contracts/
    discovery/                        stream/
    research/                         paper/
    backtest/                         execution/
    integration/                      integration/
  artifacts/                        artifacts/
    candidates/                       observations/
    selections/
    reports/
  data/          <- gitignored      data/          <- gitignored
  config/                           config/
  migrations/                       migrations/
  INTERFACE_STATUS.md               INTERFACE_STATUS.md
  pyproject.toml                    pyproject.toml
  uv.lock                           uv.lock
  .env.example                      .env.example
```

`src/common/` enthält ausschließlich planinterne Hilfsfunktionen (Logging-Setup, Pfad-Auflösung, Hash-Utilities). **Niemals Domain-Modelle** – die kommen aus `contracts/`.

---

## 2. Verbindlicher Technologie-Stack

### 2.1 Laufzeit [FEST]

| Komponente | Version | Begründung |
|---|---|---|
| Python | **3.12.x** (exakt, nicht 3.13, nicht 3.14) | `solders` und `solana-py` liefern für 3.12 verlässlich fertige Wheels. Für 3.14 ist das nicht garantiert; ein fehlendes Wheel erzwingt eine Rust-Toolchain-Kompilierung auf beiden Rechnern. Gesamtplan §3 schreibt 3.12 ohnehin vor. |
| Paketmanager | `uv` (aktuelle Version) | Gesamtplan §3. Erzeugt reproduzierbare `uv.lock`. |

**Beide Instanzen müssen dieselbe Python-Minor-Linie verwenden.** Festgeschrieben in `pyproject.toml` als `requires-python = ">=3.12,<3.13"` und in `.python-version` als `3.12`.

### 2.2 Bibliotheken [FEST]

| Zweck | Bibliothek |
|---|---|
| Validierung | `pydantic` v2 |
| JSON Schema | `jsonschema` (Draft 2020-12) |
| HTTP | `httpx` |
| WebSocket | `websockets` |
| Solana-Typen | `solders` |
| Solana-RPC | `solana` (nur wo `solders` nicht reicht) |
| Analytik | `polars`, `duckdb` |
| ORM / Migration | `sqlalchemy` v2, `alembic` |
| Spaltenspeicher | `pyarrow` (Parquet) |
| Tests | `pytest`, `pytest-asyncio`, `hypothesis` |
| Qualität | `ruff`, `mypy` |

**Verboten:** `pandas` (Polars ist gesetzt, kein zweites DataFrame-Backend), `requests` (httpx ist gesetzt), `dataclasses` für Domain-Objekte (Pydantic ist gesetzt), jede zweite Validierungsbibliothek.

### 2.3 Werkzeugkonfiguration [FEST]

Identisch in beiden Repos, wörtlich zu übernehmen:

```toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "RUF", "ASYNC", "DTZ"]
ignore = ["E501"]

[tool.mypy]
python_version = "3.12"
strict = true
warn_unreachable = true
disallow_any_explicit = true
plugins = ["pydantic.mypy"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
  "integration: benoetigt echte Provider-Zugaenge, ohne Keys uebersprungen",
  "golden: Contract-Reconciliation gegen contracts/golden",
]
```

Die Ruff-Regel `DTZ` ist bewusst aktiv: sie verbietet naive Datetimes. Das ist hier kein Stilproblem, sondern verhindert eine ganze Fehlerklasse.

---

## 3. Absolute Grundregeln für Zahlen und Zeit

Diese Regeln sind der häufigste stille Integrationsbruch in Trading-Systemen. Sie gelten ausnahmslos.

### 3.1 Geld und Tokenmengen [FEST]

- **Niemals `float`.** Kein Domain-Feld, das eine Geldmenge, Tokenmenge, Gebühr oder einen Preis darstellt, hat den Typ `float`.
- **Rohbeträge** (`*_raw`) sind nicht skalierte Ganzzahlen in der kleinsten Einheit des Tokens.
  - In Python: `int`
  - In JSON: **`string`**, dezimal, ohne Vorzeichen, ohne führende Nullen, z. B. `"1234567890123456789"`
  - Grund: JSON-Zahlen sind in vielen Parsern IEEE-754-Doubles und verlieren oberhalb von 2^53 Präzision. Ein Token mit 9 Dezimalstellen und großem Supply überschreitet das im Alltag.
- **Rohbeträge sind immer nicht-negativ.** Die Richtung ergibt sich aus `side`, nie aus einem Vorzeichen.
- **Preise, Verhältnisse, Quoten** sind `Decimal` in Python und `string` in JSON. Der Kontext wird explizit gesetzt, nie implizit vom Prozess geerbt.
- **Prozente und Slippage** werden als **Basispunkte** geführt (`int`, 1 bps = 0,01 %). Kein Feld heißt `slippage_percent`.
- **Umrechnung raw -> dezimal** passiert ausschließlich an der Darstellungsgrenze, nie in der Rechenlogik.

### 3.2 Zeit [FEST]

- Alle gespeicherten und übertragenen Zeitstempel sind **UTC**.
- JSON-Format: ISO-8601 mit `Z`-Suffix.
  - Blockzeiten: Sekundengenauigkeit, `2026-06-01T12:00:00Z`
  - Beobachtungszeitpunkte: Millisekundengenauigkeit, `2026-06-01T12:00:00.123Z`
- Python: `datetime` mit `tzinfo=timezone.utc`. Naive Datetimes sind verboten (Ruff `DTZ` erzwingt das).
- Lokalzeit existiert ausschließlich in der Anzeige für Menschen, niemals in Dateien, Datenbanken oder Verträgen.
- **Fensterfilter erfolgen über Slot-Grenzen, nicht über Datumsvergleiche.** Der Datumswert dient der Dokumentation, der Slot der Selektion.

### 3.3 Identifikatoren [FEST]

- Solana-Adressen und Signaturen werden **nie gekürzt** gespeichert oder geloggt.
- Validierung bei Eingang gegen Base58-Alphabet und Länge (Adresse 32–44 Zeichen, Signatur 87–88 Zeichen).
- Adressen sind **case-sensitiv**. Kein `.lower()`, keine Normalisierung.
- Eine Adresse ist niemals ein Anzeigename und wird nie mit einem Label vermischt.

---

## 4. Die gemeinsamen Schemas

**Ablage:** `contracts/schemas/<name>.schema.json`, JSON Schema Draft 2020-12.
**Quelle der Wahrheit ist das JSON Schema.** Die Pydantic-Modelle unter `contracts/python/gundix_contracts/` werden dagegen getestet – ein Test schlägt fehl, sobald Modell und Schema divergieren.

Jedes Schema hat `additionalProperties: false`. Ein unbekanntes Feld ist ein Fehler, keine Toleranz.

### 4.1 `SwapEvent` v1.0.0

Die kanonische Repräsentation eines erkannten Swaps. **Es gibt genau diese eine.** Historischer Parser (A) und Live-Decoder (B) erzeugen identische Objekte für dieselbe Transaktion.

| Feld | Typ (JSON) | Pflicht | Bedeutung |
|---|---|---|---|
| `schema_version` | string | ja | `"1.0.0"` |
| `chain` | string | ja | konstant `"solana"` |
| `event_id` | string | ja | 64 Zeichen Hex, deterministisch, siehe 4.1.1 |
| `signature` | string | ja | Base58-Transaktionssignatur |
| `slot` | integer | ja | ≥ 0 |
| `block_time_utc` | string | ja | ISO-8601 UTC, Sekundengenauigkeit |
| `transaction_index` | integer \| null | ja | Index im Block. `null` = Provider liefert es nicht. Nur für Ordnung, **nie** in `event_id`. |
| `instruction_path` | string | ja | siehe 4.1.2 |
| `net_swap_index` | integer | ja | ≥ 0, siehe 4.1.3 |
| `wallet` | string | ja | Base58, die Wallet, deren Nettoeffekt beschrieben wird |
| `base_mint` | string | ja | Base58, das gehandelte Token |
| `quote_mint` | string | ja | Base58, das Gegentoken |
| `side` | string | ja | `"BUY"` oder `"SELL"`, siehe 4.1.4 |
| `base_amount_raw` | string | ja | nicht-negative Ganzzahl als String |
| `quote_amount_raw` | string | ja | nicht-negative Ganzzahl als String |
| `base_decimals` | integer | ja | 0–18 |
| `quote_decimals` | integer | ja | 0–18 |
| `venue` | string | ja | Enum, siehe 4.1.5 |
| `pool` | string \| null | ja | Base58-Pool-/Marktadresse |
| `router` | object \| null | ja | siehe 4.1.6 |
| `success` | boolean | ja | siehe 4.1.7 |
| `source` | string | ja | `"HISTORICAL_BACKFILL"` \| `"LIVE_STREAM"` \| `"REPLAY"` \| `"GOLDEN_FIXTURE"` |
| `source_provider` | string | ja | z. B. `"helius"`, `"solana_tracker"`, `"fixture"` |
| `observed_at_utc` | string | ja | ISO-8601 UTC, Millisekunden. Wann **unser** System es zuerst sah. |
| `finality` | string | ja | `"PROCESSED"` \| `"CONFIRMED"` \| `"FINALIZED"` |
| `fees` | object | ja | siehe 4.1.8 |
| `decode_confidence` | string | ja | `"COMPLETE"` \| `"PARTIAL"` \| `"UNKNOWN"`, siehe 4.1.9 |

#### 4.1.1 Berechnung von `event_id` [FEST]

```python
import hashlib

US = "\x1f"  # ASCII Unit Separator

def compute_event_id(
    chain: str, signature: str, wallet: str,
    instruction_path: str, net_swap_index: int,
) -> str:
    payload = US.join([chain, signature, wallet, instruction_path, str(net_swap_index)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

Der Unit-Separator `\x1f` wird verwendet, weil er in Base58-Adressen und im Instruktionspfad nicht vorkommen kann. Ein normales Trennzeichen wie `:` oder `|` wäre theoretisch fälschbar.

**`transaction_index`, `observed_at_utc`, `source` und `finality` gehen bewusst nicht ein.** Dieselbe Transaktion muss im historischen Backfill und im Live-Stream dieselbe `event_id` bekommen – sonst ist Deduplikation über Neustarts hinweg unmöglich und I2 nicht prüfbar.

#### 4.1.2 `instruction_path` [FEST]

Punktgetrennte Kette von Instruktionsindizes, beginnend bei der Top-Level-Instruktion, dann Inner Instructions in Reihenfolge.

- `"3"` = vierte Top-Level-Instruktion
- `"3.1"` = zweite Inner Instruction darin
- `"3.1.0"` = erste Inner Instruction eine Ebene tiefer

**Bei einem aggregierten Router-Swap ist der Pfad der Pfad der äußersten Instruktion, die die gesamte Route umschließt** – nicht der des ersten Hops. Diese Regel ist zwingend, sonst ergeben A und B unterschiedliche `event_id`s für denselben Trade.

#### 4.1.3 `net_swap_index` [FEST]

Fortlaufender Index ab 0 über alle Netto-Swap-Events **derselben Wallet in derselben Transaktion**, sortiert nach dem `instruction_path` der jeweils äußersten Instruktion (segmentweise numerisch verglichen).

Bei genau einem Swap pro Wallet und Transaktion ist der Wert `0`. Das ist der Normalfall.

#### 4.1.4 `side` [FEST]

Definiert **aus Sicht von `base_mint`**:

- `BUY` – die Wallet erhält `base_mint` und gibt `quote_mint` ab.
- `SELL` – die Wallet gibt `base_mint` ab und erhält `quote_mint`.

Welches der beiden Token `base` ist, entscheidet die Quote-Priorität in 4.1.10.

#### 4.1.5 `venue` Enum [FEST]

`PUMP_FUN`, `PUMPSWAP`, `RAYDIUM_AMM_V4`, `RAYDIUM_CPMM`, `RAYDIUM_CLMM`, `RAYDIUM_LAUNCHLAB`, `METEORA_DLMM`, `METEORA_DAMM`, `ORCA_WHIRLPOOL`, `UNKNOWN`

**Erweiterung des Enums ist ein CCR.** Ein Programm, das keinem Wert zugeordnet werden kann, führt zu `venue = "UNKNOWN"` und `decode_confidence = "UNKNOWN"`. Ein solches Event darf niemals in Positionsrekonstruktion (A) oder Intent-Erzeugung (B) eingehen – es geht in Quarantäne und erhöht eine Coverage-Metrik.

#### 4.1.6 `router` [FEST]

`null`, wenn direkt gegen einen Pool gehandelt wurde. Sonst:

```json
{ "program_id": "<base58>", "label": "JUPITER_V6", "hops": 3 }
```

`label` Enum: `JUPITER_V6`, `JUPITER_V4`, `OKX_DEX`, `OTHER`.
`hops` = Anzahl der tatsächlich durchlaufenen Pool-Interaktionen, ≥ 1.

#### 4.1.7 `success` [FEST]

Der Gesamtplan sagt: „Eine fehlgeschlagene Transaktion ist kein ausgeführter Trade." Das Feld existiert trotzdem, weil Fehlschläge zählbar bleiben müssen.

**Regel:** Ein `SwapEvent` mit `success = false` darf ausschließlich in der Roh-/Quarantäne-Ebene existieren. **Jeder wirtschaftliche Konsument** – Positionsrekonstruktion, PnL, Statistik, SignalPolicy, RiskEngine – filtert zwingend auf `success == true`. Diese Filterung ist in beiden Repos durch einen Test abzusichern.

#### 4.1.8 `fees` [FEST]

Gebühren sind **transaktionsweit**, Swaps sind es nicht. Ohne feste Zuordnungsregel zählen A und B unterschiedlich.

```json
{
  "network_fee_lamports": "5000",
  "priority_fee_lamports": "120000",
  "tip_lamports": "0",
  "venue_fee_raw": "1000000",
  "venue_fee_mint": "<base58>",
  "transfer_fee_base_raw": "0",
  "transfer_fee_quote_raw": "0",
  "fee_attribution": "FULL"
}
```

**Zuordnungsregel:** Transaktionsweite Gebühren (`network_fee_lamports`, `priority_fee_lamports`, `tip_lamports`) werden **vollständig dem Event mit `net_swap_index = 0` dieser Wallet in dieser Transaktion zugeordnet** (`fee_attribution = "FULL"`). Alle weiteren Events derselben Wallet und Transaktion tragen dort Nullwerte und `fee_attribution = "NONE"`.

Damit ist die Summe über alle Events exakt die tatsächliche Transaktionsgebühr – keine Doppelzählung, kein Verlust. `venue_fee_*` und `transfer_fee_*` sind dagegen pro Swap spezifisch und stehen am jeweiligen Event.

#### 4.1.9 `decode_confidence` [FEST]

- `COMPLETE` – alle Instruktionen im Pfad wurden erkannt, der Nettoeffekt ist vollständig.
- `PARTIAL` – mindestens eine Instruktion wurde nicht erkannt, der Nettoeffekt könnte unvollständig sein.
- `UNKNOWN` – der Swap wurde nur heuristisch aus Balance-Änderungen abgeleitet.

**Nur `COMPLETE` ist wirtschaftlich verwertbar.** `PARTIAL` und `UNKNOWN` gehen in Quarantäne, werden gezählt und senken die berichtete Datenabdeckung. Sie werden niemals stillschweigend verworfen und niemals als korrekter Trade behandelt.

#### 4.1.10 Quote-Priorität – welches Token ist `base`? [FEST]

Feste Rangliste. Das in der Transaktion vorkommende Token mit dem **niedrigsten Rang** wird `quote_mint`, das andere `base_mint`:

1. `So11111111111111111111111111111111111111112` (Wrapped SOL)
2. `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (USDC)
3. `Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB` (USDT)

Kommt keines dieser Token vor, ist es ein **Token-zu-Token-Swap**: `quote_mint` ist dann die lexikografisch kleinere der beiden Adressen, und das Event erhält in der Quarantäneprüfung von Plan A zusätzlich das Label `token_to_token`. Solche Swaps werden für die PnL-Rechnung gesondert behandelt und nicht mit SOL-Paaren vermischt.

**Native SOL und Wrapped SOL gelten als dasselbe Quote-Asset** und werden immer als `So11111111111111111111111111111111111111112` geführt. Wrap und Unwrap sind kein Swap.

---

### 4.2 `WalletCandidate` v1.0.0

| Feld | Typ | Bedeutung |
|---|---|---|
| `schema_version` | string | `"1.0.0"` |
| `chain` | string | `"solana"` |
| `wallet` | string | Base58 |
| `sources` | array | mindestens 1 Eintrag, siehe unten |
| `first_seen_utc` | string | früheste Sichtung über alle Quellen |
| `last_seen_utc` | string | späteste Sichtung |
| `labels` | array[string] | freie Labels aus Quellen, **niemals bewertend verwendet** |
| `reach` | object \| null | optional, siehe A7 |
| `snapshot_id` | string | Snapshot, in dem dieser Kandidat entstand |

Eintrag in `sources`:

```json
{
  "source_id": "solana_tracker.top_traders_30d",
  "source_name": "Solana Tracker",
  "fetched_at_utc": "2026-06-01T12:00:00.000Z",
  "source_window_start_utc": "2026-05-02T00:00:00Z",
  "source_window_end_utc": "2026-06-01T00:00:00Z",
  "rank": 17,
  "reported_pnl_usd": "48213.55",
  "reported_pnl_sol": null,
  "labels": ["smart_money"],
  "raw_ref": "raw/solana_tracker/2026-06-01T12-00-00Z/page_002.json"
}
```

**Verbindliche Regel:** `rank`, `reported_pnl_*` und `labels` sind **Herkunftsdaten**. Sie dürfen in keiner Score-Berechnung, keinem Filter und keiner Gewichtung vorkommen. Sie dienen ausschließlich der Kandidatengenerierung und der späteren Baseline „ungefiltertes Kopieren der Ranglisten-Top-Wallets". Ein Test in `tests/research/` weist nach, dass der Score-Code diese Felder nicht liest.

---

### 4.3 `WalletStats` v1.0.0

Struktur mit vier Blöcken. **Jede Kennzahl trägt ihre Stichprobengröße.** Eine Kennzahl ohne `n` ist ungültig.

```json
{
  "schema_version": "1.0.0",
  "wallet": "<base58>",
  "window": { "label": "A", "start_utc": "...", "end_utc": "...",
              "start_slot": 0, "end_slot": 0 },
  "coverage": {
    "transactions_seen": 0, "transactions_decoded_complete": 0,
    "transactions_partial": 0, "transactions_unknown": 0,
    "coverage_ratio": "0.00", "unknown_trade_ratio": "0.00",
    "has_pre_window_holdings": false, "open_positions_at_end": 0
  },
  "activity": {
    "closed_positions": 0, "reconstructed_positions": 0,
    "active_days": 0, "trades_per_active_day": "0.00",
    "first_trade_utc": null, "last_trade_utc": null
  },
  "performance": {
    "realized_pnl_sol": "0", "realized_pnl_usd": "0",
    "roi_per_closed_trade_median": "0.00",
    "expectancy_sol": "0", "win_rate": "0.00", "n_wins": 0, "n_losses": 0,
    "profit_factor": "0.00", "median_trade_sol": "0",
    "max_drawdown_sol": "0", "max_drawdown_ratio": "0.00",
    "recovery_days": null,
    "pnl_excl_best_trade_sol": "0",
    "pnl_excl_best_1pct_sol": "0", "pnl_excl_best_5pct_sol": "0",
    "profitable_subwindows": 0, "total_subwindows": 0,
    "hold_seconds_p10": 0, "hold_seconds_p50": 0, "hold_seconds_p90": 0,
    "fast_trade_pnl_ratio_under_5min": "0.00",
    "fast_trade_pnl_ratio_under_10min": "0.00",
    "seconds_to_first_partial_sell_p50": 0,
    "bootstrap_ci": { "metric": "expectancy_sol",
                      "lower": "0", "upper": "0", "iterations": 10000 }
  },
  "concentration": {
    "top_token_pnl_ratio": "0.00", "distinct_tokens": 0,
    "typical_position_size_sol": "0", "max_position_size_sol": "0",
    "entry_pool_liquidity_sol_p50": "0", "exit_pool_liquidity_sol_p50": "0"
  },
  "exclusions": [
    { "reason_code": "TOO_FEW_CLOSED_TRADES",
      "evidence": { "closed_positions": 4, "threshold": 30 },
      "threshold_version": "1.0.0" }
  ]
}
```

**Regel:** `realized_pnl_*` und unrealisierter PnL werden niemals addiert. Offene Positionen am Fensterende erscheinen ausschließlich in `coverage.open_positions_at_end`, nie in `performance`.

**Reason Codes für Ausschlüsse** (Enum, Erweiterung per CCR):
`MEV_ARBITRAGE_PATTERN`, `MARKET_MAKING_PATTERN`, `EXTREME_TX_FREQUENCY`, `SAME_SLOT_SNIPER`, `PRE_LAUNCH_SUPPLY_SUSPECTED`, `WASH_TRADING`, `INSUFFICIENT_COVERAGE`, `TOO_FEW_CLOSED_TRADES`, `TOO_FEW_ACTIVE_DAYS`, `ILLIQUID_POSITIONS`, `PNL_FROM_TRANSFERS_OR_AIRDROPS`, `UNKNOWN_COST_BASIS_DOMINANT`, `HOLD_TIME_INCOMPATIBLE_WITH_LATENCY`, `NEGATIVE_COPY_EXPECTANCY`, `RESULT_DEPENDS_ON_OUTLIERS`

---

### 4.4 `TraderSelection` v1.0.0

**Das zentrale Übergabeartefakt von A nach B.**

```json
{
  "schema_version": "1.0.0",
  "selection_id": "sel_2026-06-01T12-00-00Z_a1b2c3d4",
  "created_at_utc": "2026-06-01T12:00:00.000Z",
  "data_snapshot_id": "snap_2026-05-31_helius_v1",
  "code_commit": "<40-stellige-git-sha>",
  "params_hash": "<sha256-hex>",
  "score_version": "1.0.0",
  "selection_window": { "label": "A", "start_utc": "...", "end_utc": "...",
                        "start_slot": 0, "end_slot": 0 },
  "evaluation_window": { "label": "B", "start_utc": "...", "end_utc": "...",
                         "start_slot": 0, "end_slot": 0 },
  "wallets": [
    {
      "wallet": "<base58>",
      "status": "ACTIVE",
      "rank": 1,
      "score": "0.00",
      "weight": "0.05",
      "max_weight": "0.10",
      "reason_codes": [],
      "risks": ["LOW_EXIT_LIQUIDITY"],
      "coverage_ratio": "0.94",
      "metrics_ref": "reports/2026-06-01/wallet_<base58>.json",
      "copy_pnl_by_latency": {
        "1s": "0", "3s": "0", "5s": "0", "15s": "0", "60s": "0"
      }
    }
  ],
  "applied_filters": [
    { "filter_id": "min_closed_trades", "threshold": "30",
      "threshold_version": "1.0.0", "wallets_removed": 141 }
  ],
  "manual_approval": {
    "approved": true,
    "approved_by": "<name>",
    "approved_at_utc": "2026-06-01T13:00:00.000Z",
    "note": "freigegeben fuer Paper"
  },
  "expires_at_utc": "2026-06-15T00:00:00.000Z"
}
```

`status` Enum: `ACTIVE`, `OBSERVE_ONLY`, `SUSPENDED`.
Nur `ACTIVE` darf bei B einen `CopyIntent` erzeugen. `OBSERVE_ONLY` wird beobachtet und protokolliert, führt aber immer zu `NO_TRADE` mit Grund `wallet_not_selected`.

**Ablehnungspflicht für Plan B** – B muss die Selektion fail-closed zurückweisen bei:

1. unbekannter Major-Version
2. `manual_approval.approved != true`
3. `expires_at_utc` in der Vergangenheit
4. Checksummen-Abweichung gegenüber dem Manifest
5. doppelter Wallet-Adresse in `wallets`
6. ungültiger Base58-Adresse
7. `weight > max_weight` oder Summe der Gewichte > 1
8. leerem `code_commit` oder `params_hash`

Eine **leere** `wallets`-Liste ist gültig. Sie erlaubt Beobachtung, erzeugt aber keine Intents. Der Gesamtplan sagt ausdrücklich: eine leere Selektion ist ein zulässiges Ergebnis.

**Bei fehlerhaftem Reload behält B die letzte gültige Selektion.** Aktivierung ist atomar: erst vollständig validieren, dann umschalten.

---

### 4.5 `CopyIntent` v1.0.0

Erzeugt von Plan B, gelesen von Plan A zur Auswertung.

| Feld | Typ | Bedeutung |
|---|---|---|
| `schema_version` | string | `"1.0.0"` |
| `intent_id` | string | UUIDv7 oder deterministischer Hash, eindeutig |
| `source_event_id` | string | `event_id` des auslösenden `SwapEvent` |
| `source_wallet` | string | Base58 |
| `created_at_utc` | string | ms-genau |
| `base_mint`, `quote_mint` | string | Base58 |
| `side` | string | `BUY` \| `SELL` |
| `target_size_quote_raw` | string | Zielgröße in Quote-Rohbetrag |
| `target_fraction_bps` | integer \| null | bei SELL: Anteil des Copy-Bestands in bps |
| `max_slippage_bps` | integer | > 0 |
| `expires_at_utc` | string | ms-genau |
| `mode` | string | `RESEARCH` \| `OBSERVE` \| `PAPER` \| `SHADOW` \| `LIVE` |
| `decision` | string | `EXECUTE` \| `NO_TRADE` |
| `reason_code` | string \| null | bei `NO_TRADE` Pflicht, Enum siehe Plan B §5 |
| `selection_id` | string | die zum Entscheidungszeitpunkt aktive Selektion |
| `policy_version` | string | Version der SignalPolicy |
| `dedup_policy` | string | `FIRST_SIGNAL_ONLY` \| `CONSENSUS_REQUIRED` \| `INCREMENT_WITH_CAP` |

**Invariante:** eine `source_event_id` erzeugt höchstens einen Intent mit `decision = "EXECUTE"`. `NO_TRADE`-Einträge werden vollständig protokolliert, weil Plan A daraus die tatsächliche Signalhäufigkeit und die Ablehnungsgründe auswertet.

---

### 4.6 `ExecutionResult` v1.0.0

```json
{
  "schema_version": "1.0.0",
  "result_id": "<uuid>",
  "intent_id": "<uuid>",
  "mode": "PAPER",
  "status": "FILLED",
  "quote": {
    "provider": "jupiter",
    "requested_at_utc": "...", "expires_at_utc": "...",
    "in_amount_raw": "0", "out_amount_raw": "0",
    "price_impact_bps": 0, "route_hops": 2,
    "platform_fee_raw": "0"
  },
  "expected_price": "0", "realized_price": "0",
  "filled_base_raw": "0", "filled_quote_raw": "0",
  "fees": { "network_fee_lamports": "0", "priority_fee_lamports": "0",
            "tip_lamports": "0", "venue_fee_raw": "0" },
  "latencies_ms": {
    "block_to_receive": 0, "receive_to_decode": 0,
    "decode_to_decision": 0, "decision_to_quote": 0,
    "quote_to_submit": 0, "submit_to_confirm": null
  },
  "signature": null,
  "error_code": null,
  "error_detail": null,
  "created_at_utc": "...", "finalized_at_utc": "..."
}
```

`status` Enum: `FILLED`, `PARTIAL`, `FAILED`, `EXPIRED`, `REJECTED`, `UNKNOWN`.
`error_code` Enum (Erweiterung per CCR): `NO_ROUTE`, `QUOTE_EXPIRED`, `SLIPPAGE_EXCEEDED`, `INSUFFICIENT_LIQUIDITY`, `SIMULATED_FAIL`, `TOKEN_RESTRICTED`, `PROVIDER_TIMEOUT`, `PROVIDER_ERROR`, `STATE_MISMATCH`.

**`status = "UNKNOWN"` ist ein Sperrzustand**, kein Ergebnis. Er blockiert Folgeaktionen für diesen Token, bis eine Reconciliation ihn auflöst. Er wird niemals automatisch als `FAILED` umgedeutet und niemals mit einer erneuten Order beantwortet.

Die Feldnamen unter `latencies_ms` sind **exakt** die Stufen aus Plan B §B2. Plan A liest genau diese Namen in A10, um den eingefrorenen Backtest mit gemessenen Verteilungen neu zu rechnen. Eine Umbenennung ist ein Breaking Change.

---

### 4.7 Ergänzungen gegenüber `00_GESAMTPLAN.md` §5

Zur vollen Transparenz – dies sind Felder, die der Gesamtplan nicht namentlich aufführt und die ich ergänzt habe. Sie **entfernen oder benennen nichts um**, was §5 vorschreibt:

| Schema | Ergänztes Feld | Grund |
|---|---|---|
| `SwapEvent` | `chain` | §5.1 verlangt Chain in der `event_id`-Ableitung, listet sie aber nicht als Feld. Ohne Feld nicht reproduzierbar. |
| `SwapEvent` | `net_swap_index` | §5.1 nennt ihn in der `event_id`-Ableitung, nicht in der Feldliste. |
| `SwapEvent` | `source_provider` | Providerwechsel muss laut §A2 sichtbar sein. |
| `SwapEvent` | `fees` | §A4 verlangt Gebührenzuordnung, §5.1 hat kein Feld dafür. |
| `SwapEvent` | `decode_confidence` | §5.1 verlangt „unbekannte Instruktionen messbar protokollieren"; ohne Feld nicht am Event auswertbar. |
| `TraderSelection` | `applied_filters[].wallets_removed` | §6 verlangt einen Funnel mit Ausschlusszahlen. |
| `CopyIntent` | `target_fraction_bps` | Plan B §B4 verlangt anteilige Verkäufe; eine absolute Zielgröße kann das nicht ausdrücken. |
| `ExecutionResult` | `status = "UNKNOWN"` | §B9/§B10 verlangen einen nicht-terminalen Sperrzustand. |

**Diese Ergänzungen bedürfen der ausdrücklichen Freigabe durch den Projektinhaber, bevor Code entsteht.**

---

## 5. Aggregationsregeln (`contracts/AGGREGATION.md`)

Diese Regeln entscheiden, ob Integrationspunkt I2 besteht. Beide Decoder implementieren sie identisch. [FEST]

1. **Netto pro Tupel.** Ein `SwapEvent` beschreibt den Nettoeffekt pro `(signature, wallet, base_mint, quote_mint)`.
2. **Router-Hops kollabieren.** Bei einer Multi-Hop-Route zählt ausschließlich, was die Wallet abgegeben und was sie erhalten hat. Zwischentoken erzeugen **kein** Event. `hops` dokumentiert die Anzahl.
3. **Kauf und Verkauf desselben Mints in einer Transaktion** werden saldiert. Ist das Netto exakt 0, entsteht **kein** Event; der Vorgang wird als `WASH_SAME_TX` gezählt.
4. **Ein Transfer ist kein Swap.** Ein SPL-Transfer ohne beteiligtes Swap-Programm erzeugt nie ein `SwapEvent`. Er wird als `TokenTransfer` separat protokolliert, weil Plan A ihn für die Bestandsinvariante braucht.
5. **Wrapped SOL.** Wrap und Unwrap sind kein Swap. Native SOL und wSOL sind dasselbe Quote-Asset.
6. **Mehrere Token-Paare in einer Transaktion** erzeugen mehrere Events, unterschieden über `net_swap_index`.
7. **Token-2022 Transfer Fees.** Der gespeicherte Rohbetrag ist **immer der von der Wallet tatsächlich erhaltene bzw. abgegebene Betrag** nach Transfer Fee. Die Fee steht separat in `fees.transfer_fee_*`.
8. **Fehlgeschlagene Transaktion** erzeugt kein wirtschaftlich wirksames Event (siehe 4.1.7).
9. **Unbekanntes Programm im Pfad.** Es wird **kein** Teil-Netto-Event ausgegeben. Die Transaktion geht für diese Wallet vollständig in Quarantäne mit `decode_confidence` ungleich `COMPLETE`. Halbe Wahrheiten sind gefährlicher als Lücken.
10. **LP-Aktionen** (Add/Remove Liquidity) sind kein Swap und erzeugen kein Event.
11. **Reihenfolge.** Events werden sortiert nach `(slot, transaction_index, net_swap_index)`. Ist `transaction_index` null, tritt `signature` lexikografisch an dessen Stelle. **Die Sortierung darf das Ergebnis nie verändern** – das ist ein Property-Test.

---

## 6. Golden Fixtures

**Ablage:** `contracts/golden/<case_id>/` mit

- `raw_transaction.json` – unveränderte Providerantwort, Zugangsdaten entfernt
- `expected_events.json` – Liste erwarteter `SwapEvent`s bzw. leere Liste
- `README.md` – was der Fall prüft und warum

**Beide Repos führen denselben Test** `tests/contracts/test_golden.py`, der den jeweils eigenen Decoder gegen `expected_events.json` prüft. Bestehen beide, ist I2 nachgewiesen.

### 6.1 Pflichtfälle [FEST]

| # | Fall | Erwartung |
|---|---|---|
| 1 | Pump.fun Buy, direkt | 1 Event, `side=BUY` |
| 2 | Pump.fun Sell, direkt | 1 Event, `side=SELL` |
| 3 | Raydium CPMM Swap | 1 Event |
| 4 | Jupiter 2-Hop | 1 Event, `hops=2`, kein Zwischentoken |
| 5 | Jupiter 3-Hop mit exotischem Zwischentoken | 1 Event, Zwischentoken nicht sichtbar |
| 6 | Buy und Sell desselben Mints in einer Transaktion | saldiert; bei Netto 0 kein Event |
| 7 | Zwei beobachtete Wallets in einer Transaktion | 2 Events, je Wallet eins |
| 8 | Token-2022 mit Transfer Fee | Rohbetrag nach Fee, Fee separat |
| 9 | Wrap SOL + Swap in einer Transaktion | 1 Event, kein Wrap-Event |
| 10 | Fehlgeschlagene Transaktion | kein wirtschaftliches Event |
| 11 | Reiner SPL-Transfer | kein Event, `TokenTransfer` protokolliert |
| 12 | LP Add | kein Event |
| 13 | Airdrop ohne Kauf | kein Event, kein erfundener Einstandspreis |
| 14 | Bonding-Curve-Migration zu Pool | kein Swap-Event für die Migration selbst |
| 15 | Unbekanntes Programm im Pfad | Quarantäne, `decode_confidence != COMPLETE` |
| 16 | Zwei verschiedene Token-Paare in einer Transaktion | 2 Events, `net_swap_index` 0 und 1 |
| 17 | Teilverkauf 30 % | 1 Event mit korrektem Rohbetrag |
| 18 | Wash-Trade im selben Slot | erkannt und markiert |

### 6.2 Wer liefert sie [FEST]

Die Fälle 1–5, 10 und 11 werden **initial von Hand** aus öffentlich einsehbaren Transaktionen gebaut, damit B nicht auf A wartet. Wer zuerst Providerzugang hat, baut sie und öffnet den PR. Die restlichen Fälle liefert Plan A aus dem Backfill nach.

**Bis mindestens die Fälle 1–5, 10 und 11 grün sind, gilt weder A3 noch B3 als abgenommen.**

---

## 7. Artefakt-Übergabe

### 7.1 Dateinamen [FEST]

| Von | Nach | Pfad |
|---|---|---|
| A | intern | `artifacts/candidates/candidates_<ISO8601-kompakt>.jsonl` |
| A | **B** | `artifacts/selections/<selection_id>.json` |
| A | intern | `artifacts/reports/<run_id>/report.md` |
| B | **A** | `artifacts/observations/latency_<ISO8601-kompakt>.jsonl` |
| B | **A** | `artifacts/observations/paper_fills_<ISO8601-kompakt>.jsonl` |
| B | **A** | `artifacts/observations/decoder_coverage_<ISO8601-kompakt>.json` |

`<ISO8601-kompakt>` = `2026-06-01T12-00-00Z` (Doppelpunkte durch Bindestrich ersetzt, weil Windows sie in Dateinamen nicht erlaubt).

### 7.2 Manifest [FEST]

Zu **jeder** Artefaktdatei gehört `<dateiname>.manifest.json`:

```json
{
  "schema_version": "1.0.0",
  "artifact_type": "trader_selection",
  "file_name": "sel_2026-06-01T12-00-00Z_a1b2c3d4.json",
  "file_bytes": 48213,
  "content_sha256": "<hex>",
  "record_schema_version": "1.0.0",
  "record_count": 37,
  "producer": { "plan": "A", "component": "research.selection", "version": "1.0.0" },
  "git_commit": "<40-stellige-sha>",
  "contracts_commit": "<40-stellige-sha>",
  "config_hash": "<sha256-hex>",
  "created_at_utc": "2026-06-01T12:00:00.000Z",
  "data_snapshot_id": "snap_2026-05-31_helius_v1",
  "observation_window": null
}
```

**Regeln:**

- Der Leser prüft `content_sha256` **vor** jeder Verwendung. Abweichung = fail-closed.
- `contracts_commit` ist Pflicht. So ist jederzeit belegbar, gegen welche Schemaversion das Artefakt entstand.
- **Atomares Schreiben:** in `<name>.tmp` schreiben, `fsync`, dann `os.replace` auf den Zielnamen. Erst danach das Manifest schreiben. Ein Leser, der eine Datei ohne Manifest findet, behandelt sie als unvollständig.
- Artefakte sind **unveränderlich**. Eine Korrektur ist eine neue Datei mit neuem Zeitstempel, nie ein Überschreiben.

---

## 8. Betriebsmodi

### 8.1 Modi [FEST]

`RESEARCH`, `OBSERVE`, `PAPER`, `SHADOW`, `LIVE` – Bedeutung wie Gesamtplan §9.

### 8.2 Auflösung [FEST]

- Gelesen aus der Umgebungsvariable `GUNDIX_MODE`.
- **Fehlt sie oder ist der Wert ungültig:** Plan A fällt auf `RESEARCH` zurück. Plan B **bricht den Start ab**.
- `LIVE` **kann nicht** über eine Konfigurationsdatei gesetzt werden. Es erfordert zusätzlich `GUNDIX_LIVE_CONFIRM=<selection_id>` mit exakter Übereinstimmung zur geladenen Selektion.
- Es gibt keinen Codepfad, der einen Modus **erhöht**. Ein Fehler kann den Modus nur herabstufen oder den Prozess beenden.
- Ein Test in beiden Repos weist nach: leere Umgebung führt niemals zu `LIVE`.

### 8.3 Schlüsseltrennung [FEST]

- Plan A hat **keinen Codepfad**, der einen privaten Schlüssel lädt. Kein Signer-Import, keine Keypair-Konstruktion, kein `Keypair.from_*` irgendwo im Repo. Ein Grep-Test in CI erzwingt das.
- Plan B lädt den Signer **ausschließlich** im Modus `LIVE`. In `PAPER` und `SHADOW` ist das Signer-Modul technisch nicht importierbar.
- Schlüssel liegen außerhalb beider Repos. `.env` steht in `.gitignore`. Es existiert nur `.env.example` mit leeren Werten.
- Keine Exception, kein Log, kein Artefakt und kein Test-Fixture enthält jemals einen Schlüssel oder einen vollständigen Provider-Token.

---

## 9. Test- und Qualitätsstandard

### 9.1 Verbindliche Testarten [FEST]

| Art | Regel |
|---|---|
| Unit | Kein Netzwerk, keine Uhr, keine Zufälligkeit ohne festen Seed |
| Property (`hypothesis`) | Für jede Invariante aus Abschnitt 9.2 |
| Golden | Gegen `contracts/golden/`, in beiden Repos identisch |
| Integration | Markiert `@pytest.mark.integration`, ohne Keys automatisch übersprungen |
| Negativ | Zu jedem Happy Path mindestens ein bewusst gebrochener Fall |

### 9.2 Invarianten, die als Property-Test vorliegen müssen [FEST]

**Beide Pläne:**

- Dieselben Eingabedaten in zufälliger Reihenfolge erzeugen dasselbe Ergebnis.
- Dieselbe Transaktion erzeugt in Backfill und Live dieselbe `event_id`.
- Kein Rohbetrag ist negativ.
- Summe der zugeordneten Transaktionsgebühren über alle Events einer Transaktion = tatsächliche Transaktionsgebühr.

**Plan A:**

- Bestand = Käufe + Transfers hinein − Verkäufe − Transfers hinaus, soweit die Datenabdeckung reicht.
- Kein negativer Tokenbestand ohne markierte Datenlücke.
- Keine doppelte Verarbeitung derselben `event_id`.
- Daten nach dem Ende von Fenster A verändern den Fenster-A-Score nicht. **Dies ist der wichtigste Look-ahead-Test des Projekts.**
- Die Summe der Teilverkäufe überschreitet nie den kopierbaren Bestand.

**Plan B:**

- Eine `event_id` erzeugt höchstens einen `EXECUTE`-Intent.
- Eine `intent_id` erzeugt höchstens einen wirtschaftlich wirksamen Fill.
- Verkaufte Copy-Menge ≤ vorhandene Copy-Menge.
- Tageslimits überleben einen Neustart.
- Ein Prozessabbruch an beliebiger Stelle erzeugt weder Verlust noch Doppel-Fill.

### 9.3 Definition von „fertig" pro Arbeitspaket [FEST]

Ein Arbeitspaket gilt erst als abgeschlossen, wenn **alle vier** Punkte erfüllt sind:

1. Der **echte Produktionspfad** wurde ausgeführt – nicht nur ein Mock.
2. Ein **positiver Fall** ist grün.
3. Die **relevanten Negativfälle** sind grün.
4. `ruff check`, `ruff format --check` und `mypy --strict` sind grün.

Ein bestandener Mock-Test ist kein Beweis für Funktionsfähigkeit. Das ist ausdrücklich festgehalten, weil es die häufigste Selbsttäuschung in solchen Projekten ist.

### 9.4 Verbotene Aussagen ohne Evidenz [FEST]

Weder im Code, in Kommentaren, in Berichten noch in der Kommunikation mit dem Menschen:

„beste Wallets", „konstant profitabel", „kopierbar", „live-ready", „funktioniert", „fertig"

**Stattdessen:** Zeitraum, Stichprobengröße, Kostenannahmen, Konfidenzintervall und was noch nicht bewiesen ist.

Wenn eine Instanz gefragt wird, ob etwas funktioniert, lautet die zulässige Antwort entweder „ja, nachgewiesen durch <konkreter Test/Lauf>" oder „nicht nachgewiesen".

---

## 10. Was strikt verboten ist

### 10.1 Grenzverletzungen [FEST]

- A schreibt niemals in `src/stream`, `src/paper`, `src/execution` oder deren Tests.
- B schreibt niemals in `src/discovery`, `src/research`, `src/backtest` oder deren Tests.
- Keine Instanz editiert `contracts/` direkt. Nur per CCR.

### 10.2 Fachliche Verbote [FEST]

- **B baut keine zweite Scoring-Logik.** B entscheidet nie, ob eine Wallet gut ist.
- **A sendet keine Orders** und implementiert keinen Broker.
- Keine duplizierten Pydantic-Modelle für dieselbe Domäne.
- Keine Interpretation von JSON ohne Schema-Validierung.
- Keine gemeinsame veränderliche Datei, in die beide Prozesse gleichzeitig schreiben.
- Kein automatisches wöchentliches Nachziehen der neuesten Ranglisten-Gewinner.
- Ranglisten-PnL wird niemals ungeprüft übernommen.

### 10.3 Zum gegenseitigen Leserecht [FEST]

Beide Instanzen dürfen das Repo der jeweils anderen **lesen**. Das dient dazu, das Gegenüber zu verstehen und Reconciliation vorzubereiten.

**Es erlaubt ausdrücklich nicht:**

- den Import von Code aus dem anderen Repo,
- das Kopieren von Modellen oder Hilfsfunktionen,
- sich auf ein internes Verhalten des anderen Plans zu verlassen, das nicht in diesem Vertrag steht.

**Die einzige verbindliche Schnittstelle sind die Schemas in `contracts/` und die Artefakte aus Abschnitt 7.** Wenn eine Instanz beim Lesen des anderen Repos etwas findet, das ihr für die Integration nötig erscheint, aber hier nicht steht, ist das ein CCR – kein Anlass, es einfach nachzubauen.

---

## 11. Änderungsprozess für Contracts (CCR)

**CCR = Contract Change Request.** Der einzige zulässige Weg, `contracts/` zu ändern. [FEST]

### 11.1 Ablauf

1. Die Instanz, die eine Änderung braucht, legt im Repo `gundix-contracts` einen PR mit dem Titel `CCR-<laufende-Nummer>: <kurzbeschreibung>` an.
2. Der PR-Text enthält zwingend:
   - **Was** sich ändert (Feld, Enum, Regel)
   - **Warum** es ohne die Änderung nicht geht
   - **Ob** es ein Breaking Change ist
   - **Welche** Golden Fixtures angepasst werden müssen
   - **Was** die jeweils andere Seite nachziehen muss
3. Die andere Instanz kommentiert: einverstanden, oder Einwand mit Begründung.
4. **Der Mensch merged.** Keine Instanz merged selbst.
5. Nach dem Merge zieht **jede** Seite das Submodule auf den neuen Tag und passt ihren Code an. Bis beide Seiten nachgezogen haben, gilt der Integrationsstand als gebrochen und wird in `INTERFACE_STATUS.md` so ausgewiesen.

### 11.2 Versionierung [FEST]

- Semver pro Schema: `MAJOR.MINOR.PATCH`
- **MAJOR** – Feld entfernt, umbenannt, Typ geändert, Bedeutung geändert, Enum-Wert entfernt
- **MINOR** – optionales Feld ergänzt, Enum-Wert ergänzt
- **PATCH** – nur Dokumentation
- **Leser lehnen unbekannte MAJOR-Versionen fail-closed ab.** Sie versuchen keine Migration und raten nicht.
- Ein unbekannter MINOR-Wert wird akzeptiert, aber protokolliert.

### 11.3 Bis zum Contract Freeze (I1) [FEST]

Vor I1 sind Änderungen billig und erwünscht. **Nach I1 ist jeder Breaking Change ein Vorgang, der beide Seiten aufhält.** Deshalb: lieber jetzt gründlich diskutieren als später migrieren.

---

## 12. Kommunikation zwischen den Instanzen

Die beiden Instanzen sprechen nicht direkt miteinander. Sie kommunizieren über drei Kanäle. [FEST]

### 12.1 `INTERFACE_STATUS.md`

Liegt in **jedem** Plan-Repo im Wurzelverzeichnis. Wird bei **jedem** abgeschlossenen Arbeitspaket aktualisiert.

```markdown
# INTERFACE_STATUS – Plan A

Stand: 2026-06-01T12:00:00Z
Contracts-Commit: <sha>  Tag: v1.0.0

## Ich produziere
| Artefakt | Schema | Version | Status |
|---|---|---|---|
| TraderSelection | trader_selection | 1.0.0 | STUB – synthetisch, nicht aus echten Daten |

## Ich konsumiere
| Artefakt | Schema | Version | Status |
|---|---|---|---|
| paper_fills | execution_result | 1.0.0 | ERWARTET – noch nichts erhalten |

## Fertige Arbeitspakete
A0 abgenommen. A1 in Arbeit.

## Offene CCRs
CCR-3 – wartet auf Antwort von B

## Was B über mich wissen muss
Die Selection unter artifacts/selections/ ist bis auf Weiteres synthetisch
und traegt manual_approval.approved = false. Sie darf keine Intents erzeugen.
```

**Regel:** Jede Instanz liest zu Beginn jeder Arbeitssitzung das `INTERFACE_STATUS.md` der anderen Seite. Das ist der Mechanismus, der „nahtlos zusammenpassen" praktisch herstellt.

### 12.2 CCR-PRs

Siehe Abschnitt 11.

### 12.3 Der Mensch

Alles, was keine der beiden Instanzen allein entscheiden kann. Beide Instanzen fragen aktiv nach, statt eine Annahme zu treffen und weiterzubauen. Eine falsche Annahme, auf der zwei Wochen Arbeit stehen, ist teurer als eine Rückfrage.

---

## 13. Arbeitsanweisungen für die Instanzen

### 13.1 Für Claude B (dieser Text wird B als Prompt gegeben)

> Du bist Claude-Instanz B im Projekt GundiX, einem Solana-Copy-Trading-System, das von zwei Claude-Instanzen parallel entwickelt wird. Du baust Plan B: Live-Beobachtung, Paper-Trading und die spätere, zunächst gesperrte Trade-Ausführung.
>
> **Lies vor jeder Arbeit vollständig, in dieser Reihenfolge:**
> `00_GESAMTPLAN.md`, `02_PLAN_B_TRADE_EXECUTION.md`, `03_S0_INTEGRATIONSVERTRAG.md` (dieses Dokument), alle Dateien in `contracts/`, sowie `INTERFACE_STATUS.md` im Repo `gundix-plan-a`.
>
> **Dein Eigentumsbereich** ist ausschließlich `src/stream`, `src/paper`, `src/execution` und die zugehörigen Tests in `gundix-plan-b`. Du schreibst nie in Plan-A-Verzeichnisse und nie direkt in `contracts/`. Änderungen an Contracts laufen ausschließlich über den CCR-Prozess in Abschnitt 11.
>
> Du liest das Repo `gundix-plan-a`, um Plan A zu verstehen. Du importierst daraus **keinen Code** und verlässt dich auf **kein** Verhalten, das nicht in diesem Vertrag steht.
>
> **Halte dich ausnahmslos** an die Zahl- und Zeitregeln in Abschnitt 3, die Schemas in Abschnitt 4, die Aggregationsregeln in Abschnitt 5 und die Modusregeln in Abschnitt 8.
>
> Implementiere B0 bis B10 in dieser Reihenfolge. Beginne mit synthetischen Selections und Golden-Replay-Daten – du wartest nicht auf echte Wallets von Plan A. Wenn A eine echte Selektion liefert, tauschst du nur das Artefakt, nicht die Architektur.
>
> **Nach jedem Arbeitspaket** prüfst du den echten Produktionspfad, einen positiven Fall, die relevanten Negativfälle, Idempotenz und Neustartverhalten. Erst dann gilt es als fertig. Du behebst Abweichungen sofort, bevor du weitergehst. Aktualisiere danach `INTERFACE_STATUS.md`.
>
> **Baue keine zweite Scoring-Logik.** Du entscheidest nie, ob eine Wallet gut ist – das ist ausschließlich Plan A. Du entscheidest auch nicht, ob die Backtest-Evidenz ausreicht.
>
> **Halte Paper und Shadow technisch von der Signierung getrennt.** In diesen Modi darf das Signer-Modul nicht einmal importierbar sein. Ein fehlender Modus aktiviert niemals Live.
>
> **Behaupte keine Funktions- oder Live-Bereitschaft ohne Evidenz.** Die verbotenen Formulierungen in Abschnitt 9.4 gelten für dich wörtlich. Wenn du unsicher bist, sagst du das.
>
> **Frage nach, statt anzunehmen.** Wenn etwas in diesem Vertrag fehlt, widersprüchlich ist oder du eine Entscheidung treffen müsstest, die Plan A betrifft: frag den Menschen. Baue nicht auf einer Vermutung weiter.

### 13.2 Für Claude A

Identisch aufgebaut, mit vertauschten Bereichen: Eigentumsbereich `src/discovery`, `src/research`, `src/backtest`; Reihenfolge A0 bis A10; Leserecht auf `gundix-plan-b`; zusätzlich die Verbote „sendet keine Orders" und „lädt niemals einen privaten Schlüssel"; zusätzlich die wissenschaftlichen Mindeststandards aus Gesamtplan §10.

---

## 14. Offene Punkte – Entscheidung durch den Projektinhaber

Diese Punkte kann keine der Instanzen allein entscheiden. **Punkt 1 bis 4 blockieren den Start.**

| # | Punkt | Warum es blockiert |
|---|---|---|
| 1 | **Freigabe dieses Dokuments**, insbesondere der Ergänzungen in 4.7 | Ohne Freigabe entsteht Code auf einer nicht abgestimmten Grundlage. |
| 2 | **Repo-Namen und Organisation** auf GitHub; Anlage der drei Repos und Vergabe der Leserechte | Ohne Repos kein Submodule, ohne Submodule keine gemeinsamen Contracts. |
| 3 | **Python 3.12 auf beiden Rechnern**, `uv` installiert | Aktuell ist auf dem A-Rechner nur Python 3.14.7 vorhanden und `uv` fehlt. Unterschiedliche Versionen machen `uv.lock` wertlos. |
| 4 | **Datenprovider und Budget** – wer hat welche Zugänge? Kandidatenquellen (Solana Tracker, Birdeye, Vybe, Dune) und historischer Backfill (z. B. Helius) | Ohne Zugänge kann A1/A2 keinen echten Datenpfad prüfen. A0 ist auch ohne möglich. Kostenpflichtige Anmeldungen macht der Mensch selbst. |
| 5 | **Fenstergrenzen A, B und optional C** – konkrete Start- und Endzeitpunkte | Muss **vor der ersten Datensichtung** festgelegt und eingefroren werden. Wird es später gesetzt, ist es Look-ahead und das Ergebnis wertlos. Vorschlag: A = 90 abgeschlossene Tage, B = die anschließenden 45 Tage, Abstand zum Jetzt ≥ 2 Tage für Finalität. |
| 6 | **Wer ist `approved_by`** in `manual_approval` | Die Selektion braucht eine namentliche menschliche Freigabe. |
| 7 | **Startkapital, maximale Positionsgröße, Tageslimits** für die Portfolio-Simulation in A8 | A8 kann ohne diese Zahlen nicht rechnen. Sie müssen dieselben sein, die B später als Limits verwendet. |
| 8 | **Dedup-Policy Phase 1** – Vorschlag `FIRST_SIGNAL_ONLY` | Betrifft A8 (Simulation) und B4 (Umsetzung) gleichermaßen und muss identisch sein. |

---

## 15. Meilensteine

| ID | Meilenstein | Bedingung |
|---|---|---|
| **S0** | Startpunkt | Repos angelegt, Contracts v1.0.0 getaggt, Skelett steht, CI grün, Golden-Fälle 1–5, 10, 11 vorhanden |
| **I1** | Contract Freeze | Schemas dokumentiert, Python-Typen dagegen getestet, Aggregationsregeln beschlossen, Contract-Tests grün |
| **I2** | Parser-Reconciliation | A-Parser und B-Decoder erzeugen auf allen 18 Golden-Fällen identische Events. Keine Toleranz, keine Rundung. |
| **I3** | Artefakt-Handshake | B lädt eine von A erzeugte Test-Selektion ohne Sondercode. Ungültige Versionen, Checksummen und Adressen werden abgelehnt. Hot Reload ist atomar. |
| **I4** | End-to-End Paper | Aufgezeichnete Transaktion -> Decoder -> SwapEvent -> Watchlist -> CopyIntent -> Paper-Fill -> ExecutionResult -> Auswertung durch A. Geprüft für Buy, Teilverkauf, vollständigen Exit, Duplikat, verspätetes Event, illiquiden Token und Neustart. |
| **I5** | Shadow-Kalibrierung | Mehrere Wochen Beobachtung, Latenz und Coverage quantifiziert, Backtest mit echten Verteilungen neu gerechnet |
| **I6** | Live Go/No-Go | Alle Kriterien aus Gesamtplan §11 erfüllt. **Live bleibt gesperrt, solange auch nur eines nicht erfüllt ist.** |

**S0 ist gemeinsam. Ab I1 arbeiten A und B unabhängig. I2 bis I4 sind wieder gemeinsam.**

---

## 16. Schlussbemerkung

Der Zweck dieses Dokuments ist nicht Bürokratie. Er ist, dass in einigen Monaten eine aufgezeichnete Solana-Transaktion durch beide Systeme läuft und **auf beiden Seiten dieselbe Zahl** herauskommt – ohne Adapter, ohne Umrechnungsskript, ohne „bei uns heißt das Feld anders".

Alles, was hier festgelegt ist, ist deshalb festgelegt, weil es sonst zwei plausible Antworten gäbe und beide Instanzen unabhängig voneinander die jeweils andere wählen würden.

**Wenn etwas fehlt: fragen. Nicht annehmen.**
