#!/usr/bin/env python3
"""Tank level web server for a Raspberry Pi + JSN-SR04T ultrasonic sensor.
 
Standard library only; each sensor mode needs one optional package:
    SENSOR=uart  -> pyserial   (pip install pyserial)   JSN-SR04T in serial mode
    SENSOR=gpio  -> gpiozero   (preinstalled on Pi OS)  JSN-SR04T in trigger/echo mode
    SENSOR=sim   -> nothing                              fake data for UI testing
    SENSOR=auto  -> (default) uart if the serial port exists, else gpio, else sim
 
Run:  python3 server.py
Env:  PORT=8000  SENSOR=auto  SENSOR_TO_BOTTOM_M=3.5  SAMPLE_SECONDS=60  READ_SECONDS=5
      SERIAL_PORT=/dev/serial0  SERIAL_BAUD=9600  TRIG_PIN=23  ECHO_PIN=24
"""
import json
import math
import os
import random
import signal
import sqlite3
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
 
BASE = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH", BASE / "levels.db"))
PORT = int(os.getenv("PORT", "8000"))
SENSOR = os.getenv("SENSOR", "auto").lower()
SENSOR_TO_BOTTOM_M = float(os.getenv("SENSOR_TO_BOTTOM_M", "3.5"))  # sensor face -> tank floor
SAMPLE_SECONDS = int(os.getenv("SAMPLE_SECONDS", "60"))  # how often a reading is stored for charts
READ_SECONDS = int(os.getenv("READ_SECONDS", "5"))       # how often the live value is refreshed
SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/serial0")
SERIAL_BAUD = int(os.getenv("SERIAL_BAUD", "9600"))
TRIG_PIN = int(os.getenv("TRIG_PIN", "23"))
ECHO_PIN = int(os.getenv("ECHO_PIN", "24"))
MIN_MM, MAX_MM = 200, 6000  # JSN-SR04T: ~20 cm dead zone, ~600 cm max range
LOW_PCT, HIGH_PCT = 20, 90  # status thresholds
 
 
# ---------------------------------------------------------------- sensors
# Each sensor exposes read_distance_m(): metres from sensor face to water surface.
class UartSensor:
    """JSN-SR04T in UART mode (module jumper/resistor set for serial output).
 
    Frame: 0xFF, DATA_H, DATA_L, SUM  with distance_mm = DATA_H*256 + DATA_L
    and SUM = (0xFF + DATA_H + DATA_L) & 0xFF.
    Works with both 'automatic output' and 'controlled output' variants: we send
    the 0x55 trigger byte (ignored by auto-output modules) and parse the next frame.
    """
    name = "uart"
 
    def __init__(self):
        import serial  # pyserial
        self.ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.2)
 
    def _read_mm(self):
        self.ser.reset_input_buffer()
        self.ser.write(b"\x55")
        buf = bytearray()
        deadline = time.time() + 1.0
        while time.time() < deadline:
            buf += self.ser.read(self.ser.in_waiting or 1)
            for i in range(len(buf) - 3):
                if buf[i] == 0xFF and ((0xFF + buf[i + 1] + buf[i + 2]) & 0xFF) == buf[i + 3]:
                    return (buf[i + 1] << 8) | buf[i + 2]
        return None
 
    def read_distance_m(self):
        vals = []
        for _ in range(5):
            mm = self._read_mm()
            if mm is not None and MIN_MM <= mm <= MAX_MM:
                vals.append(mm)
            time.sleep(0.1)
        if len(vals) < 2:
            raise RuntimeError("no valid UART readings (wiring, baud, or sensor mode?)")
        return statistics.median(vals) / 1000
 
    def close(self):
        self.ser.close()
 
 
class GpioSensor:
    """JSN-SR04T in default trigger/echo mode via gpiozero.
    NOTE: the ECHO line is 5 V; use a voltage divider (e.g. 1k/2k) before the Pi pin."""
    name = "gpio"
 
    def __init__(self):
        from gpiozero import DistanceSensor
        self.dev = DistanceSensor(echo=ECHO_PIN, trigger=TRIG_PIN, max_distance=MAX_MM / 1000)
 
    def read_distance_m(self):
        vals = []
        for _ in range(7):
            vals.append(self.dev.distance)
            time.sleep(0.06)
        return statistics.median(vals)
 
    def close(self):
        self.dev.close()
 
 
class SimSensor:
    """Fake tank: slow fill/drain cycle with noise."""
    name = "sim"
 
    @staticmethod
    def depth_at(t):
        frac = 0.55 + 0.35 * math.sin(t / 86400 * 2 * math.pi * 1.7)
        return max(0.0, min(SENSOR_TO_BOTTOM_M, frac * SENSOR_TO_BOTTOM_M + random.gauss(0, 0.01)))
 
    def read_distance_m(self):
        return SENSOR_TO_BOTTOM_M - self.depth_at(time.time())
 
    def close(self):
        pass
 
 
