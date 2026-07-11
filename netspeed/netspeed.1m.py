#!/usr/bin/env python3
# <xbar.title>Net Speed</xbar.title>
# <xbar.version>v1</xbar.version>
# <xbar.desc>Latest / max / avg internet speed from local probe log.</xbar.desc>

import json
import os
import re
import socket
import subprocess
import sys
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
NAMELOC = HERE / "name-location"

# The refresh cadence is encoded in the plugin filename (name.<interval>.py) —
# xbar/SwiftBar has no runtime API for it, so "changing" it means renaming the
# plugin (symlink) and letting the app's folder-watcher pick up the new token.
# Mirrors the claude-usage plugin. Note this controls how often the menu bar
# re-reads the log; the actual speed measurement runs on its own launchd
# schedule (com.nick.netspeed, every 5 min) — "Refresh" forces a probe now.
INTERVAL_PRESETS = [
    ("1m", "1 minute"),
    ("5m", "5 minutes"),
    ("10m", "10 minutes"),
    ("30m", "30 minutes"),
    ("1h", "1 hour"),
]
INTERVAL_RE = re.compile(r"\.(\d+[smhd])\.([^.]+)$")


def plugin_path():
    """Path of the plugin as the bar app sees it — the entry whose filename
    carries the refresh interval. SwiftBar exports it; for xbar we infer it
    from how the script was invoked (the symlink, not the repo target)."""
    return os.environ.get("SWIFTBAR_PLUGIN_PATH") or os.path.abspath(sys.argv[0])


def current_interval():
    m = INTERVAL_RE.search(os.path.basename(plugin_path()))
    return m.group(1) if m else None


def set_interval(new_iv):
    """Rename the plugin (symlink) so a new .<interval>. token takes effect.
    Renames the symlink itself, not its target, so the repo file keeps its
    committed name."""
    path = plugin_path()
    d, base = os.path.split(path)
    new_base = INTERVAL_RE.sub(rf".{new_iv}.\2", base)
    if new_base != base:
        os.rename(path, os.path.join(d, new_base))


def print_interval_menu():
    """A submenu to change how often the bar re-reads the log."""
    path = plugin_path()
    active = current_interval()
    print("Refresh interval | color=gray")
    for iv, label in INTERVAL_PRESETS:
        mark = "✓ " if iv == active else "   "
        print(
            f'--{mark}{label} | bash="{path}" param1=--set-interval param2={iv} '
            "terminal=false refresh=true"
        )
    if active and active not in {iv for iv, _ in INTERVAL_PRESETS}:
        print(f"--(currently every {active}) | color=gray size=11")


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


# Handle the "set refresh interval" action (from the submenu) before rendering.
if len(sys.argv) > 2 and sys.argv[1] == "--set-interval":
    try:
        set_interval(sys.argv[2])
    except Exception:
        pass  # best-effort; a failed rename just leaves the cadence as-is
    sys.exit(0)

rows = load()

if not rows:
    print("net: —")
    print("---")
    print(f"Refresh | bash={PROBE} terminal=false refresh=true")
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

    # By location (last 30d), most-visited first. Only shown once samples
    # carry a fingerprint — old samples group under "unknown".
    by_loc = {}
    for r in in_window(rows, timedelta(days=30)):
        lbl = loc_of(r, locs)
        if lbl is None:
            continue
        by_loc.setdefault(lbl, []).append(r)
    if by_loc:
        print("By location (30d)")
        # Cap to the 3 most-visited locations (by sample count) so the menu
        # stays short and converges on the regular spots (home / office /
        # coffee) rather than a fast-but-rare network.
        ranked = sorted(by_loc.items(), key=lambda kv: -len(kv[1]))
        for lbl, w in ranked[:3]:
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
    # "Refresh" runs a fresh probe (speedtest) and redraws — the manual analog
    # of the periodic launchd probe. Named to match the claude-usage plugin.
    print(f"Refresh | bash={PROBE} terminal=false refresh=true")
    print(f"Stats 7d in terminal | bash={STATS} param1=7d terminal=true")
    print(f"Stats by location | bash={STATS} param1=loc terminal=true")
    # Naming a spot means editing locations.json — but a network you've never
    # named has no line there, so "Edit locations" alone opens a file with
    # nothing to rename. "Name this location" seeds the current router's MAC
    # (with a "rename me" placeholder) and opens the file ready to edit. Lead
    # with it when the current spot is still unnamed.
    unnamed = here is None or here.startswith("unknown")
    name_item = f"Name this location | bash={NAMELOC} terminal=false refresh=true"
    if unnamed:
        print(f"⚠️ {name_item}")
    # Open in VS Code (a real editable window) rather than the default .json
    # handler, which on some machines is a read-only viewer like Safari.
    print(f"Edit locations | bash=/usr/bin/open param1=-b param2=com.microsoft.VSCode param3={LOCATIONS} terminal=false")
    if not unnamed:
        print(name_item)
    print(f"Open log | bash=/usr/bin/open param1={LOG} terminal=false")
    print_interval_menu()

# Fire notifications after the menu has been printed (kept last so a slow
# connectivity check never delays the menu-bar render).
sys.stdout.flush()
maybe_notify(rows)
