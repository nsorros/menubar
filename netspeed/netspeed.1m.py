#!/usr/bin/env python3
# <xbar.title>Net Speed</xbar.title>
# <xbar.version>v1</xbar.version>
# <xbar.desc>Latest / max / avg internet speed from local probe log.</xbar.desc>

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

# __file__ resolves through the xbar symlink to the repo, so the probe/stats
# buttons stay wired to the sibling scripts wherever this repo is cloned.
HERE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("NETSPEED_DATA_DIR", Path.home() / ".local" / "share" / "netspeed"))
LOG = DATA_DIR / "log.jsonl"
PROBE = HERE / "net-speed-probe"
STATS = HERE / "net-speed-stats"


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

    srv = last.get("server") or {}
    srv_name = srv.get("name") or srv.get("sponsor") or "?"
    pl = last.get("packet_loss")
    loss = f" · {pl:.0f}% loss" if pl is not None else ""
    print(
        f"Last: ↓ {mbps(last['download']):.1f} ↑ {mbps(last['upload']):.1f} Mbps "
        f"· {last['ping']:.0f} ms{loss} · "
        f"{last['_ts'].astimezone().strftime('%H:%M %d %b')} ({age_min:.0f}m ago)"
    )
    print(f"via {srv_name} · {last.get('isp', '?')} | size=11 color=gray")
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
    print(f"Open log | bash=/usr/bin/open param1={LOG} terminal=false")
