#!/usr/bin/env python3
# <xbar.title>Net Speed</xbar.title>
# <xbar.version>v1</xbar.version>
# <xbar.desc>Latest / max / avg internet speed from local probe log.</xbar.desc>

import json
import os
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

# __file__ resolves through the xbar symlink to the repo, so the probe/stats
# buttons stay wired to the sibling scripts wherever this repo is cloned.
HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("NETSPEED_DATA_DIR", Path.home() / ".local" / "share" / "netspeed"))
LOG = DATA_DIR / "log.jsonl"
LOCATIONS = DATA_DIR / "locations.json"
PROBE = HERE / "net-speed-probe"
STATS = HERE / "net-speed-stats"


def load_locations():
    """Map of router (gateway) MAC -> friendly location name. Edit
    locations.json to name a spot; unknown routers fall back to their MAC."""
    try:
        return {k.lower(): v for k, v in json.loads(LOCATIONS.read_text()).items()}
    except Exception:
        return {}


def loc_of(row, locs):
    mac = (row.get("gateway_mac") or "").lower()
    if not mac:
        return None  # sample predates fingerprinting
    return locs.get(mac) or f"unknown ({mac[-8:]})"


def load():
    if not LOG.exists():
        return []
    out = []
    for line in LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            r["_ts"] = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            out.append(r)
        except Exception:
            continue
    return out


def mbps(b):
    return b / 1_000_000


def fmt(b):
    # one decimal below 10 Mbps (so 0.5 doesn't round to 0), whole numbers above
    v = mbps(b)
    return f"{v:.1f}" if v < 10 else f"{v:.0f}"


def in_window(rows, td):
    cutoff = datetime.now(timezone.utc) - td
    return [r for r in rows if r["_ts"] >= cutoff]


NOTIFIER = os.path.expanduser("~/Applications/Net Speed.app/Contents/MacOS/notifly")
NOTIFY_STATE = os.path.expanduser("~/.local/state/menubar-notify/netspeed.json")
DEGRADED_RATIO = 0.30  # notify when latest download < 30% of recent average


def send_notification(title, message):
    if os.path.exists(NOTIFIER):
        try:
            subprocess.run([NOTIFIER, "--title", title, "--message", message],
                           capture_output=True, timeout=10)
        except Exception:
            pass


def internet_up():
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            socket.create_connection((host, 53), timeout=1.0).close()
            return True
        except Exception:
            continue
    return False


def maybe_notify(rows):
    """Notify on transitions into 'internet down' or 'speed degraded'."""
    try:
        state = json.loads(open(NOTIFY_STATE).read())
    except Exception:
        state = {}
    down = not internet_up()
    if down and not state.get("down", False):
        send_notification("Net Speed", "Internet appears to be down")

    degraded = False
    if not down and rows:
        recent = in_window(rows, timedelta(hours=24)) or rows
        avg_dl = mean(r["download"] for r in recent)
        latest = rows[-1]["download"]
        if avg_dl > 0 and latest < DEGRADED_RATIO * avg_dl:
            degraded = True
            if not state.get("degraded", False):
                send_notification("Net Speed", f"Slow: {mbps(latest):.0f} Mbps (avg {mbps(avg_dl):.0f})")

    new_state = {"down": down, "degraded": degraded}
    if new_state != state:
        try:
            os.makedirs(os.path.dirname(NOTIFY_STATE), exist_ok=True)
            open(NOTIFY_STATE, "w").write(json.dumps(new_state))
        except Exception:
            pass


rows = load()

if not rows:
    print("net: —")
    print("---")
    print(f"No samples yet | bash={PROBE} terminal=false refresh=true")
else:
    last = rows[-1]
    age_min = (datetime.now(timezone.utc) - last["_ts"]).total_seconds() / 60

    # :wifi: is an SF Symbol — SwiftBar renders it inline in the menu bar
    print(f":wifi: ↓{fmt(last['download'])} ↑{fmt(last['upload'])}")

    print("---")

    locs = load_locations()
    here = loc_of(last, locs)

    srv = last.get("server") or {}
    srv_name = srv.get("name") or srv.get("sponsor") or "?"
    pl = last.get("packet_loss")
    loss = f" · {pl:.0f}% loss" if pl is not None else ""
    where = f"📍 {here} · " if here else ""
    print(
        f"Last: {where}↓ {mbps(last['download']):.1f} ↑ {mbps(last['upload']):.1f} Mbps "
        f"· {last['ping']:.0f} ms{loss} · "
        f"{last['_ts'].astimezone().strftime('%H:%M %d %b')} ({age_min:.0f}m ago)"
    )
    print(f"via {srv_name} · {last.get('isp', '?')} | size=11 color=gray")
    print("---")

    # By location (last 30d), best-download first. Only shown once samples
    # carry a fingerprint — old samples group under "unknown".
    by_loc = {}
    for r in in_window(rows, timedelta(days=30)):
        lbl = loc_of(r, locs)
        if lbl is None:
            continue
        by_loc.setdefault(lbl, []).append(r)
    if by_loc:
        print("By location (30d)")
        for lbl, w in sorted(by_loc.items(), key=lambda kv: -max(r["download"] for r in kv[1])):
            avg_dl = mean(r["download"] for r in w)
            avg_ul = mean(r["upload"] for r in w)
            avg_ping = mean(r["ping"] for r in w if r.get("ping") is not None)
            here_mark = " ←" if lbl == here else ""
            print(f"{lbl} ({len(w)} samples){here_mark}")
            print(f"  avg  ↓ {mbps(avg_dl):6.1f}  ↑ {mbps(avg_ul):6.1f} Mbps  · {avg_ping:.0f} ms | size=12")
        print("---")

    for label, td in [("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("30d", timedelta(days=30))]:
        w = in_window(rows, td)
        if not w:
            continue
        max_dl = max(r["download"] for r in w)
        max_ul = max(r["upload"] for r in w)
        avg_dl = mean(r["download"] for r in w)
        avg_ul = mean(r["upload"] for r in w)
        print(f"{label} ({len(w)} samples)")
        print(f"  max  ↓ {mbps(max_dl):6.1f}  ↑ {mbps(max_ul):6.1f} Mbps")
        print(f"  avg  ↓ {mbps(avg_dl):6.1f}  ↑ {mbps(avg_ul):6.1f} Mbps")
    print("---")
    print(f"Run probe now | bash={PROBE} terminal=false refresh=true")
    print(f"Stats 7d in terminal | bash={STATS} param1=7d terminal=true")
    print(f"Stats by location | bash={STATS} param1=loc terminal=true")
    # Open in VS Code (a real editable window) rather than the default .json
    # handler, which on some machines is a read-only viewer like Safari.
    print(f"Edit locations | bash=/usr/bin/open param1=-b param2=com.microsoft.VSCode param3={LOCATIONS} terminal=false")
    print(f"Open log | bash=/usr/bin/open param1={LOG} terminal=false")

# Fire notifications after the menu has been printed (kept last so a slow
# connectivity check never delays the menu-bar render).
import sys as _sys
_sys.stdout.flush()
maybe_notify(rows)
