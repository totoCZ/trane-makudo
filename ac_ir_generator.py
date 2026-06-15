"""
AC IR Code Generator
====================
Generates Tuya-format IR codes for a Qunda-compatible split AC unit.
Protocol fully reverse-engineered from Tuya Zigbee IR blaster captures.

Usage:
    python ac_ir_generator.py --temp 24 --mode cool --fan auto
    python ac_ir_generator.py --temp 25 --mode economy
    python ac_ir_generator.py --power off
    python ac_ir_generator.py --all
    python ac_ir_generator.py --json > codes.json
"""

import base64
import struct
import argparse
import json

# ---------------------------------------------------------------------------
# Protocol constants (NEC-based IR timing)
# ---------------------------------------------------------------------------

HEADER_MARK  = 9000   # µs
HEADER_SPACE = 4500   # µs
BIT_MARK     = 576    # µs
ZERO_SPACE   = 576    # µs
ONE_SPACE    = 1662   # µs
FINAL_MARK   = 576    # µs

# B0 encodes power state in the low nibble, remote variant in the high nibble
B0_ON  = 0x68   # power ON  (lo=8)
B0_OFF = 0x60   # power OFF (lo=0)

# B1 high nibble: encodes mode + fan speed
MODE_FAN_NIBBLE = {
    ("fan",  "auto"): 0x0,
    ("fan",  "high"): 0x1,
    ("fan",  "mid"):  0x2,
    ("fan",  "low"):  0x3,
    ("cool", "auto"): 0x4,
    ("cool", "high"): 0x5,
    ("cool", "mid"):  0x6,   # inferred
    ("cool", "low"):  0x7,
    ("dry",  "auto"): 0x8,
    ("dry",  "high"): 0x9,
    ("dry",  "mid"):  0xA,   # inferred
    ("dry",  "low"):  0xB,   # inferred
    ("heat", "auto"): 0xC,   # inferred, untested
    ("heat", "high"): 0xD,   # inferred, untested
    ("heat", "mid"):  0xE,   # inferred, untested
    ("heat", "low"):  0xF,   # inferred, untested
}

FAN_B6 = {"auto": 0x07, "high": 0x02, "mid": 0x01, "low": 0x00}

VALID_MODES  = ("cool", "fan", "dry", "heat", "economy")
VALID_FANS   = ("auto", "high", "mid", "low")
VALID_POWER  = ("on", "off")
TEMP_MIN     = 16
TEMP_MAX     = 30


# ---------------------------------------------------------------------------
# Core formula
# ---------------------------------------------------------------------------

def _nibsum(byte: int) -> int:
    return (byte >> 4) + (byte & 0x0F)


def _compute_b5(b0: int, b1: int, b6: int, b7: int, target: int) -> int:
    """Compute B5 so nibsum(B0,B1,B5,B6,B7) == target. Prefers lo nibble = 5."""
    needed = target - _nibsum(b0) - _nibsum(b1) - _nibsum(b6) - _nibsum(b7)
    if not (0 <= needed <= 30):
        raise ValueError(
            f"Cannot satisfy nibsum={target}: needed B5 nibsum={needed} "
            f"(B0={b0:02X} B1={b1:02X} B6={b6:02X} B7={b7:02X})"
        )
    b5_lo = 5 if needed >= 5 else needed
    b5_hi = needed - b5_lo
    return (b5_hi << 4) | b5_lo


def make_payload(
    temp_c: int,
    mode: str = "cool",
    fan: str = "auto",
    power: str = "on",
    sweep: bool = True,
) -> list[int]:
    """
    Build the 8-byte IR payload.

    Args:
        temp_c: Target temperature in Celsius (16–30).
        mode:   "cool", "fan", "dry", "heat", or "economy".
        fan:    "auto", "high", "mid", or "low".
                Ignored in economy mode (AC enforces quiet fan internally).
        power:  "on" or "off". Controls B0 power bit.
                OFF sends current settings with power-off flag — the AC
                remembers the state for next power-on.
        sweep:  Louver flag. Only meaningful for wall-mount units;
                ducted units ignore it.

    Returns:
        List of 8 integers (raw payload bytes).
    """
    if not (TEMP_MIN <= temp_c <= TEMP_MAX):
        raise ValueError(f"Temperature {temp_c}°C out of range ({TEMP_MIN}–{TEMP_MAX})")
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown mode '{mode}'. Valid: {VALID_MODES}")
    if fan not in VALID_FANS:
        raise ValueError(f"Unknown fan speed '{fan}'. Valid: {VALID_FANS}")
    if power not in VALID_POWER:
        raise ValueError(f"Unknown power state '{power}'. Valid: {VALID_POWER}")

    b0 = B0_ON if power == "on" else B0_OFF

    if mode == "economy":
        b1 = 0x40 | (temp_c - 15)   # B1hi=4 (cool base), B1lo=temp nibble
        b6 = 0x27                     # economy flag in hi nibble
        b7 = 0x02 if sweep else 0x06  # bit 2 = louver off
        target = 47
    else:
        key = (mode, fan)
        if key not in MODE_FAN_NIBBLE:
            raise ValueError(f"Unsupported mode/fan combination: {mode}/{fan}")
        b1 = (MODE_FAN_NIBBLE[key] << 4) | (temp_c - 15)
        b6 = FAN_B6[fan]
        b7 = 0x0B - b6   # confirmed: B6 + B7 = 0x0B always
        target = 63 if mode == "dry" else 47

    b5 = _compute_b5(b0, b1, b6, b7, target)
    return [b0, b1, 0x00, 0x00, 0x00, b5, b6, b7]


