# SAYGEX — Secure Anomaly Yielding Gaurdian EXchange

A real-time, camera-free intrusion detection system using Wi-Fi CSI (Channel State Information) signals from ESP32-S3 microcontrollers. When someone moves in a room, it disturbs the Wi-Fi signal — SAYGEX detects that disturbance and alerts you instantly.

---

## How It Works

Two ESP32-S3 boards are used:

- **Transmitter** — acts as a Wi-Fi Access Point (`CSI_Beacon`) and broadcasts UDP packets every 50ms
- **Receiver** — connects to the AP as a station, captures CSI data from the incoming frames, and streams raw IQ samples over USB serial to a laptop

The Python backend reads the serial stream, extracts amplitude variance across 10 sensitive subcarriers, runs a calibration phase to establish a baseline, and flags motion when variance exceeds `baseline × multiplier`. Results are broadcast in real time to the web dashboard via WebSocket.

---

## System Architecture

```
[ESP32 Transmitter] --Wi-Fi CSI--> [ESP32 Receiver] --USB Serial--> [Python Backend]
                                                                            |
                                                                     WebSocket (ws://localhost:8000/ws)
                                                                            |
                                                              [Web Dashboard on Render]
                                                                            |
                                                                   [ngrok tunnel for remote access]
```

---

## Hardware Required

- 2× ESP32-S3 development boards
- USB cable (receiver connected to laptop)
- ESP-IDF v5.x toolchain

---

## Firmware Setup

Flash the transmitter:
```bash
cd csi_transmitter
idf.py build flash
```

Flash the receiver:
```bash
cd csi_receiver
idf.py build flash
```

The receiver streams CSI lines over serial at 115200 baud in the format:
```
CSI,<timestamp>,<rssi>,<noise_floor>,<length>,<raw IQ bytes...>
```

---

## Backend Setup

```bash
cd csi_receiver
pip install pyserial websockets numpy requests
python app.py
```

The backend will:
1. Open `/dev/ttyACM0` at 115200 baud
2. Run a 300-frame calibration (keep the room empty and still)
3. Start streaming detection results over WebSocket at `ws://localhost:8000/ws`

To change the serial port, edit `PORT` in `app.py`.

---

## Frontend (Dashboard)

Hosted on Render. Built with Vite + vanilla JS + Chart.js.

To run locally:
```bash
cd "Code Clash"
npm install
npm run dev
```

To rebuild for production:
```bash
npm run build
git add -f dist/
git commit -m "rebuild"
git push
```

---

## Remote Access via ngrok

Since the backend runs locally (the ESP32 is physically connected to your laptop), use ngrok to expose it:

```bash
ngrok http 8000
```

Then enter the tunnel URL in the dashboard login screen's Backend URL field:
```
wss://your-ngrok-url.ngrok-free.app/ws
```

---

## Dashboard Login

| Role           | Username | Password      |
|----------------|----------|---------------|
| Admin          | admin    | saygex@2026   |
| Property Owner | owner    | saygex@2026   |

Admin has full controls (recalibrate, sensitivity, mute, export).
Property Owner is view-only (live status + motion alerts).

---

## Features

- Motion detection using Wi-Fi CSI variance (no cameras)
- Live variance chart, subcarrier sparklines, 24h motion heatmap
- Telegram alerts on motion detection
- Recalibration on demand or via sensitivity slider
- Session stats: uptime, motion event count, avg variance
- CSV export of session data
- Role-based login (Admin / Property Owner)
- Dark/light theme toggle
- Remote access via ngrok

---

## Tech Stack

| Layer     | Technology                        |
|-----------|-----------------------------------|
| Hardware  | ESP32-S3, ESP-IDF, FreeRTOS       |
| Backend   | Python, pyserial, websockets, numpy |
| Frontend  | Vite, vanilla JS, Chart.js        |
| Hosting   | Render (frontend), ngrok (backend tunnel) |
| Alerts    | Telegram Bot API                  |
