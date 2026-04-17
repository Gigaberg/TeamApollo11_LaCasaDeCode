"""
FastAPI WebSocket backend for the person identification system.

Endpoints:
  WS  /ws          — live CSI stream (existing dashboard)
  WS  /ws/identify — identity detection stream
  POST /enroll/start  { name }
  POST /enroll/stop
  GET  /profiles
  DELETE /profiles/{name}
  GET  /events
"""
import asyncio
import collections
import json
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
import requests
import serial_asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from csi_parser import CSIFeatureExtractor
from profiles import ProfileStore

# ── Config ────────────────────────────────────────────────────────────────────
SERIAL_PORT   = "/dev/ttyACM0"   # Change to your ESP32 serial port
BAUD_RATE     = 115200
DEMO_MODE     = False             # Set True to test without hardware
SENSITIVE     = [19,20,21,22,23,24,25,26,38,39]
N_SUBCARRIERS = 64
WINDOW        = 30
CALIB_FRAMES  = 100
COOLDOWN_SECS = 5
BROADCAST_INTERVAL = 0.1   # max broadcast rate: 10 fps to avoid lag
CONFIRM_FRAMES = 8          # frames above threshold needed to confirm real motion (~0.8s at 10fps)
CLEAR_FRAMES   = 12         # frames below threshold needed to confirm room is clear

TELEGRAM_TOKEN   = "8653748907:AAGuS-6WWqgIUwGgYIYHKtQbfCfPD5s-ER8"
TELEGRAM_CHAT_ID = "5603958342"

# ── Globals ───────────────────────────────────────────────────────────────────
store     = ProfileStore()
extractor = CSIFeatureExtractor()
identity_clients:  list[WebSocket] = []
dashboard_clients: list[WebSocket] = []

enrolling_name: str | None = None
event_log: list[dict] = []   # last 100 crossing events

# Dashboard state (mirrors csi_receiver/app.py state dict)
cfg = {"threshold_mul": 2.0, "mute_until": 0.0}
state = {
    "variance":       0.0,
    "status":         "clear",
    "threshold":      0.0,
    "baseline":       0.0,
    "threshold_mul":  2.0,
    "occupied_since": None,
    "session": {
        "motion_events":  0,
        "uptime_s":       0,
        "avg_clear_var":  0.0,
        "avg_motion_var": 0.0,
    },
    "subcarriers":  [0.0] * len(SENSITIVE),
    "heatmap":      [0] * 24,
    "calibrating":  True,
    "activity":     "unknown",      # AI-detected activity class
    "activity_conf": 0.0,            # confidence 0.0-1.0
}
_calib_vars:  list[float] = []
_clear_vars:  list[float] = []
_motion_vars: list[float] = []
_amp_buf: collections.deque = collections.deque(maxlen=WINDOW)
_last_alert      = 0.0
_last_broadcast  = 0.0
_start_time      = time.time()
_above_count     = 0   # consecutive frames above threshold
_below_count     = 0   # consecutive frames below threshold
_confirmed_motion = False  # True once CONFIRM_FRAMES sustained
recalibrate_flag = asyncio.Event()

# ── Variance history for activity classification ───────────────────────────────
# Keep a rolling 3-second window of variance values to detect activity type
ACTIVITY_VAR_WINDOW = 60   # ~3s at 20fps
_var_history: collections.deque = collections.deque(maxlen=ACTIVITY_VAR_WINDOW)
# Calibrated empty-room variance (used as floor reference)
_empty_var_mean = 0.0
_empty_var_std  = 0.0


