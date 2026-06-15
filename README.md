# trane-makudo
Trane MCD control protocol for this refrigeeta

Reversed from Qunda and original remote (it's the same thing).

## it's hot in here, senpai!
<img width="360" height="640" alt="cool" src="https://github.com/user-attachments/assets/14484a01-abc2-4855-9c15-46df45e7fdca" />

# IR Protocol — Trane MCD/MCX (024-1064 Remote)

Reverse-engineered from Tuya Zigbee IR blaster captures. Implemented in `ac_ir_generator.py`.

-----

## Hardware Context

The Trane MCD/MCX cassette units sold in Southeast Asian markets use a **generic OEM control board** (Trane part `690428650001`, cross-compatible with `024-1821` / `024-1491`) possibly sourced from a Chinese OEM supplier — most likely Qunda or AUX family. The board carries a custom IR receiver ASIC with its own firmware; Trane does not publish the protocol. This document describes what was learned by capturing and reverse-engineering the signals.

The 024-1064 wireless remote is the standard handheld controller for this board family.

-----

## Physical Layer

NEC-derived pulse/space encoding at 38 kHz carrier.

|Parameter   |Value (µs)|
|------------|----------|
|Header mark |9000      |
|Header space|4500      |
|Bit mark    |576       |
|Zero space  |576       |
|One space   |1662      |
|Final mark  |576       |

Bits are transmitted **MSB first**. No repeat frame is used.

> **Relation to known protocols:** Timing is within ~1–2% of the `CarrierAc64` protocol in IRremoteESP8266 (header ~8940/4556, bit mark 503 µs).  Both use 8-byte payloads and nibble-sum checksums. This is likely convergent design — both sourcing IR receiver ASICs from the same small pool of Chinese OEM chip suppliers. The payload layout differs, so this is a distinct protocol.

-----

## Payload Structure

Every message is exactly **8 bytes**. Bytes are indexed B0–B7.

```
B0  B1  B2  B3  B4  B5  B6  B7
```

### B0 — Power + Remote Variant

|Value |Meaning      |
|------|-------------|
|`0x68`|Power **ON** |
|`0x60`|Power **OFF**|

High nibble (`0x6_`) identifies the remote variant. Low nibble carries the power state (`8` = on, `0` = off).

### B1 — Mode, Fan Speed, Temperature

```
  7   6   5   4   3   2   1   0
[ ——— high nibble ——— | ——— low nibble ——— ]
   mode+fan combo          temp - 15
```

**Low nibble:** `temperature_celsius - 15`. Valid range 16–30 °C → nibble values 1–15.

**High nibble:** combined mode + fan speed lookup:

|High nibble|Mode|Fan                        |
|-----------|----|---------------------------|
|`0x0`      |fan |auto                       |
|`0x1`      |fan |high                       |
|`0x2`      |fan |mid                        |
|`0x3`      |fan |low                        |
|`0x4`      |cool|auto                       |
|`0x5`      |cool|high                       |
|`0x6`      |cool|mid                        |
|`0x7`      |cool|low                        |
|`0x8`      |dry |auto                       |
|`0x9`      |dry |high                       |
|`0xA`      |dry |mid  *(inferred)*          |
|`0xB`      |dry |low  *(inferred)*          |
|`0xC`      |heat|auto *(inferred, untested)*|
|`0xD`      |heat|high *(inferred, untested)*|
|`0xE`      |heat|mid  *(inferred, untested)*|
|`0xF`      |heat|low  *(inferred, untested)*|

### B2, B3, B4 — Unused

Always `0x00` in all observed captures.

### B5 — Computed Checksum Byte

Calculated so that the **nibble sum** of B0, B1, B5, B6, B7 equals a fixed target:

|Mode      |Target|
|----------|------|
|dry       |63    |
|all others|47    |

```python
def _nibsum(byte):
    return (byte >> 4) + (byte & 0x0F)

needed = target - _nibsum(B0) - _nibsum(B1) - _nibsum(B6) - _nibsum(B7)
B5_lo = 5 if needed >= 5 else needed
B5_hi = needed - B5_lo
B5 = (B5_hi << 4) | B5_lo
```

The solver prefers `lo = 5` as a tiebreaker, matching observed remote behaviour.

### B6 — Fan Speed (secondary encoding)

|Value |Fan speed|
|------|---------|
|`0x07`|auto     |
|`0x02`|high     |
|`0x01`|mid      |
|`0x00`|low      |

### B7 — Louver / Swing

`B7 = 0x0B - B6` in all normal modes. This is a **hardware invariant** — the receiver firmware validates that `B6 + B7 == 0x0B`.

|B7 value|B6    |Meaning            |
|--------|------|-------------------|
|`0x04`  |`0x07`|swing on (auto fan)|
|`0x09`  |`0x02`|swing on (high fan)|
|`0x0A`  |`0x01`|swing on (mid fan) |
|`0x0B`  |`0x00`|swing on (low fan) |

Swing off adds 4 to B7 (sets bit 2), breaking the `0x0B` sum — only observed in economy mode captures.

-----

## Economy Mode

Economy is a special override state that bypasses the normal mode/fan matrix.

|Byte         |Value                                 |Notes                                           |
|-------------|--------------------------------------|------------------------------------------------|
|B1           |`0x40 | (temp - 15)`                  |high nibble fixed at 4 (cool base)              |
|B6           |`0x27`                                |economy flag in high nibble; breaks `B6+B7=0x0B`|
|B7           |`0x02` (sweep on) / `0x06` (sweep off)|                                                |
|nibsum target|47                                    |same as normal modes                            |

The remote enforces a quiet fan speed in economy mode.

-----

## Output Format

`ac_ir_generator.py` outputs **Tuya-format IR codes**: the raw µs duration array is packed as little-endian `uint16` values, compressed with FastLZ level-1, then base64-encoded. This format is ready for use with a Tuya Zigbee IR blaster via zigbee2mqtt (`ir_code_to_send`).

-----

## Relation to Known Open-Source Libraries

|Library                                                           |Trane MCD/MCX|Notes                                                                                             |
|------------------------------------------------------------------|-------------|--------------------------------------------------------------------------------------------------|
|[IRremoteESP8266](https://github.com/crankyoldgit/IRremoteESP8266)|❌ not present|Closest match structurally is `CarrierAc64` (same 8-byte payload, nibble checksum, similar timing)|
|[SmartIR](https://github.com/smartHomeHub/SmartIR)                |❌ not present|129 manufacturers, 357 climate codes — no Trane entries at all                                    |

This protocol is not documented or implemented elsewhere as of the time of writing.

-----

## Known Limitations

- Heat mode nibble assignments (`0xC`–`0xF` in B1 high nibble) are **inferred** by extrapolation from the cool/dry/fan pattern. They have not been validated against a unit with a heat pump.
- Swing/louver behaviour on **ducted** MCD units is ignored by the board; the sweep flag only affects wall-mount variants.
- Timer and sleep functions, if supported by the board, have not been observed or reverse-engineered.
