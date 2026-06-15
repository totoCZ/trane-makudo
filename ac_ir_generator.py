"""
AC IR Code Generator
====================
Generates Tuya-format IR codes for a Qunda-compatible split AC unit.
Protocol reverse-engineered from captured IR blaster codes.

Usage:
    python ac_ir_generator.py
    python ac_ir_generator.py --temp 24 --mode cool --fan auto
    python ac_ir_generator.py --all          # print full code table
    python ac_ir_generator.py --json         # output JSON map
"""

import base64
import struct
import argparse
import json

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

HEADER_MARK  = 9000   # µs
HEADER_SPACE = 4500   # µs
BIT_MARK     = 576    # µs
ZERO_SPACE   = 576    # µs
ONE_SPACE    = 1662   # µs
FINAL_MARK   = 576    # µs

# B1 high nibble encodes mode + fan speed together
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

FAN_B6 = {
    "auto": 0x07,
    "high": 0x02,
    "mid":  0x01,
    "low":  0x00,
}

VALID_MODES = ("cool", "fan", "dry", "heat")
VALID_FANS  = ("auto", "high", "mid", "low")
TEMP_MIN    = 16
TEMP_MAX    = 30


# ---------------------------------------------------------------------------
# Core formula
# ---------------------------------------------------------------------------

def _nibsum(byte: int) -> int:
    """Sum of the two nibbles of a byte."""
    return (byte >> 4) + (byte & 0x0F)


def make_payload(temp_c: int, mode: str = "cool", fan: str = "auto") -> list[int]:
    """
    Build the 8-byte IR payload.

    Args:
        temp_c: Target temperature in Celsius (16–30).
        mode:   AC mode — "cool", "fan", "dry", or "heat".
        fan:    Fan speed — "auto", "high", "mid", or "low".

    Returns:
        List of 8 integers (the raw payload bytes).

    Raises:
        ValueError: On out-of-range or unknown arguments.
    """
    if not (TEMP_MIN <= temp_c <= TEMP_MAX):
        raise ValueError(f"Temperature {temp_c}°C out of range ({TEMP_MIN}–{TEMP_MAX})")
    if mode not in VALID_MODES:
        raise ValueError(f"Unknown mode '{mode}'. Valid: {VALID_MODES}")
    if fan not in VALID_FANS:
        raise ValueError(f"Unknown fan speed '{fan}'. Valid: {VALID_FANS}")

    key = (mode, fan)
    if key not in MODE_FAN_NIBBLE:
        raise ValueError(f"Unsupported mode/fan combination: {mode}/{fan}")

    b0 = 0x78
    b1 = (MODE_FAN_NIBBLE[key] << 4) | (temp_c - 15)
    b6 = FAN_B6[fan]
    b7 = 0x0B - b6  # checksum: B6 + B7 = 0x0B always

    # Nibble-sum checksum: total frame nibble sum must be 47 (cool/fan) or 63 (dry).
    # Try target=47 first; fall back to 63 if B5_high would be out of [0, 15].
    for target in (47, 63):
        b5_high = target - _nibsum(b0) - _nibsum(b1) - _nibsum(b6) - _nibsum(b7) - 5
        if 0 <= b5_high <= 15:
            break
    else:
        raise ValueError(
            f"Cannot produce valid B5 for {temp_c}°C {mode}/{fan}. "
            "This combination may not be supported."
        )

    b5 = (b5_high << 4) | 0x05  # low nibble is always 5

    return [b0, b1, 0x00, 0x00, 0x00, b5, b6, b7]


# ---------------------------------------------------------------------------
# IR pulse encoding
# ---------------------------------------------------------------------------

def payload_to_durations(payload: list[int]) -> list[int]:
    """
    Convert 8-byte payload to a list of IR mark/space durations (µs).
    Format: [header_mark, header_space, bit_mark, bit_space, ...., final_mark]
    """
    durations = [HEADER_MARK, HEADER_SPACE]
    for byte in payload:
        for bit_idx in range(8):          # MSB first
            bit = (byte >> (7 - bit_idx)) & 1
            durations.append(BIT_MARK)
            durations.append(ONE_SPACE if bit else ZERO_SPACE)
    durations.append(FINAL_MARK)
    return durations


# ---------------------------------------------------------------------------
# FastLZ level-1 compression
# ---------------------------------------------------------------------------

