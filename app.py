#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
PunchPoint - Fingerprint Attendance System
==========================================
Flask + SQLite + a serial-port fingerprint driver.

Project layout (keep these names, Flask looks for templates/ and static/):

    punchpoint/
    |- app.py                          this file: database, rules, API, driver
    |- templates/
    |   |- index.html                  the app shell
    |   |- login.html                  the sign-in page
    |- static/
    |   |- app.css                     every style in the app
    |   |- app.js                      every screen, drawn in the browser
    |   |- punchpoint_scanner.ino      Arduino sketch for the fingerprint module
    |- data/
        |- punchpoint.db               made on first run, this is your database

To start it:

    pip install flask pyserial
    python app.py

Then open http://localhost:5000 and sign in with  admin / admin123
(change that password on the Settings page before anyone else uses it).

Editing app.css or app.js only needs a browser refresh. Editing app.py needs a
restart: Ctrl+C in the terminal, then python app.py again. Or run
"python app.py --debug" and it restarts itself when you save.

Everyone who opens the site talks to this one server, so all computers see the
same employees and the same attendance records. The database is a single file:
data/punchpoint.db

Running it for a whole office
-----------------------------
Start it on one computer that stays on (the "server"), then let the others
reach it over the LAN:

    python app.py --host 0.0.0.0 --port 5000

Other machines open  http://<server-ip>:5000  in a browser. The computer with
the fingerprint scanner plugged in must be the server itself, because the
serial port is a local device.

How the fingerprint scanner is wired in
---------------------------------------
Browsers cannot read a USB/serial fingerprint reader, so the reader talks to
THIS program over a serial bus (COM port on Windows, /dev/ttyUSB* or
/dev/ttyACM* on Linux, /dev/cu.* on macOS). A background thread keeps the port
open, reads one line per event and turns it into an attendance record.

Typical hardware: an R307 / AS608 / ZFM-20 fingerprint module wired to an
Arduino (or ESP32), with the Arduino connected to the server by USB. The
Arduino does the matching and prints the matched slot number. A ready-made
sketch is served at  http://localhost:5000/arduino  and printed on the
Settings page.

Serial protocol (plain text, one line per message, newline terminated)
---------------------------------------------------------------------
  Device -> server
      READY                  scanner booted and waiting
      SCAN <id>              finger matched slot <id>      e.g.  SCAN 7
      SCAN <id> IN|OUT       same, but forces the direction
      <id>                   bare number, same as SCAN <id>
      NOMATCH                a finger was read but is not enrolled
      ENROLL PLACE|LIFT|AGAIN|IMAGE   progress while enrolling
      ENROLL OK <id>         enrollment finished, stored in slot <id>
      ENROLL FAIL <reason>   enrollment failed
      DELETED <id>           slot erased
      LOG <text>             anything you want to see in the status card
  Server -> device
      PING                   asks for READY
      ENROLL <id>            start enrolling into slot <id>
      DELETE <id>            erase slot <id>
      MODE SCAN              go back to normal scanning

Anything the device prints that does not match is ignored, so debug prints in
your sketch are harmless.

No hardware yet? Two options, both on the Settings page:
  * Turn on "demo scanner" to get simulate buttons on the Time clock page.
  * Post a scan over HTTP from any device on the network (ESP32 on Wi-Fi,
    another script, a test with curl):

        curl -X POST http://localhost:5000/api/scan \
             -H "Content-Type: application/json" \
             -H "X-Device-Key: <device key from Settings>" \
             -d '{"fingerprint_id": 7}'

Privacy note
------------
Only the slot number of a fingerprint template is kept here. The template
itself never leaves the scanner, and no fingerprint image is ever stored.
Biometric data is sensitive personal information under the Philippine Data
Privacy Act (RA 10173): collect written consent before enrolling anyone.
"""

import argparse
import csv
import getpass
import io

import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta

try:
    from flask import (Flask, Response, jsonify, redirect, render_template,
                       request, send_from_directory, session)
    from werkzeug.security import check_password_hash, generate_password_hash
except ImportError:  # pragma: no cover
    sys.exit("Flask is not installed yet. Run:\n\n    pip install flask pyserial\n")

try:
    import serial                       # pyserial
    from serial.tools import list_ports
except ImportError:                     # the app still runs, the scanner just cannot open
    serial = None
    list_ports = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("PUNCHPOINT_DB") or os.path.join(DATA_DIR, "punchpoint.db")
DEFAULT_ADMIN = ("admin", "admin123")
DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'staff',
  created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS employees(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  code       TEXT UNIQUE NOT NULL,
  name       TEXT NOT NULL,
  dept       TEXT NOT NULL DEFAULT '',
  position   TEXT NOT NULL DEFAULT '',
  finger_id  INTEGER UNIQUE,
  active     INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS logs(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  emp_id  INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
  ts      INTEGER NOT NULL,
  day     TEXT NOT NULL,
  type    TEXT NOT NULL,
  source  TEXT NOT NULL DEFAULT 'fingerprint',
  by_user TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_day  ON logs(day);
CREATE INDEX IF NOT EXISTS idx_logs_emp  ON logs(emp_id, ts);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS holidays(day TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     INTEGER NOT NULL,
  user   TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
"""

_local = threading.local()


