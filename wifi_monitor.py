#!/usr/bin/env python3
"""Sample Linux Wi-Fi connectivity each minute; speed-test every 30 minutes.

Requires: Python 3, NetworkManager (`nmcli`), iproute2 (`ip`),
iputils-ping (`ping`). `curl` enables HTTPS fallback when ICMP is filtered.
No root privileges needed.
"""

import argparse
import datetime as dt
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

DB_PATH = Path(
    os.environ.get(
        "WIFI_MONITOR_DB", str(Path.home() / ".local/share/wifi-monitor/uptime.db")
    )
)
SPEED_INTERVAL = dt.timedelta(minutes=30)
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=2000000"
PING_TARGETS = ("1.1.1.1", "8.8.8.8")
HTTPS_TARGETS = (
    "https://www.google.com/generate_204",
    "https://www.cloudflare.com/cdn-cgi/trace",
)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(s):
    d = dt.datetime.fromisoformat(s)
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d


def run_command(args, timeout=8):
    try:
        return subprocess.run(
            args, text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def unescape_nmcli(s):
    # nmcli -t escapes ':' and '\\' with a leading backslash.
    return re.sub(r"\\([:\\])", r"\1", s)


def get_wifi():
    """Return (interface, SSID), or (None, None) when not associated."""
    status = run_command(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device", "status"])
    if not status or status.returncode != 0:
        raise RuntimeError("nmcli could not query NetworkManager device status")
    for line in status.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        iface, typ, state = parts
        if typ != "wifi" or not state.startswith("connected"):
            continue
        scan = run_command(
            [
                "nmcli",
                "-t",
                "-f",
                "IN-USE,SSID",
                "device",
                "wifi",
                "list",
                "ifname",
                iface,
            ]
        )
        if scan and scan.returncode == 0:
            for row in scan.stdout.splitlines():
                if row.startswith("*:"):
                    return iface, unescape_nmcli(row[2:]) or None
        # The interface is associated, but its SSID is unavailable.
        return iface, None
    return None, None


def get_signal_score(iface):
    """Return NetworkManager's 0-100 signal score, or None if unavailable.

    This uses the same active Wi-Fi interface as the connectivity check.
    It is a quality score, not a percentage of physical signal power.
    """
    if not iface:
        return None
    scan = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "IN-USE,SIGNAL",
            "device",
            "wifi",
            "list",
            "ifname",
            iface,
        ]
    )
    if not scan or scan.returncode != 0:
        return None
    for row in scan.stdout.splitlines():
        if row.startswith("*:"):
            try:
                score = int(row[2:])
            except ValueError:
                return None
            return score if 0 <= score <= 100 else None
    return None


def get_gateway(iface):
    if not iface or not shutil.which("ip"):
        return None
    result = run_command(["ip", "-4", "route", "show", "default", "dev", iface])
    if not result or result.returncode != 0:
        return None
    match = re.search(r"\bvia\s+(\d+\.\d+\.\d+\.\d+)\b", result.stdout)
    return match.group(1) if match else None


def ping(host, iface):
    """Return (success, milliseconds or None), always using the Wi-Fi interface."""
    if not iface or not shutil.which("ping"):
        return False, None
    result = run_command(
        ["ping", "-n", "-I", iface, "-c", "1", "-W", "2", host], timeout=5
    )
    if not result or result.returncode != 0:
        return False, None
    match = re.search(r"time[=<]([\d.]+)\s*ms", result.stdout)
    return True, float(match.group(1)) if match else None


def https_probe(iface):
    """Return True on any bound HTTPS response, False if all fail, None if unavailable."""
    if not iface or not shutil.which("curl"):
        return None
    for url in HTTPS_TARGETS:
        result = run_command(
            [
                "curl",
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--interface",
                iface,
                "--connect-timeout",
                "3",
                "--max-time",
                "5",
                "--write-out",
                "%{http_code}",
                url,
            ],
            timeout=7,
        )
        if (
            result
            and result.returncode == 0
            and re.fullmatch(r"[1-4]\d\d", result.stdout.strip())
        ):
            return True
    return False