def _fastlz1_compress(data: bytes) -> bytes:
    """Minimal FastLZ level-1 compressor (LZ77 variant, 8 kB window)."""
    output = bytearray()
    i = 0
    n = len(data)

    while i < n:
        # Search for longest match in the look-back window
        best_len = 0
        best_off = 0
        window_start = max(0, i - 8192)

        for j in range(window_start, i):
            ml = 0
            while (i + ml < n and ml < 264 and j + ml < i
                   and data[j + ml] == data[i + ml]):
                ml += 1
            if ml >= 3 and ml > best_len:
                best_len = ml
                best_off = i - j - 1

        if best_len >= 3:
            length = best_len - 2
            if length < 7:
                ctrl = (length << 5) | (best_off >> 8)
            else:
                ctrl = (7 << 5) | (best_off >> 8)
            output.append(ctrl)
            if length >= 7:
                output.append(length - 7)
            output.append(best_off & 0xFF)
            i += best_len
        else:
            # Literal run (up to 32 bytes)
            run_start = i
            run_len = 0
            while run_len < 32 and i < n:
                # Stop early if a back-reference is available
                found = False
                ws = max(0, i - 8192)
                for j in range(ws, i):
                    ml = 0
                    while (i + ml < n and ml < 264 and j + ml < i
                           and data[j + ml] == data[i + ml]):
                        ml += 1
                    if ml >= 3:
                        found = True
                        break
                if found and run_len > 0:
                    break
                run_len += 1
                i += 1
            output.append(run_len - 1)
            output.extend(data[run_start:run_start + run_len])

    return bytes(output)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_tuya_code(temp_c: int, mode: str = "cool", fan: str = "auto") -> str:
    """
    Generate a Tuya-format IR code string for the given AC settings.

    Args:
        temp_c: Target temperature in Celsius (16–30).
        mode:   "cool", "fan", "dry", or "heat".
        fan:    "auto", "high", "mid", or "low".

    Returns:
        Base64-encoded Tuya IR code string, ready to send via
        zigbee2mqtt `ir_code_to_send` or Tuya local API.
    """
    payload   = make_payload(temp_c, mode, fan)
    durations = payload_to_durations(payload)
    raw       = struct.pack(f"<{len(durations)}H", *durations)
    compressed = _fastlz1_compress(raw)
    return base64.b64encode(compressed).decode()


def generate_all_codes() -> dict:
    """
    Generate codes for all valid combinations.

    Returns:
        Nested dict: codes[mode][fan][temp_c] = tuya_code_string
    """
    codes = {}
    for mode in VALID_MODES:
        codes[mode] = {}
        for fan in VALID_FANS:
            codes[mode][fan] = {}
            for temp in range(TEMP_MIN, TEMP_MAX + 1):
                try:
                    codes[mode][fan][temp] = make_tuya_code(temp, mode, fan)
                except ValueError:
                    pass  # skip unsupported combinations
    return codes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(
        description="Generate Tuya IR codes for a Qunda-compatible AC unit."
    )
    parser.add_argument("--temp", type=int, default=25,
                        help=f"Temperature in °C ({TEMP_MIN}–{TEMP_MAX}), default 25")
    parser.add_argument("--mode", choices=VALID_MODES, default="cool",
                        help="AC mode (default: cool)")
    parser.add_argument("--fan", choices=VALID_FANS, default="auto",
                        help="Fan speed (default: auto)")
    parser.add_argument("--all", action="store_true",
                        help="Print codes for every valid combination")
    parser.add_argument("--json", action="store_true",
                        help="Output all codes as JSON (implies --all)")
    parser.add_argument("--payload", action="store_true",
                        help="Also show the raw 8-byte payload")
    args = parser.parse_args()

    if args.json or args.all:
        codes = generate_all_codes()
        if args.json:
            print(json.dumps(codes, indent=2))
        else:
            for mode in VALID_MODES:
                for fan in VALID_FANS:
                    for temp in range(TEMP_MIN, TEMP_MAX + 1):
                        code = codes.get(mode, {}).get(fan, {}).get(temp)
                        if code:
                            print(f"{temp}°C  {mode:4}  {fan:4}  {code}")
    else:
        try:
            code = make_tuya_code(args.temp, args.mode, args.fan)
            print(f"Code: {code}")
            if args.payload:
                p = make_payload(args.temp, args.mode, args.fan)
                print(f"Payload: {' '.join(f'{b:02X}' for b in p)}")
        except ValueError as e:
            print(f"Error: {e}")
            raise SystemExit(1)


if __name__ == "__main__":
    _cli()