def db():
    """One SQLite connection per thread (the serial reader is its own thread)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def q(sql, args=()):
    return db().execute(sql, args).fetchall()


def q1(sql, args=()):
    return db().execute(sql, args).fetchone()


def run(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()
    return cur


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

SETTING_KINDS = {
    "company": "str",
    "shift_start": "str",
    "shift_end": "str",
    "grace": "int",
    "cooldown": "int",
    "break_minutes": "int",
    "ot_after": "int",
    "work_days": "days",
    "demo_mode": "bool",
    "device_key": "str",
    "serial_enabled": "bool",
    "serial_port": "str",
    "serial_baud": "int",
    "secret_key": "str",
}

SETTING_DEFAULTS = {
    "company": "PunchPoint",
    "shift_start": "08:00",
    "shift_end": "17:00",
    "grace": "10",
    "cooldown": "5",
    "break_minutes": "60",
    "ot_after": "30",
    "work_days": "1,2,3,4,5",
    "demo_mode": "1",
    "serial_enabled": "0",
    "serial_port": "",
    "serial_baud": "9600",
}

_settings_cache = {}
_settings_lock = threading.Lock()


def _cast(key, raw):
    kind = SETTING_KINDS.get(key, "str")
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0
    if kind == "bool":
        return str(raw) in ("1", "true", "True", "yes", "on")
    if kind == "days":
        return [int(x) for x in str(raw).split(",") if x.strip().isdigit()]
    return "" if raw is None else str(raw)


def settings():
    with _settings_lock:
        if _settings_cache:
            return dict(_settings_cache)
    rows = q("SELECT key, value FROM settings")
    data = {r["key"]: _cast(r["key"], r["value"]) for r in rows}
    for key, raw in SETTING_DEFAULTS.items():
        data.setdefault(key, _cast(key, raw))
    with _settings_lock:
        _settings_cache.clear()
        _settings_cache.update(data)
    return dict(data)


def set_setting(key, value):
    if isinstance(value, bool):
        raw = "1" if value else "0"
    elif isinstance(value, (list, tuple)):
        raw = ",".join(str(int(v)) for v in value)
    else:
        raw = str(value)
    run("INSERT INTO settings(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, raw))
    with _settings_lock:
        _settings_cache.clear()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db().executescript(SCHEMA)
    db().commit()
    for key, raw in SETTING_DEFAULTS.items():
        if not q1("SELECT 1 FROM settings WHERE key=?", (key,)):
            set_setting(key, raw)
    if not q1("SELECT 1 FROM settings WHERE key='device_key'"):
        set_setting("device_key", secrets.token_hex(16))
    if not q1("SELECT 1 FROM settings WHERE key='secret_key'"):
        set_setting("secret_key", secrets.token_hex(32))
    if not q1("SELECT 1 FROM users"):
        run("INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
            (DEFAULT_ADMIN[0], generate_password_hash(DEFAULT_ADMIN[1]), "admin", int(time.time())))


def audit(user, action, detail=""):
    run("INSERT INTO audit(ts, user, action, detail) VALUES(?,?,?,?)",
        (int(time.time()), user or "system", action, detail))


# --------------------------------------------------------------------------
# dates and times
# --------------------------------------------------------------------------

def today_key():
    return date.today().isoformat()


def day_of(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")


def day_label(key):
    return datetime.strptime(key, "%Y-%m-%d").strftime("%a, %b %d")


def day_label_long(key):
    d = datetime.strptime(key, "%Y-%m-%d")
    return d.strftime("%A, %B ") + str(d.day) + d.strftime(", %Y")


def to_min(hhmm):
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def dur(mins):
    if mins is None:
        return "-"
    total = int(round(mins))
    return "%dh %02dm" % (total // 60, total % 60)


def day_range(start, end):
    a = datetime.strptime(start, "%Y-%m-%d").date()
    b = datetime.strptime(end, "%Y-%m-%d").date()
    if a > b:
        a, b = b, a
    out = []
    while a <= b and len(out) < 400:
        out.append(a.isoformat())
        a += timedelta(days=1)
    return out


def valid_day(text, fallback=None):
    try:
        datetime.strptime(str(text), "%Y-%m-%d")
        return str(text)
    except Exception:
        return fallback


# --------------------------------------------------------------------------
# attendance rules
# --------------------------------------------------------------------------

def holiday_map(days=None):
    if days:
        marks = ",".join("?" * len(days))
        rows = q("SELECT day, name FROM holidays WHERE day IN (%s)" % marks, tuple(days))
    else:
        rows = q("SELECT day, name FROM holidays")
    return {r["day"]: r["name"] for r in rows}


def summarize(day, rows, st, holiday=None, now=None):
    """Turn one employee's punches for one day into a tidy attendance record.

    rows: list of dicts/sqlite rows with ts and type, oldest first.
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    first_in = last_out = None
    worked = 0.0
    open_in = None
    pairs = 0

    for r in rows:
        if r["type"] == "in":
            if first_in is None:
                first_in = r["ts"]
            if open_in is None:
                open_in = r["ts"]
        else:
            last_out = r["ts"]
            if open_in is not None:
                worked += (r["ts"] - open_in) / 60.0
                open_in = None
                pairs += 1

    note = ""
    if open_in is not None:
        if day == today:
            note = "Still clocked in"
            worked += max(0.0, (now.timestamp() - open_in) / 60.0)   # running total
        else:
            note = "No time-out recorded"

    # One long stretch that covers lunch: take the unpaid break out of it.
    if pairs <= 1 and worked >= st["break_minutes"] + 240:
        worked -= st["break_minutes"]

    start_min = to_min(st["shift_start"])
    end_min = to_min(st["shift_end"])
    expected = max(0, end_min - start_min - st["break_minutes"])

    late_by = 0
    if first_in is not None:
        in_min = datetime.fromtimestamp(first_in).hour * 60 + datetime.fromtimestamp(first_in).minute
        late_by = max(0, in_min - start_min)
        status = "Late" if late_by > st["grace"] else "Present"
        if late_by <= st["grace"]:
            late_by = 0
        if holiday:
            note = (note + " - " if note else "") + holiday
    elif holiday:
        status = "Holiday"
        note = holiday
    elif day > today:
        status = "-"
    elif datetime.strptime(day, "%Y-%m-%d").weekday() not in [(d - 1) % 7 for d in st["work_days"]]:
        status = "Rest day"
    elif day == today and (now.hour * 60 + now.minute) < end_min:
        status = "Not yet in"
    else:
        status = "Absent"

    over = under = 0
    if first_in is not None and expected:
        extra = worked - expected
        if extra >= st["ot_after"]:
            over = int(round(extra))
        elif extra < 0 and not note.startswith("Still"):
            under = int(round(-extra))

    return {
        "day": day,
        "day_label": day_label(day),
        "first_in": first_in,
        "last_out": last_out,
        "time_in": clock(first_in) if first_in else "",
        "time_out": clock(last_out) if last_out else "",
        "mins": round(worked, 1) if worked > 0 else None,
        "hours": dur(worked) if worked > 0 else ("0h 00m" if rows else "-"),
        "status": status,
        "late_by": late_by,
        "overtime": over,
        "undertime": under,
        "punches": len(rows),
        "note": note,
    }


def logs_by_day(emp_ids, days):
    """{(emp_id, day): [rows]} for a set of employees over a set of days."""
    if not emp_ids or not days:
        return {}
    out = {}
    for start in range(0, len(emp_ids), 400):       # SQLite caps the number of ? markers
        chunk = emp_ids[start:start + 400]
        marks = ",".join("?" * len(chunk))
        rows = q("SELECT id, emp_id, ts, day, type, source FROM logs "
                 "WHERE emp_id IN (%s) AND day BETWEEN ? AND ? ORDER BY ts" % marks,
                 tuple(chunk) + (min(days), max(days)))
        for r in rows:
            out.setdefault((r["emp_id"], r["day"]), []).append(r)
    return out


def day_summary(emp_id, day, st=None, holidays=None):
    st = st or settings()
    rows = q("SELECT ts, type FROM logs WHERE emp_id=? AND day=? ORDER BY ts", (emp_id, day))
    holidays = holidays if holidays is not None else holiday_map([day])
    return summarize(day, rows, st, holidays.get(day))


# --------------------------------------------------------------------------
# punching in and out
# --------------------------------------------------------------------------

EVENTS = deque(maxlen=60)
_event_seq = 0
_event_lock = threading.Lock()
_punch_lock = threading.Lock()


