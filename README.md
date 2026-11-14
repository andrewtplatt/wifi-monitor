# Wi-Fi Monitor

A lightweight Python script for recording Wi-Fi connectivity and performance on Linux systems using **NetworkManager**. It takes one measurement per run, saves it in a local SQLite database, and can report connection statistics and outages.

Run it once a minute using **cron** or, a **systemd service and timer**. This is a one-shot script, not a continuously running daemon.

## Features

- Finds the connected Wi-Fi interface and SSID using `nmcli`.
- Records NetworkManager's Wi-Fi signal-quality score (0–100).
- Checks gateway and Internet connectivity, measures ICMP latency, and falls back to HTTPS when ping is blocked.
- Measures download speed using a 2 MB download from Cloudflare, no more than once every 30 minutes **per SSID**, when connectivity is working.
- Records time-stamped measurements in SQLite.
- Reports connection statistics and estimates Internet and Wi-Fi outage periods, including recovery and network switches.
- Uses only the Python standard library; no `pip` packages or root privileges are needed.

> **Scope:** This monitors an active NetworkManager-managed Wi-Fi connection, not Ethernet or every network interface. It does not reconnect Wi-Fi or send notifications.

## Requirements

- Linux, with Wi-Fi managed by **NetworkManager**.
- **Python 3.11+** (the script uses `datetime.UTC`).
- `nmcli` from NetworkManager (required).
- `curl` for HTTPS checks and speed measurements (required).
- `ip` from `iproute2` to find the gateway.
- `ping` from `iputils-ping` for reachability and latency.
- `git` to clone the project.

On Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 git network-manager iproute2 iputils-ping curl
```

> Installing NetworkManager alone does not configure it to manage your Wi-Fi. If another network manager is in use, changing the configuration may disrupt connectivity, particularly on headless machines.

Verify your setup:

```bash
python3 --version      # Requires 3.11 or newer
nmcli device status    # Look for a connected Wi-Fi device
```

## Installation

These instructions put the project at `~/wifi-monitor`, as expected by the scheduling examples.

```bash
cd "$HOME"
git clone https://github.com/andrewtplatt/wifi-monitor.git
cd wifi-monitor
python3 wifi_monitor.py --help
```

There are no third-party Python dependencies to install.

## Usage

### Take a measurement

```bash
cd ~/wifi-monitor
python3 wifi_monitor.py
```

Every invocation **takes one sample and exits**. On first use, the script creates `monitor.db` beside `wifi_monitor.py`, unless configured otherwise.

Example output (illustrative):

```text
2026-11-14T18:00:00+00:00 SSID='HomeWiFi' interface='wlan0' status=up gateway=True ping_ms=11.4 signal_score=78 speed_mbps=95.42 speed_test=True
```

The line includes a UTC timestamp, SSID, interface, connectivity status, gateway availability, ping latency in milliseconds, signal-quality score, download speed in Mbps, and whether a speed test was attempted. Unavailable readings show `None`.

Connectivity status can be `up`, `down`, `wifi_disconnected`, or `unknown`. The 0–100 signal score is a **NetworkManager quality score**, not a percentage of physical signal power. The download measurement is a small-file transfer, not a comprehensive broadband benchmark.

### View summary statistics

After at least one sample has been recorded:

```bash
python3 wifi_monitor.py --summary
```

The summary shows counts of reachable/unavailable/unknown samples, the fraction of **conclusive** samples that were reachable, and average measured download speed. **This sample fraction is not a continuous-time uptime percentage.**

### Report outages

```bash
# All recorded outages for an SSID
python3 wifi_monitor.py --outages 'HomeWiFi'

# Outages overlapping the past seven days
python3 wifi_monitor.py --outages 'HomeWiFi' --days 7
```

Use the exact SSID reported by NetworkManager. The script infers outages from the overall sequence of samples: Internet failures and Wi-Fi disconnections can start an outage, and recovery or a switch to another SSID can close it. The reported durations are **estimates based on sample timestamps**. The `--days` option requires `--outages` and a positive number.

### Change the database location

By default, data is stored in `monitor.db` next to the script. Override it with `WIFI_MONITOR_DB`:

```bash
WIFI_MONITOR_DB="$HOME/.local/state/wifi-monitor/monitor.db" python3 wifi_monitor.py
```

The script creates parent directories when necessary. Set the same `WIFI_MONITOR_DB` for scheduled recording and subsequent `--summary` or `--outages` commands, or they will access different databases.

## Automatic monitoring

**Choose one method**, not both. Schedule a sample every minute. Speed tests remain limited to one attempt every 30 minutes per SSID while the Internet is reachable.

### Option A: systemd service and timer

The script exits after each measurement, so use a **oneshot service** and a **timer**, not a permanently running service. This example uses systemd *user* units, avoids running as root, and assumes the project is installed in `~/wifi-monitor`.

Create the unit directory:

```bash
mkdir -p ~/.config/systemd/user
```

Create `~/.config/systemd/user/wifi-monitor.service`:

```ini
[Unit]
Description=Sample Wi-Fi connectivity and performance

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/wifi-monitor/wifi_monitor.py
```

Create `~/.config/systemd/user/wifi-monitor.timer`:

```ini
[Unit]
Description=Run Wi-Fi Monitor every minute