def check_connectivity(iface):
    """Return (status, latency_ms, gateway_ok).

    status: 'up', 'down', or 'unknown'. No active interface => 'down' is
    recorded separately as a Wi-Fi disconnection in measurement status.
    """
    if not iface:
        return "unknown", None, None
    gateway = get_gateway(iface)
    gateway_ok = ping(gateway, iface)[0] if gateway else None

    latency = None
    icmp_ok = False
    for host in PING_TARGETS:
        ok, ms = ping(host, iface)
        if ok:
            icmp_ok = True
            if latency is None:
                latency = ms

    if icmp_ok:
        return "up", latency, gateway_ok

    # HTTPS fallback avoids classifying ICMP filtering as an Internet outage.
    https_ok = https_probe(iface)
    if https_ok is None:
        return "unknown", None, gateway_ok
    return ("up" if https_ok else "down"), None, gateway_ok


def measure_speed(iface):
    """Download 2 MB; bind to Wi-Fi with curl to avoid Ethernet/VPN routing."""
    if not iface or not shutil.which("curl"):
        return None
    url = SPEED_URL + "&t=" + str(time.time_ns())
    start = time.monotonic()
    result = run_command(
        [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--location",
            "--interface",
            iface,
            "--connect-timeout",
            "5",
            "--max-time",
            "15",
            "--output",
            "/dev/null",
            "--write-out",
            "%{size_download}",
            url,
        ],
        timeout=18,
    )
    elapsed = time.monotonic() - start
    if not result or result.returncode != 0 or elapsed <= 0:
        return None
    try:
        size = float(result.stdout.strip())
        return round(size * 8 / elapsed / 1_000_000, 2) if size > 0 else None
    except ValueError:
        return None


def initialize_database():
    """Create a fresh database schema; no legacy migrations are needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            ssid TEXT,
            connected INTEGER NOT NULL,
            reachable INTEGER NOT NULL,
            ping_ms REAL,
            download_mbps REAL,
            speed_test_attempted INTEGER NOT NULL DEFAULT 0,
            wifi_interface TEXT,
            connectivity_status TEXT,
            gateway_reachable INTEGER,
            signal_score INTEGER CHECK (signal_score BETWEEN 0 AND 100)
        )""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_wifi_time ON measurements(timestamp, id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_wifi_ssid_time ON measurements(ssid, timestamp)"
        )


def speed_test_due(db, ssid, timestamp):
    row = db.execute(
        """SELECT timestamp FROM measurements
        WHERE ssid=? AND speed_test_attempted=1
        ORDER BY timestamp DESC, id DESC LIMIT 1""",
        (ssid,),
    ).fetchone()
    return row is None or timestamp - parse_time(row[0]) >= SPEED_INTERVAL


def record(
    timestamp, iface, ssid, status, ping_ms, gateway_ok, mbps, attempted, signal_score
):
    connected = int(ssid is not None and iface is not None)
    reachable = int(status == "up")
    with sqlite3.connect(DB_PATH, timeout=15) as db:
        db.execute(
            """INSERT INTO measurements
            (timestamp, ssid, connected, reachable, ping_ms,
             download_mbps, speed_test_attempted, wifi_interface,
             connectivity_status, gateway_reachable, signal_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp.isoformat(timespec="seconds"),
                ssid,
                connected,
                reachable,
                ping_ms,
                mbps,
                int(attempted),
                iface,
                status,
                None if gateway_ok is None else int(gateway_ok),
                signal_score,
            ),
        )


def monitor():
    timestamp = now_utc()
    iface, ssid = get_wifi()
    # Capture NetworkManager's 0-100 score at every associated Wi-Fi sample.
    signal_score = get_signal_score(iface) if ssid is not None else None
    if not iface:
        status, latency, gateway_ok = "wifi_disconnected", None, None
    elif not ssid:
        # Association was detected, but its SSID could not be read.
        status, latency, gateway_ok = "unknown", None, None
    else:
        status, latency, gateway_ok = check_connectivity(iface)

    attempted, speed = False, None
    if status == "up":
        with sqlite3.connect(DB_PATH) as db:
            due = speed_test_due(db, ssid, timestamp)
        if due:
            attempted, speed = True, measure_speed(iface)

    record(
        timestamp,
        iface,
        ssid,
        status,
        latency,
        gateway_ok,
        speed,
        attempted,
        signal_score,
    )
    print(
        f"{timestamp.isoformat(timespec='seconds')} SSID={ssid!r} "
        f"interface={iface!r} status={status} gateway={gateway_ok} "
        f"ping_ms={latency} signal_score={signal_score} "
        f"speed_mbps={speed} speed_test={attempted}"
    )