def push_event(ev):
    """Keep the newest scans in memory so every open Time clock page sees them."""
    global _event_seq
    with _event_lock:
        _event_seq += 1
        ev["event_id"] = _event_seq
        ev["at"] = int(time.time())
        EVENTS.append(ev)
    return ev


def events_since(since):
    with _event_lock:
        return _event_seq, [dict(e) for e in EVENTS if e["event_id"] > since]


def last_event_id():
    with _event_lock:
        return _event_seq


def punch(finger_id, source="fingerprint", force=None, by_user=""):
    """The one place a scan becomes an attendance record.

    The serial driver, the HTTP device endpoint and the demo buttons all end
    up here, so they can never drift apart.
    """
    st = settings()
    with _punch_lock:
        emp = q1("SELECT * FROM employees WHERE finger_id=?", (finger_id,))
        if not emp:
            return push_event({
                "ok": False, "source": source, "finger_id": finger_id,
                "error": "Fingerprint not recognized",
                "detail": "Slot %s is not linked to anyone. Enroll it on the Employees page."
                          % finger_id,
                "emp": None,
            })
        if not emp["active"]:
            return push_event({
                "ok": False, "source": source, "finger_id": finger_id,
                "error": "Inactive employee",
                "detail": "%s is marked inactive, so punches are not being recorded." % emp["name"],
                "emp": {"id": emp["id"], "code": emp["code"], "name": emp["name"], "dept": emp["dept"]},
            })

        now = int(time.time())
        last = q1("SELECT * FROM logs WHERE emp_id=? ORDER BY ts DESC LIMIT 1", (emp["id"],))
        if last and now - last["ts"] < st["cooldown"] and force is None:
            return push_event({
                "ok": False, "source": source, "finger_id": finger_id,
                "error": "Already recorded",
                "detail": "Time %s was taken at %s. Wait a few seconds before scanning again."
                          % (last["type"], clock(last["ts"])),
                "emp": {"id": emp["id"], "code": emp["code"], "name": emp["name"], "dept": emp["dept"]},
            })

        today = day_of(now)
        last_today = q1("SELECT type FROM logs WHERE emp_id=? AND day=? ORDER BY ts DESC LIMIT 1",
                        (emp["id"], today))
        kind = force if force in ("in", "out") else ("out" if last_today and last_today["type"] == "in" else "in")
        run("INSERT INTO logs(emp_id, ts, day, type, source, by_user) VALUES(?,?,?,?,?,?)",
            (emp["id"], now, today, kind, source, by_user))

    summary = day_summary(emp["id"], today, st)
    first = kind == "in" and summary["first_in"] == now
    return push_event({
        "ok": True,
        "source": source,
        "finger_id": finger_id,
        "type": kind,
        "time": clock(now),
        "date_label": day_label(today),
        "first": first,
        "late": first and summary["status"] == "Late",
        "late_by": summary["late_by"],
        "worked": summary["hours"],
        "status": summary["status"],
        "emp": {"id": emp["id"], "code": emp["code"], "name": emp["name"],
                "dept": emp["dept"], "position": emp["position"]},
    })


# --------------------------------------------------------------------------
# the serial driver: keeps the scanner's port open and reads it line by line
# --------------------------------------------------------------------------

