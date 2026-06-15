"""
ac_pyscript_ha.py — AC IR Code Generator for Home Assistant pyscript
=====================================================================

Place this file in:  <HA config>/pyscript/ac_ir_generator.py

Supports multiple rooms — each with its own Tuya Zigbee IR blaster.
Add or rename rooms in the IR_BLASTERS dict below.

Services:
  pyscript.ac_send_command(room, temp, mode, fan, power, sweep)
  pyscript.ac_turn_off(room)
  pyscript.ac_set_temperature(room, temp)
  pyscript.ac_set_mode(room, mode)
  pyscript.ac_set_fan(room, fan)
"""

import base64
import struct

# ── Configuration ─────────────────────────────────────────────────────────────

# Map room name → Zigbee2MQTT friendly name of that room's IR blaster.
# Add as many rooms as you need.
IR_BLASTERS = {
    "living":  "zigbee2mqtt/living_room_ir/set",
    "bedroom": "zigbee2mqtt/bedroom_ir/set",
}

TEMP_MIN = 16
TEMP_MAX = 30

# ── Protocol constants ────────────────────────────────────────────────────────

HEADER_MARK  = 9000
HEADER_SPACE = 4500
BIT_MARK     = 576
ZERO_SPACE   = 576
ONE_SPACE    = 1662
FINAL_MARK   = 576

B0_ON  = 0x68
B0_OFF = 0x60

MODE_FAN_NIBBLE = {
    ("fan",  "auto"): 0x0, ("fan",  "high"): 0x1,
    ("fan",  "mid"):  0x2, ("fan",  "low"):  0x3,
    ("cool", "auto"): 0x4, ("cool", "high"): 0x5,
    ("cool", "mid"):  0x6, ("cool", "low"):  0x7,
    ("dry",  "auto"): 0x8, ("dry",  "high"): 0x9,
    ("dry",  "mid"):  0xA, ("dry",  "low"):  0xB,
}

FAN_B6 = {"auto": 0x07, "high": 0x02, "mid": 0x01, "low": 0x00}

# ── Core generator ────────────────────────────────────────────────────────────

def _nibsum(b):
    return (b >> 4) + (b & 0x0F)

def _compute_b5(b0, b1, b6, b7, target):
    needed = target - _nibsum(b0) - _nibsum(b1) - _nibsum(b6) - _nibsum(b7)
    if not (0 <= needed <= 30):
        raise ValueError(f"Cannot satisfy nibsum={target}: needed={needed}")
    b5_lo = 5 if needed >= 5 else needed
    return ((needed - b5_lo) << 4) | b5_lo

def _make_payload(temp_c, mode, fan, power="on", sweep=True):
    b0 = B0_ON if power == "on" else B0_OFF
    if mode == "economy":
        b1 = 0x40 | (temp_c - 15)
        b6 = 0x27
        b7 = 0x02 if sweep else 0x06
        target = 47
    else:
        key = (mode, fan)
        if key not in MODE_FAN_NIBBLE:
            raise ValueError(f"Unknown mode/fan: {mode}/{fan}")
        b1 = (MODE_FAN_NIBBLE[key] << 4) | (temp_c - 15)
        b6 = FAN_B6[fan]
        b7 = 0x0B - b6
        target = 63 if mode == "dry" else 47
    return [b0, b1, 0, 0, 0, _compute_b5(b0, b1, b6, b7, target), b6, b7]

def _payload_to_durations(payload):
    d = [HEADER_MARK, HEADER_SPACE]
    for byte in payload:
        for idx in range(8):
            bit = (byte >> (7 - idx)) & 1
            d.append(BIT_MARK)
            d.append(ONE_SPACE if bit else ZERO_SPACE)
    d.append(FINAL_MARK)
    return d

def _fastlz1_compress(data):
    data = bytes(data); out = bytearray(); i = 0; n = len(data)
    while i < n:
        best_len = 0; best_off = 0; ws = max(0, i - 8192)
        for j in range(ws, i):
            ml = 0
            while i+ml < n and ml < 264 and j+ml < i and data[j+ml] == data[i+ml]:
                ml += 1
            if ml >= 3 and ml > best_len:
                best_len, best_off = ml, i - j - 1
        if best_len >= 3:
            ln = best_len - 2
            out.append(((min(ln, 7)) << 5) | (best_off >> 8))
            if ln >= 7: out.append(ln - 7)
            out.append(best_off & 0xFF); i += best_len
        else:
            rs = i; rl = 0
            while rl < 32 and i < n:
                found = False
                for j in range(max(0, i - 8192), i):
                    ml = 0
                    while i+ml < n and ml < 264 and j+ml < i and data[j+ml] == data[i+ml]:
                        ml += 1
                    if ml >= 3: found = True; break
                if found and rl > 0: break
                rl += 1; i += 1
            out.append(rl - 1); out.extend(data[rs:rs+rl])
    return bytes(out)