def make_sensor():
    if SENSOR == "sim":
        return SimSensor()
    if SENSOR == "uart":
        return UartSensor()   # fail loudly if explicitly requested
    if SENSOR == "gpio":
        return GpioSensor()
    # auto
    if os.path.exists(SERIAL_PORT):
        try:
            return UartSensor()
        except Exception as exc:
            print("UART unavailable:", exc)
    try:
        return GpioSensor()
    except Exception as exc:
        print("GPIO unavailable:", exc)
    print("No sensor found - using simulated data")
    return SimSensor()
 
 
sensor = make_sensor()
 
 
def read_depth_m():
    distance = sensor.read_distance_m()
    return max(0.0, min(SENSOR_TO_BOTTOM_M, SENSOR_TO_BOTTOM_M - distance))
 
 
# ---------------------------------------------------------------- storage
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS levels (ts INTEGER PRIMARY KEY, depth REAL)")
    return conn
 
 
def store(ts, depth):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO levels VALUES (?, ?)", (int(ts), depth))
 
 
def backfill_if_empty():
    """Simulation only: give the Month/Year charts something to show."""
    if sensor.name != "sim":
        return
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM levels").fetchone()[0]:
            return
        now = int(time.time())
        c.executemany(
            "INSERT INTO levels VALUES (?, ?)",
            [(t, SimSensor.depth_at(t)) for t in range(now - 365 * 86400, now, 900)],
        )
 
 
RANGES = {  # name: (window seconds, bucket seconds)
    "day": (86400, 600),
    "month": (30 * 86400, 10800),
    "year": (365 * 86400, 86400),
}
 
 
def history(name):
    window, bucket = RANGES.get(name, RANGES["day"])
    since = int(time.time()) - window
    with db() as c:
        rows = c.execute(
            "SELECT (ts / ?) * ? AS b, AVG(depth) FROM levels WHERE ts >= ? GROUP BY b ORDER BY b",
            (bucket, bucket, since),
        ).fetchall()
    return [[int(t), round(d, 3)] for t, d in rows]
 
 
# ---------------------------------------------------------------- sampling
# One background thread owns the sensor (the serial port must not be shared);
# HTTP requests only read the cached latest value.
latest = None
stop = threading.Event()
 
 
def make_snapshot(ts, depth):
    ratio = depth / SENSOR_TO_BOTTOM_M * 100
    status = "Low" if ratio < LOW_PCT else "High" if ratio > HIGH_PCT else "Normal"
    return {
        "ts": int(ts),
        "depth_m": round(depth, 2),
        "ratio_pct": round(ratio),
        "status": status,
        "tank_height_m": SENSOR_TO_BOTTOM_M,
        "sensor": sensor.name,
        "simulated": sensor.name == "sim",
    }
 
 
def sampler():
    global latest
    last_store = 0
    while not stop.is_set():
        try:
            now = time.time()
            depth = read_depth_m()
            latest = make_snapshot(now, depth)
            if now - last_store >= SAMPLE_SECONDS:
                store(now, depth)
                last_store = now
        except Exception as exc:  # keep going if one read fails; "latest" just goes stale
            print("sample error:", exc)
        stop.wait(READ_SECONDS)  # returns immediately when shutdown is requested
 
 
# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
 
    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")
 
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/level":
            if latest is None:
                self._json({"error": "no reading yet"}, 503)
            else:
                self._json(latest)
        elif url.path == "/api/history":
            self._json(history(parse_qs(url.query).get("range", ["day"])[0]))
        elif url.path in ("/", "/index.html"):
            self._send(200, (BASE / "index.html").read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, b"Not found", "text/plain")
 
    def log_message(self, *args):  # quiet
        pass
 
 
if __name__ == "__main__":
    backfill_if_empty()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    sampler_thread = threading.Thread(target=sampler)
    server_thread = threading.Thread(target=httpd.serve_forever)
 
    def request_stop(signum, _frame):
        print(f"\nReceived {signal.Signals(signum).name}, shutting down...")
        stop.set()
 
    signal.signal(signal.SIGINT, request_stop)   # Ctrl-C
    signal.signal(signal.SIGTERM, request_stop)  # systemctl stop
 
    sampler_thread.start()
    server_thread.start()
    print(f"Serving on http://localhost:{PORT}  (sensor: {sensor.name})  - Ctrl-C to stop")
 
    while not stop.wait(0.5):  # main thread just waits for a signal
        pass
 
    httpd.shutdown()          # stop accepting requests
    server_thread.join()
    httpd.server_close()      # release the listening socket
    sampler_thread.join(timeout=10)  # let an in-flight sensor read finish
    try:
        sensor.close()        # release serial port / GPIO pins
    except Exception as exc:
        print("sensor close error:", exc)
    print("Stopped cleanly.")