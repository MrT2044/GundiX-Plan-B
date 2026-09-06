# CCR-003: `venue`-Enum – fehlende Venues und Multi-Venue-Routen

**Eingereicht von:** Claude-Instanz B
**Betrifft:** `03_S0_INTEGRATIONSVERTRAG.md` §4.1.5
**Breaking Change:** nein – nur Enum-Ergänzungen, MINOR nach §11.2
**Status:** offen

---

## 1. Problem A: Eine Route über mehrere Venues hat keinen gültigen Wert

§5 Regel 2 verlangt, dass eine Multi-Hop-Route zu **einem** Event kollabiert. Jupiter routet
regelmäßig über mehrere Venues gleichzeitig, etwa Pump.fun **und** Raydium in derselben
Instruktion. Das entstehende Event braucht dann genau einen `venue`-Wert, und keiner der
vorhandenen ist richtig.

Aktuelles Verhalten von Plan B: `venue = UNKNOWN`, dadurch zwingend
`decode_confidence = UNKNOWN` und damit Quarantäne. Das ist vertragskonform und
fail-closed, verwirft aber gerade die Routen, die ein Aggregator-Nutzer am häufigsten
erzeugt.

In der Stichprobe vom 2026-09-06 traf das auf 1 von 2 erfolgreichen Jupiter-Transaktionen zu.

**Vorschlag:** Enum-Wert `MIXED` mit der Bedeutung „aggregierter Netto-Swap, dessen Legs
über mehrere Venues liefen". Ein `MIXED`-Event ist wirtschaftlich verwertbar, wenn die
übrigen Bedingungen erfüllt sind. `pool` ist dann zwingend `null`, weil sich der Nettoeffekt
keinem einzelnen Pool zuordnen lässt.

**Wenn abgelehnt:** Bitte ausdrücklich bestätigen, dass Multi-Venue-Routen dauerhaft
unverwertbar bleiben sollen. Plan A muss dieselbe Entscheidung treffen, sonst weicht der
Kandidaten-PnL im Backfill systematisch vom kopierbaren Ergebnis ab.

---

## 2. Problem B: Real gehandelte Venues ohne Enum-Wert

Diese Programme existieren auf Mainnet, sind verifiziert und wickeln echte Swaps
beobachtbarer Wallets ab, haben aber keinen Wert in §4.1.5:

| Programm | Vorgeschlagener Enum-Wert |
|---|---|
| `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` | `METEORA_DBC` (Dynamic Bonding Curve) |
| `2wT8Yq49kHgDzXuPxZSaeLaH1qbmGXtEyPy64bL7aD3c` | `LIFINITY_V2` |
| `PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY` | `PHOENIX` |
| `opnb2LAfJYbRMAHHvqjCwQxanZn7ReEHp1k81EohpZb` | `OPENBOOK_V2` |

Alle vier sind bei Plan B bereits als **bekannte** Programme registriert
(`VENUE_PROGRAMS_WITHOUT_ENUM_VALUE`), damit sie nicht fälschlich die Quarantäneregel für
unbekannte Programme auslösen. Ihre Swaps erhalten `venue = UNKNOWN` und bleiben damit
wirtschaftlich unverwertbar.

**Vorschlag:** Enum um die vier Werte erweitern. Ein Enum-Wert allein macht eine Venue noch
nicht handelbar – dafür bleibt ein Golden-Fixture-Nachweis je Venue erforderlich (§6). Er
macht aber den Unterschied sichtbar zwischen „wir kennen diese Venue und handeln sie
bewusst nicht" und „wir wissen nicht, was hier passiert ist".

---

## 3. Warum das nicht warten sollte

Solange beide Fälle auf `UNKNOWN` fallen, ist die Coverage-Metrik nicht interpretierbar:
Plan A kann aus `decoder_coverage_<ts>.json` nicht ablesen, ob eine niedrige Abdeckung
eine echte Decoder-Lücke ist (Kriterium für I6) oder nur eine fehlende Enum-Zeile.

---

## 4. Was die andere Seite nachziehen muss

- **Plan A:** dieselben Enum-Werte, dieselbe Zuordnung Programm → Venue. Eine abweichende
  Zuordnung bricht I2 auf jeder betroffenen Transaktion.
- **Golden Fixtures:** neue Fälle für jede zusätzlich unterstützte Venue, bevor sie als
  handelbar gilt.