def _make_tuya_code(temp_c, mode, fan, power="on", sweep=True):
    payload   = _make_payload(temp_c, mode, fan, power, sweep)
    durations = _payload_to_durations(payload)
    raw       = struct.pack(f"<{len(durations)}H", *durations)
    return base64.b64encode(_fastlz1_compress(raw)).decode()

def _get_topic(room):
    room = str(room).lower().strip()
    topic = IR_BLASTERS.get(room)
    if not topic:
        raise ValueError(
            f"Unknown room '{room}'. "
            f"Valid rooms: {list(IR_BLASTERS.keys())}. "
            f"Add new rooms to IR_BLASTERS in ac_pyscript_ha.py."
        )
    return topic

def _state_input(room, entity_type, entity_name, fallback):
    """Read an input_* helper scoped to a room, e.g. input_number.ac_living_temperature"""
    entity_id = f"{entity_type}.ac_{room}_{entity_name}"
    val = state.get(entity_id)
    return val if val is not None else fallback

# ── Pyscript services ─────────────────────────────────────────────────────────

@service
def ac_send_command(room="living", temp=25, mode="cool", fan="auto", power="on", sweep=True):
    """
    Send a full AC command to a specific room's IR blaster.

    Parameters
    ----------
    room  : str   Room name — must match a key in IR_BLASTERS.
    temp  : int   Temperature °C (16–30).
    mode  : str   cool | fan | dry | economy
    fan   : str   auto | high | mid | low
    power : str   on | off
    sweep : bool  Louver flag (wall units only; ducted ignores it).
    """
    room  = str(room).lower().strip()
    temp  = int(temp)
    mode  = str(mode).lower().strip()
    fan   = str(fan).lower().strip()
    power = str(power).lower().strip()
    sweep = bool(sweep)

    if mode == "fan_only":
        mode = "fan"

    # Validate
    try:
        topic = _get_topic(room)
    except ValueError as e:
        log.error(f"ac_send_command: {e}"); return

    if not (TEMP_MIN <= temp <= TEMP_MAX):
        log.error(f"ac_send_command: temp {temp} out of range"); return
    if mode not in ("cool", "fan", "dry", "economy"):
        log.error(f"ac_send_command: unknown mode '{mode}'"); return
    if fan not in ("auto", "high", "mid", "low"):
        log.error(f"ac_send_command: unknown fan '{fan}'"); return
    if power not in ("on", "off"):
        log.error(f"ac_send_command: unknown power '{power}'"); return

    try:
        ir_code = _make_tuya_code(temp, mode, fan, power, sweep)
    except ValueError as e:
        log.error(f"ac_send_command: {e}"); return

    payload_bytes = _make_payload(temp, mode, fan, power, sweep)
    log.info(
        f"ac_send_command: [{room}] power={power} {temp}°C {mode}/{fan} "
        f"→ [{' '.join(f'{b:02X}' for b in payload_bytes)}] → {topic}"
    )
    mqtt.publish(topic=topic, payload=f'{{"ir_code_to_send":"{ir_code}"}}')


@service
def ac_turn_off(room="living"):
    """Power off the AC in the given room."""
    room = str(room).lower().strip()
    try:
        _get_topic(room)  # validate early
    except ValueError as e:
        log.error(f"ac_turn_off: {e}"); return

    temp = int(float(_state_input(room, "input_number", "temperature", 25)))
    mode = _state_input(room, "input_select", "mode", "cool")
    fan  = _state_input(room, "input_select", "fan",  "auto")
    ac_send_command(room=room, temp=temp, mode=mode, fan=fan, power="off")


@service
def ac_set_temperature(room="living", temp=25):
    """Change temperature for a room, keep current mode/fan."""
    room = str(room).lower().strip()
    mode = _state_input(room, "input_select", "mode", "cool")
    fan  = _state_input(room, "input_select", "fan",  "auto")
    ac_send_command(room=room, temp=int(temp), mode=mode, fan=fan, power="on")


@service
def ac_set_mode(room="living", mode="cool"):
    """Change mode for a room, keep current temperature/fan."""
    room = str(room).lower().strip()
    temp = int(float(_state_input(room, "input_number", "temperature", 25)))
    fan  = _state_input(room, "input_select", "fan", "auto")
    ac_send_command(room=room, temp=temp, mode=mode, fan=fan, power="on")


@service
def ac_set_fan(room="living", fan="auto"):
    """Change fan speed for a room, keep current temperature/mode."""
    room = str(room).lower().strip()
    temp = int(float(_state_input(room, "input_number", "temperature", 25)))
    mode = _state_input(room, "input_select", "mode", "cool")
    ac_send_command(room=room, temp=temp, mode=mode, fan=fan, power="on")


