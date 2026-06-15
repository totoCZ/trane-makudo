# trane-makudo
Trane MCD control protocol for this refrigeeta

Reversed from Qunda and original remote (it's the same thing).

## it's hot in here, senpai!
<img width="360" height="640" alt="cool" src="https://github.com/user-attachments/assets/14484a01-abc2-4855-9c15-46df45e7fdca" />

# AC IR Protocol — Reverse Engineered

Reverse-engineered IR protocol for a **Trane / compatible split AC unit**, captured via a **Tuya Zigbee IR blaster** (ZS06/ZS08/TS1201 family) and decoded from scratch.

> Two remotes were tested (Qunda and original brand). They produce **identical IR frames** — one remote is enough for all captures.

---

## How Tuya Stores IR Codes

Tuya learned-codes are **not** proprietary database IDs. The format is:

```
Base64( FastLZ( u16le[] of µs pulse/space durations ) )
```

The blaster records the raw IR waveform at the hardware level and replays it verbatim. Protocol-agnostic.

### Frame structure
- **Header mark:** 9000 µs
- **Header space:** 4500 µs  
- **Bit mark:** 576 µs (constant)
- **`0` bit space:** 576 µs
- **`1` bit space:** 1662 µs
- **Final mark:** 576 µs
- **Bit order:** MSB first
- **Total data:** 64 bits (8 bytes)

---

## The 8-Byte Payload

```
[B0] [B1] [00] [00] [00] [B5] [B6] [B7]
```

### B0 — Device byte
Always `0x78`. Some older captures show `0x68`; the AC accepts both as long as the nibble-sum checksum holds.

### B1 — Mode + Temperature (the main byte)

```
B1 = [mode_nibble << 4] | [temp_nibble]
```

**Mode nibble (high):**

| Nibble | Mode |
|--------|------|
| `0x0`  | Fan only — auto speed |
| `0x1`  | Fan only — high speed |
| `0x2`  | Fan only — mid speed  |
| `0x3`  | Fan only — low speed  |
| `0x4`  | Cool — auto fan |
| `0x5`  | Cool — high fan |
| `0x6`  | Cool — mid fan *(inferred)* |
| `0x7`  | Cool — low fan |
| `0x8`  | Dry — auto fan |
| `0x9`  | Dry — high fan |
| `0xA`  | Dry — mid fan *(inferred)* |
| `0xB`  | Dry — low fan *(inferred)* |
| `0xC–F`| Heat modes *(inferred, untested)* |

**Temperature nibble (low):**

```
temp_nibble = temp_celsius - 15
```

| Temp | Nibble | | Temp | Nibble |
|------|--------|-|------|--------|
| 16°C | `0x1`  | | 24°C | `0x9`  |
| 17°C | `0x2`  | | 25°C | `0xA`  |
| 18°C | `0x3`  | | 26°C | `0xB`  |
| 19°C | `0x4`  | | 27°C | `0xC`  |
| 20°C | `0x5`  | | 28°C | `0xD`  |
| 21°C | `0x6`  | | 29°C | `0xE`  |
| 22°C | `0x7`  | | 30°C | `0xF`  |
| 23°C | `0x8`  | | | |

### B2, B3, B4 — Always `0x00`

### B5 — Nibble-sum filler

The entire frame's nibble sum must equal a target constant. B5 is set to make this work:

```
nibble_sum(all bytes) = 47  for cool / fan modes
nibble_sum(all bytes) = 63  for dry mode

B5_high = target - nibsum(B0) - nibsum(B1) - nibsum(B6) - nibsum(B7) - 5
B5 = (B5_high << 4) | 0x05
```

The low nibble of B5 is always `0x5`.

If `target=47` produces `B5_high` out of range `[0–15]`, use `target=63` instead (happens with high-mode-nibble + high-temp combinations).

### B6 — Fan speed

| Value  | Speed |
|--------|-------|
| `0x07` | Auto  |
| `0x02` | High  |
| `0x01` | Mid   |
| `0x00` | Low   |

### B7 — Checksum

```
B7 = 0x0B - B6
```

Confirmed across all 12 captured samples. **100% reliable.**

| B6 (fan) | B7 |
|----------|----|
| `0x07` auto | `0x04` |
| `0x02` high | `0x09` |
| `0x01` mid  | `0x0A` |
| `0x00` low  | `0x0B` |

---

## Confidence Levels

| Feature | Confidence | Verified |
|---------|-----------|---------|
| Temperature encoding | ✅ Certain | 6 samples across 3 remotes |
| Fan speed (B6) | ✅ Certain | 4 fan speeds confirmed |
| B7 checksum (B7 = 0x0B − B6) | ✅ Certain | 12/12 samples |
| B5 nibble-sum checksum | ✅ Certain | 11/12 samples |
| Cool mode (auto/high fan) | ✅ Certain | Multiple captures |
| Fan-only mode (all speeds) | ✅ Certain | All 4 speeds captured |
| Dry mode (auto/high fan) | ✅ Certain | 2 captures |
| Cool mid/low fan | ⚠️ Inferred | Formula consistent, 1 sample each |
| Dry mid/low fan | ⚠️ Inferred | Formula consistent, no capture |
| Heat mode | ❌ Unknown | No captures — needs testing |

---

## Files

| File | Description |
|------|-------------|
| `ac_ir_generator.py` | Standalone Python generator — produces Tuya base64 codes for any temp/mode/fan |
| `ac_climate_ha.yaml` | Home Assistant configuration: `climate` entity + `remote` actions via Zigbee IR blaster |
| `ac_pyscript_ha.py` | Optional pyscript for HA — exposes a callable service to send arbitrary AC commands |

---

## Captures Used

```
Remote A / B (identical protocol):
  23°C cool auto, 24°C cool auto
  24°C fan auto, 25°C fan auto
  25°C cool auto, 25°C cool high
  25°C fan auto/low/mid/high
  25°C dry auto, 25°C dry high
  26°C cool low, 26°C dry auto
```

---

## Economy Mode

Economy mode uses a separate encoding — **not** the B1 mode/fan table.

```
Frame: [B0][B1][00][00][00][B5][B6][B7]
```

| Byte | Value | Notes |
|------|-------|-------|
| **B0** | `0x78` | Same as always |
| **B1** | `0x40 \| (temp - 15)` | B1hi always `4` (cool base), B1lo = temp nibble |
| **B6** | `0x27` | Fixed. Hi nibble `2` = economy flag. Lo nibble `7` = auto fan field (AC forces low internally) |
| **B7** | `0x02` (sweep on) / `0x06` (sweep off) | Bit 2 toggles louver. Irrelevant for ducted units. |
| **B5** | nibsum filler | Same formula: `nibsum(all bytes) = 47` |

Economy mode is always cooling. The AC enforces quiet/low-power fan internally regardless of the fan field.

---

## B5 Formula (updated)

```python
needed = target - nibsum(B0) - nibsum(B1) - nibsum(B6) - nibsum(B7)
B5_lo  = 5 if needed >= 5 else needed   # prefer lo=5
B5_hi  = needed - B5_lo
B5     = (B5_hi << 4) | B5_lo
```

When `needed < 5` (rare — only seen in economy+sweep_off), the remote uses a different split. The AC only verifies the total nibble sum, so any valid split works.

---

## Captures Used (updated)

```
23°C economy, 25°C economy, 25°C economy sweep-off
25°C dry fan-high (confirms dry formula)
```

Total: 18 unique captures across 3 remotes and all major modes.