# ---------------------------------------------------------------------------
# IR pulse encoding
# ---------------------------------------------------------------------------

def payload_to_durations(payload: list[int]) -> list[int]:
    """Convert 8-byte payload to IR mark/space durations (µs). MSB first."""
    d = [HEADER_MARK, HEADER_SPACE]
    for byte in payload:
        for bit_idx in range(8):
            bit = (byte >> (7 - bit_idx)) & 1
            d.append(BIT_MARK)
            d.append(ONE_SPACE if bit else ZERO_SPACE)
    d.append(FINAL_MARK)
    return d


# ---------------------------------------------------------------------------
# FastLZ level-1 compression
# ---------------------------------------------------------------------------

def _fastlz1_compress(data: bytes) -> bytes:
    output = bytearray()
    i = 0
    n = len(data)
    while i < n:
        best_len = 0
        best_off = 0
        ws = max(0, i - 8192)
        for j in range(ws, i):
            ml = 0
            while (i + ml < n and ml < 264 and j + ml < i
                   and data[j + ml] == data[i + ml]):
                ml += 1
            if ml >= 3 and ml > best_len:
                best_len, best_off = ml, i - j - 1
        if best_len >= 3:
            ln = best_len - 2
            output.append(((min(ln, 7)) << 5) | (best_off >> 8))
            if ln >= 7:
                output.append(ln - 7)
            output.append(best_off & 0xFF)
            i += best_len
        else:
            rs = i
            rl = 0
            while rl < 32 and i < n:
                found = False
                for j in range(max(0, i - 8192), i):
                    ml = 0
                    while (i + ml < n and ml < 264 and j + ml < i
                           and data[j + ml] == data[i + ml]):
                        ml += 1
                    if ml >= 3:
                        found = True
                        break
                if found and rl > 0:
                    break
                rl += 1
                i += 1
            output.append(rl - 1)
            output.extend(data[rs:rs + rl])
    return bytes(output)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_tuya_code(
    temp_c: int,
    mode: str = "cool",
    fan: str = "auto",
    power: str = "on",
    sweep: bool = True,
) -> str:
    """
    Generate a Tuya-format IR code string for the given AC settings.

    Returns:
        Base64-encoded Tuya IR code, ready for zigbee2mqtt ir_code_to_send.
    """
    payload    = make_payload(temp_c, mode, fan, power, sweep)
    durations  = payload_to_durations(payload)
    raw        = struct.pack(f"<{len(durations)}H", *durations)
    compressed = _fastlz1_compress(raw)
    return base64.b64encode(compressed).decode()


def generate_all_codes() -> dict:
    """
    Generate codes for all valid combinations.

    Returns:
        Nested dict: codes[power][mode][fan][temp_c] = tuya_code_string
    """
    codes = {}
    for power in VALID_POWER:
        codes[power] = {}
        for mode in VALID_MODES:
            codes[power][mode] = {}
            fans = ["auto"] if mode == "economy" else VALID_FANS
            for fan in fans:
                codes[power][mode][fan] = {}
                for temp in range(TEMP_MIN, TEMP_MAX + 1):
                    try:
                        codes[power][mode][fan][temp] = make_tuya_code(
                            temp, mode, fan, power
                        )
                    except ValueError:
                        pass
    return codes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(
        description="Generate Tuya IR codes for a Qunda-compatible AC unit."
    )
    parser.add_argument("--temp",  type=int, default=25,
                        help=f"Temperature °C ({TEMP_MIN}–{TEMP_MAX}), default 25")
    parser.add_argument("--mode",  choices=VALID_MODES, default="cool")
    parser.add_argument("--fan",   choices=VALID_FANS, default="auto")
    parser.add_argument("--power", choices=VALID_POWER, default="on")
    parser.add_argument("--sweep", action=argparse.BooleanOptionalAction, default=True,
                        help="Louver sweep (wall units only)")
    parser.add_argument("--all",   action="store_true",
                        help="Print codes for every valid combination")
    parser.add_argument("--json",  action="store_true",
                        help="Output all codes as JSON")
    parser.add_argument("--payload", action="store_true",
                        help="Also show the raw 8-byte payload")
    args = parser.parse_args()

    if args.json or args.all:
        codes = generate_all_codes()
        if args.json:
            print(json.dumps(codes, indent=2))
        else:
            for power in VALID_POWER:
                for mode in VALID_MODES:
                    fans = ["auto"] if mode == "economy" else VALID_FANS
                    for fan in fans:
                        for temp in range(TEMP_MIN, TEMP_MAX + 1):
                            code = codes.get(power,{}).get(mode,{}).get(fan,{}).get(temp)
                            if code:
                                print(f"{power:<3}  {temp}°C  {mode:<8}  {fan:<4}  {code}")
    else:
        try:
            code = make_tuya_code(args.temp, args.mode, args.fan, args.power, args.sweep)
            print(f"Code: {code}")
            if args.payload:
                p = make_payload(args.temp, args.mode, args.fan, args.power, args.sweep)
                print(f"Payload: {' '.join(f'{b:02X}' for b in p)}")
        except ValueError as e:
            print(f"Error: {e}")
            raise SystemExit(1)


if __name__ == "__main__":
    _cli()