# ── MQTT climate bridge ───────────────────────────────────────────────────────
#
# climate.living_room_ac / climate.bedroom_ac are proper MQTT climate entities
# defined in packages/ac_control.yaml. Pyscript's role here is:
#   1. Publish retained state to ha/ac/<room>/state on startup + helper changes
#   2. Handle pyscript.ac_handle_cmd called by yaml automations when the MQTT
#      climate entity sends a command to ha/ac/<room>/cmd/<field>

import json as _json

CLIMATE_ROOMS = {
    "living": {
        "temp_entity":  "input_number.ac_living_temperature",
        "mode_entity":  "input_select.ac_living_mode",
        "fan_entity":   "input_select.ac_living_fan",
        "power_entity": "input_boolean.ac_living_power",
        "temp_sensor":  "sensor.living_room_sensor_temperature",
        "state_topic":  "ha/ac/living/state",
    },
    "bedroom": {
        "temp_entity":  "input_number.ac_bedroom_temperature",
        "mode_entity":  "input_select.ac_bedroom_mode",
        "fan_entity":   "input_select.ac_bedroom_fan",
        "power_entity": "input_boolean.ac_bedroom_power",
        "temp_sensor":  "sensor.0xa4c13810c4936fde_temperature",
        "state_topic":  "ha/ac/bedroom/state",
    },
}


def _publish_state(room, cfg):
    power    = state.get(cfg["power_entity"])
    mode_val = state.get(cfg["mode_entity"]) or "cool"
    fan_val  = state.get(cfg["fan_entity"])  or "auto"
    temp_val = state.get(cfg["temp_entity"])
    cur_raw  = state.get(cfg["temp_sensor"])

    hvac_mode = "off" if power == "off" else ("fan_only" if mode_val == "fan" else mode_val)

    try:
        target_temp = float(temp_val) if temp_val else 25.0
    except (ValueError, TypeError):
        target_temp = 25.0

    payload = {"mode": hvac_mode, "temperature": target_temp, "fan_mode": fan_val}
    try:
        if cur_raw not in (None, "unknown", "unavailable"):
            payload["current_temperature"] = float(cur_raw)
    except (ValueError, TypeError):
        pass

    mqtt.publish(topic=cfg["state_topic"], payload=_json.dumps(payload), retain=True)


@time_trigger("startup")
def ac_climate_startup():
    for room, cfg in CLIMATE_ROOMS.items():
        _publish_state(room, cfg)
    log.info("ac_climate: MQTT state published")


@state_trigger(
    "input_boolean.ac_living_power",
    "input_select.ac_living_mode",
    "input_select.ac_living_fan",
    "input_number.ac_living_temperature",
    "sensor.living_room_sensor_temperature",
)
def ac_living_sync(**kwargs):
    _publish_state("living", CLIMATE_ROOMS["living"])


@state_trigger(
    "input_boolean.ac_bedroom_power",
    "input_select.ac_bedroom_mode",
    "input_select.ac_bedroom_fan",
    "input_number.ac_bedroom_temperature",
    "sensor.0xa4c13810c4936fde_temperature",
)
def ac_bedroom_sync(**kwargs):
    _publish_state("bedroom", CLIMATE_ROOMS["bedroom"])


@service
def ac_handle_cmd(room="living", cmd="mode", value="off"):
    """Called by yaml automations when the MQTT climate entity sends a command."""
    room = str(room).lower().strip()
    cfg  = CLIMATE_ROOMS.get(room)
    if not cfg:
        log.error(f"ac_handle_cmd: unknown room '{room}'"); return

    temp  = int(float(state.get(cfg["temp_entity"]) or 25))
    mode  = state.get(cfg["mode_entity"]) or "cool"
    fan   = state.get(cfg["fan_entity"])  or "auto"
    power = state.get(cfg["power_entity"]) or "off"

    if cmd == "mode":
        hvac_mode = str(value).strip()
        if hvac_mode == "off":
            input_boolean.turn_off(entity_id=cfg["power_entity"])
            ac_send_command(room=room, temp=temp, mode=mode, fan=fan, power="off")
        else:
            ir_mode = "fan" if hvac_mode == "fan_only" else hvac_mode
            input_boolean.turn_on(entity_id=cfg["power_entity"])
            input_select.select_option(entity_id=cfg["mode_entity"], option=ir_mode)
            mode = ir_mode
            ac_send_command(room=room, temp=temp, mode=ir_mode, fan=fan, power="on")

    elif cmd == "temperature":
        temp = int(float(value))
        input_number.set_value(entity_id=cfg["temp_entity"], value=float(temp))
        if power == "on":
            ac_send_command(room=room, temp=temp, mode=mode, fan=fan, power="on")

    elif cmd == "fan_mode":
        fan = str(value).strip()
        input_select.select_option(entity_id=cfg["fan_entity"], option=fan)
        if power == "on":
            ac_send_command(room=room, temp=temp, mode=mode, fan=fan, power="on")

    _publish_state(room, cfg)