def classify_activity(var_history: list[float], baseline: float, threshold: float) -> tuple[str, float]:
    """
    Classify activity from recent variance history instead of single-frame amplitudes.
    
    Uses the ratio and pattern of variance relative to the calibrated baseline:
      - empty     : variance stays near baseline (ratio < 1.3)
      - breathing : tiny periodic variance just above baseline (ratio 1.3–2.5, low std)
      - stationary: small sustained offset above baseline (ratio 2.5–4.0)
      - walking   : large irregular spikes (ratio > 4.0 or high std-of-var)
      - fall      : sudden large spike followed by drop to near-baseline
    """
    if len(var_history) < 10 or baseline <= 0:
        return "unknown", 0.0

    arr = np.array(var_history, dtype=float)
    mean_var   = float(np.mean(arr))
    std_var    = float(np.std(arr))
    max_var    = float(np.max(arr))
    ratio      = mean_var / (baseline + 1e-9)
    cv         = std_var / (mean_var + 1e-9)   # coefficient of variation

    # Check for fall: large spike in the first half, drops off in the second half
    half = len(arr) // 2
    if len(arr) >= 20:
        first_mean  = float(np.mean(arr[:half]))
        second_mean = float(np.mean(arr[half:]))
        spike_ratio = first_mean / (second_mean + 1e-9)
        if max_var > threshold * 2.5 and spike_ratio > 2.5 and second_mean < threshold * 1.5:
            return "fall", min(0.5 + spike_ratio * 0.05, 0.95)

    # Walking: high variance with high variability (lots of movement-driven spikes)
    if ratio > 3.5 or (ratio > 2.0 and cv > 0.5):
        conf = min(0.5 + (ratio - 3.5) * 0.1 + cv * 0.2, 0.95)
        return "walking", round(conf, 3)

    # Stationary: elevated but stable variance (person present, not moving much)
    if 2.0 < ratio <= 3.5 and cv < 0.5:
        conf = min(0.5 + (ratio - 2.0) * 0.15, 0.85)
        return "stationary", round(conf, 3)

    # Breathing: small variance just above baseline, low variability
    if 1.3 < ratio <= 2.0 and cv < 0.4:
        conf = min(0.5 + (ratio - 1.3) * 0.3, 0.80)
        return "breathing", round(conf, 3)

    # Empty: variance at or near baseline
    if ratio <= 1.3:
        conf = min(0.5 + (1.3 - ratio) * 0.5, 0.95)
        return "empty", round(conf, 3)

    return "stationary", 0.4


# ── CSI line parser (matches ESP32 LLTF output) ───────────────────────────────
def parse_csi_line(line: str):
    """
    Parse a raw ESP32 CSI line.
    Handles both bare format:  CSI,<ts>,<rssi>,<noise>,<len>,<bytes...>
    And ESP-IDF log prefix:    I (1234) csi: CSI,<ts>,...
    Returns (aggregate_amplitude, subcarrier_amplitudes) or None.
    """
    line = line.strip()
    # Strip ESP-IDF log prefix if present (e.g. "I (1234) csi: CSI,...")
    if "CSI," in line:
        line = line[line.index("CSI,"):]
    elif not line.startswith("CSI"):
        return None
    parts = line.split(",")
    if len(parts) < 8:
        return None
    try:
        length = int(parts[4])
        raw    = [int(x) for x in parts[5:5 + length]]
    except (ValueError, IndexError):
        return None

    amps = []
    for i in range(0, len(raw) - 1, 2):
        im, re = raw[i], raw[i + 1]
        if im > 127: im -= 256
        if re > 127: re -= 256
        amps.append(math.sqrt(re**2 + im**2))

    sensitive_amps = [amps[i] for i in SENSITIVE if i < len(amps)]
    if not sensitive_amps:
        return None

    aggregate = float(np.mean(sensitive_amps))
    return aggregate, sensitive_amps


# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if time.time() < cfg["mute_until"]:
        return
    try:
        requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            params={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=3,
        )
    except Exception:
        pass


# ── Broadcast helper ──────────────────────────────────────────────────────────
async def _broadcast(clients: list[WebSocket], data: dict):
    dead = []
    for ws in clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.remove(ws)