def find_outages(ssid, days=None):
    """Derive SSID outages from the GLOBAL chronological event stream.

    - A failure while associated with SSID starts an Internet outage.
    - Loss of association after observing SSID starts/continues an outage.
    - Re-association to SSID with 'up' closes the outage as recovered.
    - Association with another SSID closes it as 'switched', not recovered.
    - Unknown probe results are not treated as definitive failures.
    - Endpoints are sample timestamps: durations are estimates.
    """
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute("""SELECT timestamp, ssid, connectivity_status
            FROM measurements ORDER BY timestamp, id""").fetchall()

    outages = []
    last_ssid = None
    active = None

    def close(end, reason):
        nonlocal active
        if active is not None:
            outages.append(
                {
                    **active,
                    "end": end,
                    "duration": end - active["start"],
                    "status": reason,
                    "ongoing": False,
                }
            )
            active = None

    for ts_text, observed_ssid, status in rows:
        ts = parse_time(ts_text)

        if observed_ssid is not None:
            # A new SSID ends an outage of the old SSID, but does not count
            # as recovery on the original network.
            if observed_ssid != ssid:
                if active is not None:
                    close(ts, "switched_network")
                last_ssid = observed_ssid
                continue

            last_ssid = observed_ssid
            if status == "up":
                close(ts, "recovered")
            elif status == "down":
                if active is None:
                    active = {
                        "start": ts,
                        "reason": "internet_unreachable",
                        "failed_checks": 0,
                    }
                active["failed_checks"] += 1
            # Unknown status neither starts nor resolves outages.
            continue

        # A NULL SSID after this SSID was last observed is attributed to it.
        # Leading NULLs are ambiguous and cannot safely be assigned.
        if last_ssid == ssid and status == "wifi_disconnected":
            if active is None:
                active = {
                    "start": ts,
                    "reason": "wifi_disconnected",
                    "failed_checks": 0,
                }
            active["failed_checks"] += 1
            # Distinguish Wi-Fi loss from the initial Internet-only failure.
            if active["reason"] == "internet_unreachable":
                active["reason"] = "internet_then_wifi_disconnected"

    if active is not None:
        outages.append(
            {
                **active,
                "end": None,
                "duration": None,
                "status": "ongoing",
                "ongoing": True,
            }
        )

    if days is not None:
        cutoff = now_utc() - dt.timedelta(days=days)
        outages = [o for o in outages if o["end"] is None or o["end"] >= cutoff]
    return outages


def show_outages(ssid, days):
    results = find_outages(ssid, days)
    print(f"Outages for SSID {ssid!r} (UTC; durations estimated from samples)")
    if not results:
        print("None found.")
    for o in results:
        end = o["end"].isoformat(timespec="seconds") if o["end"] else "ONGOING"
        dur = str(o["duration"]) if o["duration"] else "unknown"
        print(
            f"{o['start'].isoformat(timespec='seconds')} -> {end} "
            f"duration={dur} reason={o['reason']} "
            f"closure={o['status']} failed_samples={o['failed_checks']}"
        )


def show_summary():
    with sqlite3.connect(DB_PATH) as db:
        total, up, down, unknown = db.execute("""SELECT COUNT(*),
            SUM(CASE WHEN connectivity_status='up' THEN 1 ELSE 0 END),
            SUM(CASE WHEN connectivity_status IN ('down','wifi_disconnected') THEN 1 ELSE 0 END),
            SUM(CASE WHEN connectivity_status='unknown' THEN 1 ELSE 0 END)
            FROM measurements""").fetchone()
        speed = db.execute(
            "SELECT ROUND(AVG(download_mbps),2) FROM measurements"
        ).fetchone()[0]
    print(f"Samples: {total}; up: {up}; unavailable: {down}; unknown: {unknown}")
    known = up + down
    print(
        f"Successful sample fraction (excluding unknown): {100 * up / known:.2f}%"
        if known
        else "No conclusive samples"
    )
    print(f"Average measured download Mbps: {speed}")
    print("This is not a continuous-time uptime percentage.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--summary", action="store_true")
    group.add_argument("--outages", metavar="SSID")
    parser.add_argument("--days", type=int)
    args = parser.parse_args()
    if args.days is not None and (args.days <= 0 or args.outages is None):
        parser.error("--days requires --outages and a positive number of days")
    if not shutil.which("nmcli"):
        raise RuntimeError("nmcli is required but was not found in PATH")
    initialize_database()
    if args.summary:
        show_summary()
    elif args.outages is not None:
        show_outages(args.outages, args.days)
    else:
        monitor()


if __name__ == "__main__":
    main()