[Timer]
OnCalendar=*-*-* *:*:00
AccuracySec=1s
Persistent=true
Unit=wifi-monitor.service

[Install]
WantedBy=timers.target
```

Reload systemd, enable the timer, and optionally take an immediate sample:

```bash
systemctl --user daemon-reload
systemctl --user enable --now wifi-monitor.timer
systemctl --user start wifi-monitor.service
```

Check its schedule and logs:

```bash
systemctl --user list-timers --all wifi-monitor.timer
journalctl --user -u wifi-monitor.service -n 30 --no-pager
```

**Run after logout/reboot:** Systemd user services normally depend on the user's manager being active. To keep monitoring on an unattended machine, enable lingering (requires administrator privileges):

```bash
sudo loginctl enable-linger "$USER"
```

Stop scheduled sampling with:

```bash
systemctl --user disable --now wifi-monitor.timer
```

It is normal for a successful oneshot service to display `inactive (dead)` between executions. Check the timer and journal to confirm it has run.

### Option B: cron

Open the user crontab:

```bash
crontab -e
```

Add this entry to run every minute:

```cron
* * * * * /usr/bin/flock -n "$HOME/.wifi-monitor.lock" /usr/bin/python3 "$HOME/wifi-monitor/wifi_monitor.py" >> "$HOME/wifi-monitor/monitor.log" 2>&1
```

This records standard output and errors in `~/wifi-monitor/monitor.log`. `flock` (from `util-linux`) prevents overlapping instances if a measurement runs unusually long. Adjust the paths if you installed the project elsewhere. Cron has a minimal environment: use absolute executable paths and set any needed environment variables in your crontab.

Verify and view recent logs:

```bash
crontab -l
tail -n 30 ~/wifi-monitor/monitor.log
```

Logs will grow over time, so configure log rotation or periodic cleanup for long-running installations. Remove the crontab entry to stop monitoring.

## SQLite data

The `measurements` table contains:

| Field | Meaning |
| --- | --- |
| `timestamp` | Sample time in UTC |
| `ssid`, `wifi_interface` | Observed wireless network and interface |
| `connectivity_status` | `up`, `down`, `wifi_disconnected`, or `unknown` |
| `connected`, `reachable` | Connection indicators (0 or 1) |
| `gateway_reachable` | Whether the Wi-Fi gateway answered ping |
| `ping_ms` | Internet ICMP latency, when available |
| `signal_score` | NetworkManager signal score (0–100) |
| `download_mbps` | Measured download speed, when available |
| `speed_test_attempted` | Whether a download test was attempted |

For example, using the optional `sqlite3` command-line client:

```bash
sqlite3 ~/wifi-monitor/monitor.db \
  'SELECT timestamp, ssid, connectivity_status, ping_ms, signal_score, download_mbps FROM measurements ORDER BY id DESC LIMIT 10;'
```

The local database stores SSIDs and connection history. It is not automatically pruned, so consider retention and backups if monitoring for a long period. The repository's `.gitignore` excludes `monitor.db` and `monitor.log*`.

## Troubleshooting

- **`nmcli` missing or no connected Wi-Fi:** Check that NetworkManager is installed and manages the wireless interface (`nmcli device status`).
- **`curl is required`:** Install `curl`; it is checked even for reporting commands.
- **`ping_ms=None`:** ICMP may be blocked or `ping` unavailable. The script can still use HTTPS to determine connectivity.
- **`speed_mbps=None`:** A test only runs when the connection is reported `up` and at least 30 minutes have passed since the previous attempted test for that SSID. A failed test can also produce `None`.
- **No new samples:** Run `python3 ~/wifi-monitor/wifi_monitor.py` manually. Check the cron log or systemd journal, and verify write access to the database directory.
- **Approximate outage times:** Failures are only observed when a sample runs. Short outages may go unnoticed, and boundaries are estimated.

## External connectivity checks

The script pings the default Wi-Fi gateway and attempts ICMP probes to `1.1.1.1` and `8.8.8.8`. If those fail, it tries HTTPS endpoints at Google and Cloudflare. Download-speed checks fetch approximately **2 MB** from Cloudflare using the Wi-Fi interface. Monitoring therefore generates outbound traffic and depends on these endpoints being reachable.