# ── Calibration ───────────────────────────────────────────────────────────────
async def run_calibration(reader):
    global _calib_vars, _amp_buf
    state["calibrating"] = True
    _calib_vars = []
    _amp_buf.clear()
    print("\n🔧 Calibrating — empty the room and stay still...")
    await _broadcast(dashboard_clients, state)

    deadline = time.time() + 60  # max 60s for calibration
    while len(_calib_vars) < CALIB_FRAMES:
        if recalibrate_flag.is_set():
            break
        if time.time() > deadline:
            print("\n⚠️  Calibration timeout — using collected data")
            break
        try:
            line = (await asyncio.wait_for(reader.readline(), timeout=2.0)).decode("utf-8", errors="ignore")
        except asyncio.TimeoutError:
            continue
        result = parse_csi_line(line)
        if result is None:
            continue
        agg, sc_amps = result
        # Pad/trim to exactly len(SENSITIVE) elements
        sc_amps_fixed = (sc_amps + [0.0] * len(SENSITIVE))[:len(SENSITIVE)]
        _amp_buf.append(sc_amps_fixed)
        if len(_amp_buf) == WINDOW:
            arr = np.array(_amp_buf, dtype=float)
            _calib_vars.append(float(np.var(arr, axis=0).mean()))
        print(f"  Calibrating... {len(_calib_vars)}/{CALIB_FRAMES}", end="\r")
        # Broadcast progress so frontend progress bar moves
        await _broadcast(dashboard_clients, state)

    baseline  = float(np.mean(_calib_vars)) if _calib_vars else 1.0
    threshold = baseline * cfg["threshold_mul"]
    state["baseline"]      = round(baseline, 4)
    state["threshold"]     = round(threshold, 4)
    state["threshold_mul"] = cfg["threshold_mul"]
    state["calibrating"]   = False
    # Store empty-room variance stats for activity classification
    global _empty_var_mean, _empty_var_std, _var_history
    _empty_var_mean = baseline
    _empty_var_std  = float(np.std(_calib_vars)) if len(_calib_vars) > 1 else baseline * 0.1
    _var_history.clear()
    print(f"\n✅ Calibration done — baseline={baseline:.4f}  threshold={threshold:.4f}")
    await _broadcast(dashboard_clients, state)
    return threshold


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(csi_reader_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── CSI reader loop ───────────────────────────────────────────────────────────
async def csi_reader_loop():
    if DEMO_MODE:
        await _demo_loop()
    else:
        await _serial_loop()


async def _demo_loop():
    """Simulate CSI crossings for testing without hardware."""
    import random
    global _last_alert, _start_time
    _start_time = time.time()
    crossing_timer = 0
    # Fake calibration
    state["baseline"]     = 1.0
    state["threshold"]    = 2.0
    state["calibrating"]  = False
    while True:
        await asyncio.sleep(0.01)
        crossing_timer += 1
        amp = random.gauss(0, 0.3)
        if crossing_timer > 800:
            phase = (crossing_timer - 800) / 80
            amp += 5.0 * math.exp(-((phase - 1.5) ** 2) / 0.5) * (1 + random.gauss(0, 0.1))
            if crossing_timer > 1050:
                crossing_timer = 0
        sc_amps = [abs(amp + random.gauss(0, 0.1)) for _ in SENSITIVE]
        await _process_frame(abs(amp), sc_amps, state["threshold"])


async def _serial_loop():
    """Read from real ESP32 over serial using the ESP32 LLTF CSI format."""
    global _start_time
    _start_time = time.time()
    try:
        reader, _ = await serial_asyncio.open_serial_connection(
            url=SERIAL_PORT, baudrate=BAUD_RATE
        )
        threshold = await run_calibration(reader)

        # Drain any backlogged serial data accumulated during calibration
        try:
            while True:
                await asyncio.wait_for(reader.readline(), timeout=0.05)
        except asyncio.TimeoutError:
            pass

        print(f"🌐 WebSocket live at ws://localhost:8000/ws\n")

        while True:
            if recalibrate_flag.is_set():
                recalibrate_flag.clear()
                threshold = await run_calibration(reader)
                # Drain again after recalibration
                try:
                    while True:
                        await asyncio.wait_for(reader.readline(), timeout=0.05)
                except asyncio.TimeoutError:
                    pass
                continue

            line = (await reader.readline()).decode("utf-8", errors="ignore")
            result = parse_csi_line(line)
            if result is None:
                continue
            agg, sc_amps = result
            await _process_frame(agg, sc_amps, threshold)

    except Exception as e:
        print(f"Serial error: {e}. Falling back to demo mode.")
        await _demo_loop()


async def _process_frame(agg: float, sc_amps: list[float], threshold: float):
    """Update dashboard state and run identity feature extraction."""
    global _last_alert, _clear_vars, _motion_vars, _last_broadcast
    global _above_count, _below_count, _confirmed_motion

    # Pad/trim sc_amps to exactly len(SENSITIVE) elements
    sc_amps_fixed = (sc_amps + [0.0] * len(SENSITIVE))[:len(SENSITIVE)]
    _amp_buf.append(sc_amps_fixed)
    if len(_amp_buf) < WINDOW:
        return

    arr         = np.array(_amp_buf, dtype=float)
    current_var = float(np.var(arr, axis=0).mean())
    sub_vars    = np.var(arr, axis=0).tolist()
    now         = time.time()
    hour        = datetime.now().hour

    # Accumulate variance history for activity classification
    _var_history.append(current_var)
    # ── Sustained motion detection ────────────────────────────────────────────
    if current_var > threshold:
        _above_count += 1
        _below_count  = 0
        # Only confirm motion after CONFIRM_FRAMES consecutive above-threshold frames
        if _above_count >= CONFIRM_FRAMES and not _confirmed_motion:
            _confirmed_motion = True
    else:
        _below_count += 1
        _above_count  = 0
        # Only clear after CLEAR_FRAMES consecutive below-threshold frames
        if _below_count >= CLEAR_FRAMES:
            _confirmed_motion = False

    if _confirmed_motion:
        status = "motion"
        if state["occupied_since"] is None:
            state["occupied_since"] = now
            state["session"]["motion_events"] += 1
            state["heatmap"][hour] += 1
        _motion_vars.append(current_var)
        if now - _last_alert > COOLDOWN_SECS:
            send_telegram(f"🚨 Motion detected! (var={current_var:.2f})")
            _last_alert = now
    else:
        status = "clear"
        if not _confirmed_motion:
            state["occupied_since"] = None
        _clear_vars.append(current_var)

    if len(_clear_vars)  > 200: _clear_vars.pop(0)
    if len(_motion_vars) > 200: _motion_vars.pop(0)

    state["variance"]    = round(current_var, 4)
    state["status"]      = status
    state["subcarriers"] = [round(v, 3) for v in sub_vars]
    state["session"]["uptime_s"]       = int(now - _start_time)
    state["session"]["avg_clear_var"]  = round(float(np.mean(_clear_vars)),  3) if _clear_vars  else 0.0
    state["session"]["avg_motion_var"] = round(float(np.mean(_motion_vars)), 3) if _motion_vars else 0.0

    # AI activity classification — variance-based temporal classifier
    activity_name, activity_conf = classify_activity(
        list(_var_history), state["baseline"], state["threshold"]
    )
    state["activity"]      = activity_name
    state["activity_conf"] = activity_conf

    # Throttle broadcasts to avoid flooding the WebSocket and causing lag
    if now - _last_broadcast >= BROADCAST_INTERVAL:
        _last_broadcast = now
        await _broadcast(dashboard_clients, state)

    # Identity feature extraction runs every frame (no throttle needed)
    await _process_identity(current_var, sc_amps_fixed)


def _sanitize(obj):
    """Recursively replace nan/inf floats so JSON serialization never fails."""
    if isinstance(obj, float):
        if obj != obj or obj == float('inf') or obj == float('-inf'):
            return 0.0
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


async def _process_identity(agg: float, sc_amps: list[float]):
    """Run identity feature extraction; broadcast on crossing completion."""
    global enrolling_name
    features = extractor.push_multi(agg, sc_amps)
    if features is None:
        return

    scalar_vec = extractor.scalar_vector(features).tolist()
    ts = time.strftime("%H:%M:%S")

    if enrolling_name:
        count = store.enroll(enrolling_name, features, scalar_vec)
        print(f"  ✏️  Enrolled {enrolling_name} — crossing #{count}")
        event = {
            "type":     "enrolled",
            "name":     enrolling_name,
            "count":    count,
            "time":     ts,
            "features": {k: v for k, v in features.items() if k != "envelope"},
        }
    else:
        name, dist = store.identify(features, extractor.scalar_vector(features))
        event = {
            "type":         "identified" if name != "unknown" else "unknown",
            "name":         name,
            "display_name": f"Highly Likely {name}" if name != "unknown" else "UNKNOWN",
            "distance":     round(dist, 3),
            "time":         ts,
            "features":     {k: v for k, v in features.items() if k != "envelope"},
        }

    event_log.insert(0, _sanitize(event))
    if len(event_log) > 100:
        event_log.pop()

    await _broadcast(identity_clients, event)


# ── WebSocket endpoints ───────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_dashboard(ws: WebSocket):
    """Existing dashboard CSI stream — also accepts control commands."""
    await ws.accept()
    dashboard_clients.append(ws)
    await ws.send_json(state)
    try:
        async for raw in ws.iter_text():
            try:
                cmd = json.loads(raw)
                if cmd.get("cmd") == "recalibrate":
                    recalibrate_flag.set()
                elif cmd.get("cmd") == "set_mul":
                    val = float(cmd.get("value", 2.0))
                    cfg["threshold_mul"] = max(1.1, min(val, 10.0))
                    recalibrate_flag.set()
                elif cmd.get("cmd") == "mute":
                    mins = int(cmd.get("minutes", 10))
                    cfg["mute_until"] = time.time() + mins * 60
                    await ws.send_json({"muted_until": cfg["mute_until"]})
                elif cmd.get("cmd") == "export":
                    lines = ["time,status,variance"]
                    for row in event_log:
                        lines.append(f"{row['time']},{row.get('type','')},{row.get('features',{}).get('peak_variance','')}")
                    await ws.send_json({"export": "\n".join(lines)})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if ws in dashboard_clients:
            dashboard_clients.remove(ws)


@app.websocket("/ws/identify")
async def ws_identify(ws: WebSocket):
    """Identity event stream for the new dashboard panel."""
    await ws.accept()
    identity_clients.append(ws)
    # Send recent history on connect
    for ev in event_log[:20]:
        await ws.send_json(ev)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in identity_clients:
            identity_clients.remove(ws)


# ── REST endpoints ────────────────────────────────────────────────────────────
class EnrollRequest(BaseModel):
    name: str


@app.post("/enroll/start")
async def enroll_start(req: EnrollRequest):
    global enrolling_name
    enrolling_name = req.name.strip()
    return {"status": "enrolling", "name": enrolling_name}


@app.post("/enroll/stop")
async def enroll_stop():
    global enrolling_name
    name = enrolling_name
    enrolling_name = None
    count = len(store.profiles.get(name, [])) if name else 0
    return {"status": "stopped", "name": name, "crossings": count}


@app.get("/profiles")
async def get_profiles():
    return store.list_profiles()


@app.delete("/profiles/{name}")
async def delete_profile(name: str):
    ok = store.delete(name)
    return {"deleted": ok, "name": name}


@app.get("/events")
async def get_events():
    return _sanitize(event_log[:50])