class Enrollment(object):
    """Tracks the 'put your finger down three times' handshake."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.status = "idle"        # idle | running | done | failed
        self.step = 0               # 0..3
        self.message = ""
        self.emp_id = None
        self.finger_id = None
        self.started = 0

    def start(self, emp_id, finger_id):
        with self.lock:
            self.reset()
            self.status = "running"
            self.step = 1
            self.emp_id = emp_id
            self.finger_id = finger_id
            self.message = "Place the finger on the scanner"
            self.started = time.time()

    def progress(self, step, message):
        with self.lock:
            if self.status != "running":
                return
            self.step = max(self.step, step)
            self.message = message

    def finish(self, finger_id=None):
        with self.lock:
            if finger_id:
                self.finger_id = finger_id
            self.status = "done"
            self.step = 3
            self.message = "Fingerprint saved in slot %s" % self.finger_id
            emp_id, fid = self.emp_id, self.finger_id
        if emp_id and fid:
            try:
                run("UPDATE employees SET finger_id=NULL WHERE finger_id=? AND id<>?", (fid, emp_id))
                run("UPDATE employees SET finger_id=? WHERE id=?", (fid, emp_id))
            except Exception:
                pass

    def fail(self, message):
        with self.lock:
            self.status = "failed"
            self.message = message or "Enrollment failed"

    def snapshot(self):
        with self.lock:
            if self.status == "running" and time.time() - self.started > 60:
                self.status = "failed"
                self.message = "Timed out waiting for the scanner"
            return {"status": self.status, "step": self.step, "message": self.message,
                    "finger_id": self.finger_id, "emp_id": self.emp_id}


ENROLL = Enrollment()


class SerialScanner(threading.Thread):
    """Background thread that owns the serial port.

    It reconnects on its own, so unplugging the scanner and plugging it back
    in does not need a restart of the server.
    """

    daemon = True

    def __init__(self):
        threading.Thread.__init__(self, name="serial-scanner")
        self.out = queue.Queue()
        self.lock = threading.Lock()
        self.state = {
            "connected": False, "enabled": False, "port": "", "baud": 0,
            "message": "Serial scanner is off", "last_line": "", "last_seen": None,
            "scans": 0, "errors": 0,
        }
        self.port = None

    # -- state ------------------------------------------------------------
    def set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def status(self):
        with self.lock:
            s = dict(self.state)
        s["pyserial"] = serial is not None
        return s

    def send(self, line):
        """Queue a command for the device. Raises if the port is not open."""
        if not self.state.get("connected"):
            raise RuntimeError("The fingerprint scanner is not connected.")
        self.out.put(line.strip() + "\n")

    # -- the loop ---------------------------------------------------------
    def run(self):
        while True:
            st = settings()
            if not st["serial_enabled"] or not st["serial_port"]:
                self.set(connected=False, enabled=False, port=st["serial_port"], baud=st["serial_baud"],
                         message="Serial scanner is turned off in Settings")
                time.sleep(1)
                continue
            if serial is None:
                self.set(connected=False, enabled=True, message="pyserial is not installed. Run: pip install pyserial")
                time.sleep(3)
                continue
            self.set(enabled=True, port=st["serial_port"], baud=st["serial_baud"],
                     message="Opening %s ..." % st["serial_port"])
            try:
                self.port = serial.Serial(st["serial_port"], st["serial_baud"], timeout=0.4)
                time.sleep(2)                     # Arduinos reset when the port opens
                self.port.reset_input_buffer()
                self.set(connected=True, message="Connected to %s at %d baud"
                                                 % (st["serial_port"], st["serial_baud"]))
                self.out.put("PING\n")
                self._pump(st)
            except Exception as exc:
                self.set(connected=False, message=self._friendly(exc), errors=self.state["errors"] + 1)
                time.sleep(3)
            finally:
                try:
                    if self.port:
                        self.port.close()
                except Exception:
                    pass
                self.port = None
                if self.state.get("connected"):
                    self.set(connected=False, message="Scanner disconnected")

    def _pump(self, st):
        buf = b""
        while True:
            now = settings()
            if (not now["serial_enabled"] or now["serial_port"] != st["serial_port"]
                    or now["serial_baud"] != st["serial_baud"]):
                return                             # settings changed, reopen with the new ones
            while not self.out.empty():
                self.port.write(self.out.get_nowait().encode("utf-8", "ignore"))
            chunk = self.port.read(256)
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.handle(line.decode("utf-8", "ignore").strip("\r\n \t"))
            if len(buf) > 4096:
                buf = b""

    @staticmethod
    def _friendly(exc):
        text = str(exc)
        if "PermissionError" in text or "Access is denied" in text or "Permission denied" in text:
            return ("Cannot open the port: something else is using it (close the Arduino Serial "
                    "Monitor), or your user is not in the 'dialout' group on Linux.")
        if "could not open port" in text or "FileNotFoundError" in text:
            return "That port does not exist right now. Check the cable and the port name in Settings."
        return "Serial error: " + text

    # -- one line from the device ----------------------------------------
    def handle(self, line):
        if not line:
            return
        self.set(last_line=line, last_seen=int(time.time()))
        upper = line.upper()

        if upper.startswith("SCAN") or re.match(r"^\d+$", upper) or upper.startswith("ID"):
            nums = re.findall(r"\d+", line)
            if not nums:
                return
            force = "in" if " IN" in upper else ("out" if " OUT" in upper else None)
            self.set(scans=self.state["scans"] + 1)
            punch(int(nums[0]), source="fingerprint", force=force)
            return

        if "NOMATCH" in upper or "NO MATCH" in upper or upper.startswith("FAIL"):
            push_event({"ok": False, "source": "fingerprint", "finger_id": None, "emp": None,
                        "error": "Fingerprint not recognized",
                        "detail": "The scanner read a finger but found no match. Try again, or ask "
                                  "the admin to enroll it."})
            return

        if upper.startswith("ENROLL"):
            rest = upper[6:].strip()
            nums = re.findall(r"\d+", rest)
            if rest.startswith("OK"):
                ENROLL.finish(int(nums[0]) if nums else None)
            elif rest.startswith("FAIL") or rest.startswith("ERR"):
                ENROLL.fail(line[6:].strip().split(" ", 1)[-1] or "The scanner could not read that finger")
            elif rest.startswith("PLACE") or rest.startswith("IMAGE"):
                ENROLL.progress(1, "Place the finger on the scanner")
            elif rest.startswith("LIFT") or rest.startswith("REMOVE"):
                ENROLL.progress(2, "Lift the finger")
            elif rest.startswith("AGAIN"):
                ENROLL.progress(3, "Place the same finger again")
            return

        if upper.startswith("DELETED"):
            self.set(message="Scanner slot erased")
            return
        if upper.startswith("READY"):
            self.set(message="Scanner is ready")
            return
        if upper.startswith("LOG"):
            self.set(message=line[3:].strip())


SCANNER = SerialScanner()


def demo_enroll(emp_id, finger_id):
    """Stand-in for the hardware handshake when the demo scanner is on."""
    def worker():
        ENROLL.start(emp_id, finger_id)
        for step, msg in ((1, "Place the finger on the scanner"),
                          (2, "Lift the finger"),
                          (3, "Place the same finger again")):
            time.sleep(1.1)
            if ENROLL.snapshot()["status"] != "running":
                return
            ENROLL.progress(step, msg)
        time.sleep(1.0)
        if ENROLL.snapshot()["status"] == "running":
            ENROLL.finish(finger_id)
    threading.Thread(target=worker, daemon=True).start()


def next_finger_id():
    used = {r["finger_id"] for r in q("SELECT finger_id FROM employees WHERE finger_id IS NOT NULL")}
    i = 1
    while i in used:
        i += 1
    return i


# --------------------------------------------------------------------------
# web app: sign in
# --------------------------------------------------------------------------

app = Flask(__name__)
_login_tries = {}


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return q1("SELECT id, username, role FROM users WHERE id=?", (uid,))


def login_required(fn):
    def wrapper(*a, **kw):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify({"error": "Your session ended. Sign in again."}), 401
            return redirect("/login")
        return fn(*a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper


def admin_required(fn):
    def wrapper(*a, **kw):
        user = current_user()
        if not user:
            return jsonify({"error": "Your session ended. Sign in again."}), 401
        if user["role"] != "admin":
            return jsonify({"error": "Only an admin can do that."}), 403
        return fn(*a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper


def body():
    return request.get_json(silent=True) or {}


def fail(message, code=400):
    return jsonify({"error": message}), code


def as_int(value, fallback=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def uses_default_password():
    row = q1("SELECT password_hash FROM users WHERE username=?", (DEFAULT_ADMIN[0],))
    return bool(row and check_password_hash(row["password_hash"], DEFAULT_ADMIN[1]))


@app.after_request
def no_store(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if current_user():
            return redirect("/")
        hint = "First time here? Sign in with admin / admin123" if uses_default_password() else ""
        return render_template("login.html", hint=hint, error="")

    ip = request.remote_addr or "?"
    tries, until = _login_tries.get(ip, (0, 0))
    if tries >= 6 and time.time() < until:
        wait = int(until - time.time())
        return render_template("login.html", hint="",
                               error="Too many attempts. Try again in %d seconds." % wait), 429

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    row = q1("SELECT * FROM users WHERE username=?", (username,))
    if row and check_password_hash(row["password_hash"], password):
        _login_tries.pop(ip, None)
        session.clear()
        session["uid"] = row["id"]
        session.permanent = True
        audit(username, "signed in")
        return redirect("/")

    _login_tries[ip] = (tries + 1, time.time() + 60)
    return render_template("login.html", hint="", error="Wrong username or password."), 401


@app.post("/api/logout")
def logout():
    user = current_user()
    if user:
        audit(user["username"], "signed out")
    session.clear()
    return jsonify({"ok": True})


@app.get("/")
@login_required
def home():
    return render_template("index.html")


@app.get("/arduino")
def arduino_sketch():
    """The sketch for the scanner side, so it can be opened straight from Settings."""
    return send_from_directory(app.static_folder, "punchpoint_scanner.ino", mimetype="text/plain")


# --------------------------------------------------------------------------
# api: who am I, dashboard
# --------------------------------------------------------------------------

@app.get("/api/me")
@login_required
def api_me():
    user = current_user()
    st = settings()
    return jsonify({
        "username": user["username"], "role": user["role"],
        "today": today_key(), "today_label": day_label_long(today_key()),
        "company": st["company"], "demo_mode": st["demo_mode"],
        "default_password": user["role"] == "admin" and uses_default_password(),
        "scanner": SCANNER.status(),
    })


@app.get("/api/dashboard")
@login_required
def api_dashboard():
    st = settings()
    today = today_key()
    emps = q("SELECT * FROM employees WHERE active=1 ORDER BY name")
    week = [(date.today() - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    holidays = holiday_map(week)
    grouped = logs_by_day([e["id"] for e in emps], week)

    present = late = in_now = 0
    waiting = 0
    floor = []
    for e in emps:
        rows = grouped.get((e["id"], today), [])
        s = summarize(today, rows, st, holidays.get(today))
        if s["first_in"]:
            present += 1
            if s["status"] == "Late":
                late += 1
            if rows and rows[-1]["type"] == "in":
                in_now += 1
                floor.append({"name": e["name"], "dept": e["dept"], "since": clock(s["first_in"]),
                              "hours": s["hours"]})
        elif s["status"] in ("Not yet in", "Absent"):
            waiting += 1

    bars = []
    for key in week:
        count = sum(1 for e in emps if grouped.get((e["id"], key)))
        weekday_js = (datetime.strptime(key, "%Y-%m-%d").weekday() + 1) % 7
        rest = weekday_js not in st["work_days"] and count == 0
        bars.append({"key": key, "label": DAY_NAMES[weekday_js], "count": count,
                     "rest": rest, "holiday": holidays.get(key, "")})

    recent = q("SELECT l.id, l.ts, l.day, l.type, l.source, e.name FROM logs l "
               "JOIN employees e ON e.id=l.emp_id ORDER BY l.ts DESC LIMIT 8")
    return jsonify({
        "today": today, "today_label": day_label_long(today),
        "employee_count": len(emps),
        "enrolled_count": sum(1 for e in emps if e["finger_id"]),
        "present": present, "late": late, "waiting": waiting,
        "waiting_label": "Absent" if any(
            summarize(today, grouped.get((e["id"], today), []), st, holidays.get(today))["status"] == "Absent"
            for e in emps) else "Not yet in",
        "in_now": floor,
        "week": bars,
        "holiday_today": holidays.get(today, ""),
        "recent": [{"name": r["name"], "type": r["type"], "time": clock(r["ts"]),
                    "day_label": day_label(r["day"]), "is_today": r["day"] == today,
                    "source": r["source"]} for r in recent],
    })


# --------------------------------------------------------------------------
# api: employees
# --------------------------------------------------------------------------

def emp_json(row):
    return {"id": row["id"], "code": row["code"], "name": row["name"], "dept": row["dept"],
            "position": row["position"], "finger_id": row["finger_id"], "active": bool(row["active"])}


def next_code():
    top = 0
    for r in q("SELECT code FROM employees"):
        nums = re.findall(r"\d+", r["code"] or "")
        if nums:
            top = max(top, int(nums[-1]))
    return "EMP-%03d" % (top + 1)


@app.get("/api/employees")
@login_required
def api_employees():
    rows = q("SELECT * FROM employees ORDER BY active DESC, name")
    return jsonify([emp_json(r) for r in rows])


@app.post("/api/employees")
@admin_required
def api_employee_add():
    data = body()
    name = (data.get("name") or "").strip()
    if not name:
        return fail("Enter the employee's full name.")
    dept = (data.get("dept") or "").strip()
    if not dept:
        return fail("Enter a department.")
    finger = data.get("finger_id")
    finger = int(finger) if str(finger or "").strip().isdigit() else None
    if finger is not None and q1("SELECT 1 FROM employees WHERE finger_id=?", (finger,)):
        return fail("Scanner slot %d already belongs to someone else." % finger)
    code = (data.get("code") or "").strip() or next_code()
    if q1("SELECT 1 FROM employees WHERE code=?", (code,)):
        return fail("Employee ID %s is already taken." % code)
    active = 1 if data.get("active", True) else 0
    cur = run("INSERT INTO employees(code, name, dept, position, finger_id, active, created_at) "
              "VALUES(?,?,?,?,?,?,?)",
              (code, name, dept, (data.get("position") or "").strip(), finger, active, int(time.time())))
    audit(current_user()["username"], "added employee", name)
    return jsonify(emp_json(q1("SELECT * FROM employees WHERE id=?", (cur.lastrowid,))))


@app.put("/api/employees/<int:emp_id>")
@admin_required
def api_employee_edit(emp_id):
    row = q1("SELECT * FROM employees WHERE id=?", (emp_id,))
    if not row:
        return fail("That employee no longer exists.", 404)
    data = body()
    name = (data.get("name") or "").strip() or row["name"]
    dept = (data.get("dept") or "").strip()
    if not dept:
        return fail("Enter a department.")
    finger = data.get("finger_id")
    finger = int(finger) if str(finger or "").strip().isdigit() else None
    if finger is not None:
        clash = q1("SELECT name FROM employees WHERE finger_id=? AND id<>?", (finger, emp_id))
        if clash:
            return fail("Scanner slot %d already belongs to %s." % (finger, clash["name"]))
    active = 1 if data.get("active", True) else 0
    run("UPDATE employees SET name=?, dept=?, position=?, finger_id=?, active=? WHERE id=?",
        (name, dept, (data.get("position") or "").strip(), finger, active, emp_id))
    audit(current_user()["username"], "edited employee", name)
    return jsonify(emp_json(q1("SELECT * FROM employees WHERE id=?", (emp_id,))))


@app.delete("/api/employees/<int:emp_id>")
@admin_required
def api_employee_delete(emp_id):
    row = q1("SELECT * FROM employees WHERE id=?", (emp_id,))
    if not row:
        return fail("That employee no longer exists.", 404)
    run("DELETE FROM employees WHERE id=?", (emp_id,))
    audit(current_user()["username"], "deleted employee", row["name"])
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# api: attendance logs
# --------------------------------------------------------------------------

@app.get("/api/logs")
@login_required
def api_logs():
    st = settings()
    where, args = [], []
    day = valid_day(request.args.get("date"))
    if day:
        where.append("l.day=?")
        args.append(day)
    emp = as_int(request.args.get("emp"))
    if emp is not None:
        where.append("l.emp_id=?")
        args.append(emp)
    if request.args.get("type") in ("in", "out"):
        where.append("l.type=?")
        args.append(request.args["type"])
    if request.args.get("source") in ("fingerprint", "manual"):
        where.append("l.source=?")
        args.append(request.args["source"])
    limit = max(1, min(as_int(request.args.get("limit"), 200) or 200, 1000))
    sql = ("SELECT l.*, e.name, e.code, e.dept FROM logs l JOIN employees e ON e.id=l.emp_id "
           + ("WHERE " + " AND ".join(where) + " " if where else "")
           + "ORDER BY l.ts DESC LIMIT ?")
    rows = q(sql, tuple(args) + (limit,))

    cache = {}
    out = []
    for r in rows:
        arrival = None
        if r["type"] == "in":
            key = (r["emp_id"], r["day"])
            if key not in cache:
                cache[key] = day_summary(r["emp_id"], r["day"], st)
            s = cache[key]
            if s["first_in"] == r["ts"]:
                arrival = {"status": s["status"], "late_by": s["late_by"]}
        out.append({"id": r["id"], "emp_id": r["emp_id"], "name": r["name"], "code": r["code"],
                    "dept": r["dept"], "time": clock(r["ts"]), "day": r["day"],
                    "day_label": day_label(r["day"]), "type": r["type"], "source": r["source"],
                    "by_user": r["by_user"], "arrival": arrival})
    return jsonify(out)


@app.post("/api/logs")
@login_required
def api_log_add():
    data = body()
    emp = q1("SELECT * FROM employees WHERE id=?", (as_int(data.get("emp_id"), 0),))
    if not emp:
        return fail("Pick an employee.")
    day = valid_day(data.get("date"))
    if not day:
        return fail("Choose a valid date.")
    stamp = (data.get("time") or "").strip()
    if not re.match(r"^\d{1,2}:\d{2}$", stamp):
        return fail("Choose a valid time.")
    kind = data.get("type") if data.get("type") in ("in", "out") else "in"
    ts = int(datetime.strptime(day + " " + stamp, "%Y-%m-%d %H:%M").timestamp())
    if ts > time.time() + 60:
        return fail("That time has not happened yet.")
    run("INSERT INTO logs(emp_id, ts, day, type, source, by_user) VALUES(?,?,?,?,'manual',?)",
        (emp["id"], ts, day, kind, current_user()["username"]))
    audit(current_user()["username"], "manual entry", "%s %s %s %s" % (emp["name"], kind, day, stamp))
    return jsonify({"ok": True})


@app.delete("/api/logs/<int:log_id>")
@admin_required
def api_log_delete(log_id):
    row = q1("SELECT l.*, e.name FROM logs l JOIN employees e ON e.id=l.emp_id WHERE l.id=?", (log_id,))
    if not row:
        return fail("That punch is already gone.", 404)
    run("DELETE FROM logs WHERE id=?", (log_id,))
    audit(current_user()["username"], "deleted punch",
          "%s %s %s" % (row["name"], row["type"], clock(row["ts"])))
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# api: reports
# --------------------------------------------------------------------------

def build_report(args):
    st = settings()
    today = today_key()
    to = valid_day(args.get("to"), today)
    frm = valid_day(args.get("from"), (date.today() - timedelta(days=6)).isoformat())
    days = day_range(frm, to)[-62:]              # about two months at a time
    sql = "SELECT * FROM employees WHERE 1=1"
    params = []
    emp = as_int(args.get("emp"))
    if emp is not None:
        sql += " AND id=?"
        params.append(emp)
    if args.get("dept"):
        sql += " AND dept=?"
        params.append(args["dept"])
    if args.get("active") != "all":
        sql += " AND active=1"
    emps = q(sql + " ORDER BY name", tuple(params))
    grouped = logs_by_day([e["id"] for e in emps], days)
    holidays = holiday_map(days)

    rows = []
    for day in reversed(days):
        for e in emps:
            s = summarize(day, grouped.get((e["id"], day), []), st, holidays.get(day))
            if s["status"] in ("-", "Rest day"):
                continue
            if s["status"] == "Holiday" and not s["first_in"]:
                continue
            s = dict(s)
            s.update({"emp_id": e["id"], "code": e["code"], "name": e["name"], "dept": e["dept"]})
            rows.append(s)
    return {"from": days[0] if days else frm, "to": days[-1] if days else to,
            "days": len(days), "rows": rows}


@app.get("/api/report")
@login_required
def api_report():
    return jsonify(build_report(request.args))


@app.get("/api/report.csv")
@login_required
def api_report_csv():
    data = build_report(request.args)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date", "Employee ID", "Name", "Department", "Time in", "Time out",
                "Hours", "Late (min)", "Overtime (min)", "Undertime (min)", "Status", "Notes"])
    for r in data["rows"]:
        w.writerow([r["day"], r["code"], r["name"], r["dept"], r["time_in"], r["time_out"],
                    "" if r["mins"] is None else round(r["mins"] / 60.0, 2),
                    r["late_by"] or "", r["overtime"] or "", r["undertime"] or "",
                    r["status"], r["note"]])
    name = "attendance_%s_to_%s.csv" % (data["from"], data["to"])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=" + name})


# --------------------------------------------------------------------------
# api: the time clock page
# --------------------------------------------------------------------------

@app.get("/api/kiosk/events")
@login_required
def api_events():
    since = as_int(request.args.get("since"), 0) or 0
    last, items = events_since(since)
    return jsonify({"last_id": last, "events": items, "scanner": SCANNER.status()})


@app.post("/api/kiosk/scan")
@login_required
def api_kiosk_scan():
    """The demo buttons on the Time clock page. Same path as a real scan."""
    if not settings()["demo_mode"]:
        return fail("The demo scanner is switched off in Settings.", 403)
    data = body()
    try:
        finger = int(data.get("fingerprint_id"))
    except (TypeError, ValueError):
        return fail("Pick a finger to simulate.")
    return jsonify(punch(finger, source="demo", by_user=current_user()["username"]))


@app.post("/api/scan")
def api_device_scan():
    """For a scanner that talks over the network instead of a serial cable."""
    key = request.headers.get("X-Device-Key") or request.args.get("key") or ""
    if not secrets.compare_digest(key, settings()["device_key"]):
        return fail("Bad device key.", 401)
    data = body()
    try:
        finger = int(data.get("fingerprint_id"))
    except (TypeError, ValueError):
        return fail("Send fingerprint_id as a number.")
    kind = data.get("type") if data.get("type") in ("in", "out") else None
    return jsonify(punch(finger, source="fingerprint", force=kind))


# --------------------------------------------------------------------------
# api: enrolling a finger through the scanner
# --------------------------------------------------------------------------

@app.get("/api/fingerprint/next")
@login_required
def api_next_finger():
    return jsonify({"id": next_finger_id()})


@app.get("/api/fingerprint/enroll")
@login_required
def api_enroll_state():
    state = ENROLL.snapshot()
    state["scanner"] = SCANNER.status()
    return jsonify(state)


@app.post("/api/fingerprint/enroll")
@admin_required
def api_enroll_start():
    data = body()
    emp_id = as_int(data.get("emp_id"), 0) or None
    finger = data.get("finger_id")
    finger = int(finger) if str(finger or "").strip().isdigit() else next_finger_id()
    clash = q1("SELECT name FROM employees WHERE finger_id=? AND id<>?", (finger, emp_id or -1))
    if clash:
        return fail("Scanner slot %d already belongs to %s." % (finger, clash["name"]))

    st = settings()
    if SCANNER.status()["connected"]:
        ENROLL.start(emp_id, finger)
        try:
            SCANNER.send("ENROLL %d" % finger)
        except Exception as exc:
            ENROLL.fail(str(exc))
            return fail(str(exc))
        return jsonify({"ok": True, "finger_id": finger, "mode": "scanner"})
    if st["demo_mode"]:
        demo_enroll(emp_id, finger)
        return jsonify({"ok": True, "finger_id": finger, "mode": "demo"})
    return fail("The fingerprint scanner is not connected. Turn on the demo scanner in Settings "
                "if you are testing without hardware.")


@app.post("/api/fingerprint/cancel")
@admin_required
def api_enroll_cancel():
    ENROLL.fail("Cancelled")
    try:
        SCANNER.send("MODE SCAN")
    except Exception:
        pass
    return jsonify({"ok": True})


@app.post("/api/fingerprint/delete")
@admin_required
def api_finger_delete():
    finger = as_int(body().get("finger_id"), 0)
    run("UPDATE employees SET finger_id=NULL WHERE finger_id=?", (finger,))
    try:
        SCANNER.send("DELETE %d" % finger)
    except Exception:
        pass
    audit(current_user()["username"], "cleared fingerprint", "slot %d" % finger)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# api: settings, scanner setup, users, holidays
# --------------------------------------------------------------------------

EDITABLE = ["company", "shift_start", "shift_end", "grace", "cooldown", "break_minutes",
            "ot_after", "work_days", "demo_mode", "serial_enabled", "serial_port", "serial_baud"]


@app.get("/api/settings")
@login_required
def api_settings():
    st = settings()
    out = {k: st.get(k) for k in EDITABLE}
    out["scanner"] = SCANNER.status()
    out["ports"] = serial_ports()
    if current_user()["role"] == "admin":
        out["device_key"] = st["device_key"]
    return jsonify(out)


@app.put("/api/settings")
@admin_required
def api_settings_save():
    data = body()
    for key, value in data.items():
        if key not in EDITABLE:
            continue
        if key in ("shift_start", "shift_end"):
            if not re.match(r"^\d{1,2}:\d{2}$", str(value)):
                return fail("Enter the time as HH:MM.")
        if key == "work_days":
            value = [int(v) for v in value if str(v).isdigit() and 0 <= int(v) <= 6]
        if key in ("grace", "cooldown", "break_minutes", "ot_after", "serial_baud"):
            try:
                value = max(0, int(value))
            except (TypeError, ValueError):
                return fail("That value has to be a number.")
        set_setting(key, value)
    audit(current_user()["username"], "changed settings", ", ".join(data.keys()))
    return jsonify({"ok": True})


@app.post("/api/settings/device-key")
@admin_required
def api_new_device_key():
    set_setting("device_key", secrets.token_hex(16))
    audit(current_user()["username"], "made a new device key")
    return jsonify({"device_key": settings()["device_key"]})


def serial_ports():
    if list_ports is None:
        return []
    try:
        return [{"port": p.device, "label": (p.description or p.device)} for p in list_ports.comports()]
    except Exception:
        return []


@app.get("/api/serial/status")
@login_required
def api_serial_status():
    return jsonify({"scanner": SCANNER.status(), "ports": serial_ports()})


@app.post("/api/serial/test")
@admin_required
def api_serial_test():
    try:
        SCANNER.send("PING")
    except Exception as exc:
        return fail(str(exc))
    return jsonify({"ok": True})


@app.get("/api/users")
@admin_required
def api_users():
    rows = q("SELECT id, username, role, created_at FROM users ORDER BY username")
    return jsonify([{"id": r["id"], "username": r["username"], "role": r["role"],
                     "created": day_label(day_of(r["created_at"]))} for r in rows])


@app.post("/api/users")
@admin_required
def api_user_add():
    data = body()
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    role = "admin" if data.get("role") == "admin" else "staff"
    if not re.match(r"^[a-z0-9._-]{3,24}$", username):
        return fail("Usernames are 3-24 characters: letters, numbers, dot, dash or underscore.")
    if len(password) < 8:
        return fail("Give the new account a password of at least 8 characters.")
    if q1("SELECT 1 FROM users WHERE username=?", (username,)):
        return fail("That username is taken.")
    run("INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
        (username, generate_password_hash(password), role, int(time.time())))
    audit(current_user()["username"], "added user", username)
    return jsonify({"ok": True})


@app.delete("/api/users/<int:user_id>")
@admin_required
def api_user_delete(user_id):
    me = current_user()
    if me["id"] == user_id:
        return fail("You cannot delete the account you are signed in with.")
    row = q1("SELECT username, role FROM users WHERE id=?", (user_id,))
    if not row:
        return fail("That account is already gone.", 404)
    if row["role"] == "admin" and q1("SELECT COUNT(*) c FROM users WHERE role='admin'")["c"] <= 1:
        return fail("Keep at least one admin account.")
    run("DELETE FROM users WHERE id=?", (user_id,))
    audit(me["username"], "deleted user", row["username"])
    return jsonify({"ok": True})


@app.post("/api/password")
@login_required
def api_password():
    data = body()
    me = current_user()
    row = q1("SELECT * FROM users WHERE id=?", (me["id"],))
    if not check_password_hash(row["password_hash"], data.get("current") or ""):
        return fail("That is not your current password.")
    new = data.get("new") or ""
    if len(new) < 8:
        return fail("Use at least 8 characters.")
    if new == DEFAULT_ADMIN[1]:
        return fail("Pick something other than the default password.")
    run("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), me["id"]))
    audit(me["username"], "changed password")
    return jsonify({"ok": True})


@app.get("/api/holidays")
@login_required
def api_holidays():
    rows = q("SELECT day, name FROM holidays ORDER BY day DESC LIMIT 100")
    return jsonify([{"day": r["day"], "label": day_label(r["day"]), "name": r["name"]} for r in rows])


@app.post("/api/holidays")
@admin_required
def api_holiday_add():
    data = body()
    day = valid_day(data.get("day"))
    name = (data.get("name") or "").strip()
    if not day:
        return fail("Choose a date.")
    if not name:
        return fail("Name the holiday, for example New Year's Day.")
    run("INSERT INTO holidays(day, name) VALUES(?,?) "
        "ON CONFLICT(day) DO UPDATE SET name=excluded.name", (day, name))
    audit(current_user()["username"], "added holiday", "%s %s" % (day, name))
    return jsonify({"ok": True})


@app.delete("/api/holidays/<day>")
@admin_required
def api_holiday_delete(day):
    run("DELETE FROM holidays WHERE day=?", (valid_day(day, ""),))
    return jsonify({"ok": True})


@app.get("/api/audit")
@admin_required
def api_audit():
    rows = q("SELECT * FROM audit ORDER BY id DESC LIMIT 40")
    return jsonify([{"when": day_label(day_of(r["ts"])) + ", " + clock(r["ts"]),
                     "user": r["user"], "action": r["action"], "detail": r["detail"]} for r in rows])


# --------------------------------------------------------------------------
# api: sample data and housekeeping
# --------------------------------------------------------------------------

SAMPLE = [
    ("Maria Santos", "Accounting", "Senior Accountant", 1),
    ("Juan Dela Cruz", "IT", "Systems Administrator", 2),
    ("Angela Reyes", "Human Resources", "HR Officer", 3),
    ("Paolo Villanueva", "Operations", "Operations Lead", 4),
    ("Grace Mendoza", "Sales", "Sales Associate", 5),
    ("Ramon Bautista", "Warehouse", "Inventory Clerk", None),
]


def load_sample():
    import random
    run("DELETE FROM logs")
    run("DELETE FROM employees")
    st = settings()
    now = datetime.now()
    for i, (name, dept, position, finger) in enumerate(SAMPLE, start=1):
        run("INSERT INTO employees(code, name, dept, position, finger_id, active, created_at) "
            "VALUES(?,?,?,?,?,1,?)",
            ("EMP-%03d" % i, name, dept, position, finger, int(time.time())))
    emps = q("SELECT * FROM employees WHERE finger_id IS NOT NULL")
    for back in range(1, 11):
        day = (now - timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)
        if (day.weekday() + 1) % 7 not in st["work_days"]:
            continue
        for e in emps:
            roll = random.random()
            if roll < 0.07:
                continue                       # someone was out that day
            in_min = (8 * 60 + 11 + random.randint(0, 34)) if roll < 0.25 else (7 * 60 + 40 + random.randint(0, 27))
            out_min = 17 * 60 - 5 + random.randint(0, 50)
            for minute, kind in ((in_min, "in"), (out_min, "out")):
                ts = int((day + timedelta(minutes=minute, seconds=random.randint(0, 59))).timestamp())
                run("INSERT INTO logs(emp_id, ts, day, type, source) VALUES(?,?,?,?,'fingerprint')",
                    (e["id"], ts, day.strftime("%Y-%m-%d"), kind))
    minute_now = now.hour * 60 + now.minute
    for i, minute in enumerate([7 * 60 + 52, 8 * 60 + 3, 8 * 60 + 24]):
        if minute < minute_now - 1 and i < len(emps):
            ts = int(now.replace(hour=minute // 60, minute=minute % 60, second=20, microsecond=0).timestamp())
            run("INSERT INTO logs(emp_id, ts, day, type, source) VALUES(?,?,?,?,'fingerprint')",
                (emps[i]["id"], ts, now.strftime("%Y-%m-%d"), "in"))


@app.post("/api/admin/seed")
@admin_required
def api_seed():
    load_sample()
    audit(current_user()["username"], "loaded sample data")
    return jsonify({"ok": True})


@app.post("/api/admin/clear-logs")
@admin_required
def api_clear_logs():
    run("DELETE FROM logs")
    audit(current_user()["username"], "cleared all logs")
    return jsonify({"ok": True})


@app.post("/api/admin/reset")
@admin_required
def api_reset():
    run("DELETE FROM logs")
    run("DELETE FROM employees")
    audit(current_user()["username"], "deleted all employees and logs")
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# start the server
# --------------------------------------------------------------------------

def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))     # no packet is sent, it just picks the route
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def bootstrap(seed_if_new=True):
    """Prepare the database, the session key and the serial reader."""
    fresh = not os.path.exists(DB_PATH)
    init_db()
    if fresh and seed_if_new and not q1("SELECT 1 FROM employees"):
        load_sample()
    app.secret_key = settings()["secret_key"]
    app.permanent_session_lifetime = timedelta(days=14)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    if not SCANNER.is_alive():
        SCANNER.start()


def main():
    parser = argparse.ArgumentParser(description="PunchPoint attendance server")
    parser.add_argument("--host", default="127.0.0.1",
                        help="use 0.0.0.0 to let other computers on the network reach it")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--list-ports", action="store_true", help="show the serial ports this computer can see")
    parser.add_argument("--reset-admin", action="store_true", help="put the admin password back to admin123")
    parser.add_argument("--add-admin", metavar="USERNAME",
                        help="create an admin account (or make an existing account an admin), then stop")
    parser.add_argument("--password", help="password to use with --add-admin; you are asked for one if left out")
    parser.add_argument("--empty", action="store_true", help="skip the sample data on a brand new database")
    args = parser.parse_args()

    if args.list_ports:
        found = serial_ports()
        print("\n".join("%-16s %s" % (p["port"], p["label"]) for p in found) or
              "No serial ports found (install pyserial, or check the cable).")
        return

    if args.add_admin:
        init_db()
        name = args.add_admin.strip().lower()
        if not re.match(r"^[a-z0-9._-]{3,24}$", name):
            print("Usernames are 3-24 characters: letters, numbers, dot, dash or underscore.")
            return
        password = args.password or getpass.getpass("Password for %s (at least 8 characters): " % name)
        if len(password) < 8:
            print("That password is too short. Use at least 8 characters.")
            return
        if q1("SELECT 1 FROM users WHERE username=?", (name,)):
            run("UPDATE users SET password_hash=?, role='admin' WHERE username=?",
                (generate_password_hash(password), name))
            print("%s is now an admin, with the password you just gave." % name)
        else:
            run("INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,'admin',?)",
                (name, generate_password_hash(password), int(time.time())))
            print("Admin account %s created. Sign in with it at the login page." % name)
        audit("system", "added admin from the command line", name)
        return

    if args.reset_admin:
        init_db()
        run("UPDATE users SET password_hash=? WHERE username=?",
            (generate_password_hash(DEFAULT_ADMIN[1]), DEFAULT_ADMIN[0]))
        print("Admin password is back to %s / %s" % DEFAULT_ADMIN)
        return

    bootstrap(seed_if_new=not args.empty)

    where = "http://%s:%d" % ("localhost" if args.host in ("127.0.0.1", "localhost") else lan_ip(), args.port)
    print("")
    print("  PunchPoint is running")
    print("  ---------------------")
    print("  Open        %s" % where)
    print("  Sign in     %s / %s" % DEFAULT_ADMIN if uses_default_password()
          else "  Sign in     with your account")
    print("  Database    %s" % DB_PATH)
    print("  Scanner     %s" % (settings()["serial_port"] or "not set yet - Settings > Fingerprint scanner"))
    if serial is None:
        print("  Note        pyserial is missing, so the serial scanner cannot open. pip install pyserial")
    print("  Stop it with Ctrl+C")
    print("")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True,
            use_reloader=False)


if __name__ == "__main__":
    main()
