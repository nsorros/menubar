#!/usr/bin/python3
# <xbar.title>OpenRouter Credits</xbar.title>
# <xbar.version>v1.0</xbar.version>
# <xbar.author>Nick</xbar.author>
# <xbar.desc>OpenRouter credit balance: how much is left on the account, plus this key's daily/weekly/monthly spend.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
#
# How it works:
#   1. Resolves an OpenRouter API key (env var -> keychain -> config file).
#   2. GET https://openrouter.ai/api/v1/credits  -> account-wide purchased
#      credits + lifetime usage; remaining = total_credits - total_usage.
#   3. GET https://openrouter.ai/api/v1/key      -> this key's own spend
#      (daily/weekly/monthly) and its optional spend limit.
#
# No secret is ever printed; only balances and spend figures.

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CREDITS_URL = "https://openrouter.ai/api/v1/credits"
KEY_URL = "https://openrouter.ai/api/v1/key"
KEYCHAIN_SERVICE = "openrouter-api-key"
KEY_FILE = os.path.expanduser("~/.config/openrouter/key")
CACHE_FILE = os.path.expanduser("~/.openrouter-credits-cache.json")

# ---- "Where do the costs happen?" sources -------------------------------
# The OpenRouter balance is shared across tools, so the account total alone
# can't say what burned it. Two tools log their own per-call cost (each asks
# OpenRouter for the authoritative usage.cost), and we merge them here into a
# trailing-24h breakdown by source.
COST_WINDOW_HOURS = int(os.environ.get("OPENROUTER_COST_WINDOW_HOURS", "24"))

# 1) The meeting recorder's local SQLite ledger (one row per transcription
#    call). Path mirrors the recorder's own STATE_DIR default.
MREC_STATE_DIR = os.path.expanduser(
    os.environ.get("MEETING_RECORDER_STATE_DIR", "~/.local/state/meeting-recorder"))
MREC_COST_DB = os.path.join(MREC_STATE_DIR, "openrouter-costs.db")

# 2) The ant app's server-side llm_calls, exposed at /api/admin/llm_costs.
#    Needs the operator admin token (env -> keychain -> file); absent = ant is
#    simply omitted from the breakdown, the recorder half still shows.
ANT_BASE_URL = os.environ.get("ANT_BASE_URL", "https://ant.finant.ai").rstrip("/")
ANT_TOKEN_KEYCHAIN = "ant-admin-token"
ANT_TOKEN_FILE = os.path.expanduser("~/.config/ant/admin-token")

# At login / wake-from-sleep the network often isn't up yet when xbar fires the
# plugin. Retry a few times to ride out that gap before giving up.
FETCH_RETRIES = 3
FETCH_BACKOFF = 2  # seconds between attempts

# Balance is money, not a percentage, so the thresholds are absolute dollars.
# Override per-machine with OPENROUTER_LOW / OPENROUTER_CRITICAL.
LOW_USD = float(os.environ.get("OPENROUTER_LOW", "20"))
CRITICAL_USD = float(os.environ.get("OPENROUTER_CRITICAL", "5"))

GREEN = "#30d158"
AMBER = "#ffd60a"
RED = "#ff453a"
GREY = "#8e8e93"

# The refresh cadence is encoded in the plugin's filename (name.<interval>.py) —
# xbar/SwiftBar has no runtime API for it, so "changing" it means renaming the
# plugin (symlink) and letting the app's folder-watcher pick up the new token.
INTERVAL_PRESETS = [
    ("10m", "10 minutes"),
    ("30m", "30 minutes"),
    ("1h", "1 hour"),
    ("6h", "6 hours"),
]
INTERVAL_RE = re.compile(r"\.(\d+[smhd])\.([^.]+)$")


def plugin_path():
    """Path of the plugin file as the bar app sees it — the entry whose
    filename carries the refresh interval. SwiftBar exports it; xbar we infer
    from how the script was invoked."""
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


def read_key():
    """Resolve the API key. xbar launches from launchd with a bare environment,
    so the shell export is only a fallback — the keychain is the reliable one."""
    env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env:
        return env
    try:
        out = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    try:
        with open(KEY_FILE) as f:
            return f.read().strip()
    except Exception:
        return None


def read_ant_token():
    """Resolve the ant operator admin token. Env -> keychain -> file, like the
    OpenRouter key. Returns None when unset (ant is then omitted, not an error)."""
    env = os.environ.get("ANT_ADMIN_TOKEN", "").strip()
    if env:
        return env
    try:
        out = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", ANT_TOKEN_KEYCHAIN, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    try:
        with open(ANT_TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return None


def meeting_recorder_costs(hours=COST_WINDOW_HOURS):
    """Trailing-window spend from the meeting recorder's local ledger, by model.

    Opened read-only so a missing DB (recorder not yet run) contributes nothing
    rather than creating an empty file. Returns None when there's no ledger."""
    if not os.path.exists(MREC_COST_DB):
        return None
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    try:
        uri = "file:" + urllib.request.pathname2url(MREC_COST_DB) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            rows = conn.execute(
                "SELECT model, COUNT(*), SUM(cost) FROM openrouter_costs "
                "WHERE created_at >= ? GROUP BY model ORDER BY SUM(cost) DESC",
                (since,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    items = [{"label": r[0] or "unknown", "calls": int(r[1] or 0),
              "cost": float(r[2] or 0.0)} for r in rows]
    return {"source": "Meeting recorder", "total": sum(i["cost"] for i in items),
            "items": items}


def ant_costs(hours=COST_WINDOW_HOURS):
    """Trailing-window ant spend by feature (kind), via /api/admin/llm_costs.

    ant's endpoint windows in whole days; days=1 == the last 24h. Best-effort:
    any failure (no token, network, non-24h window) returns None so the recorder
    half still renders. Returns None when no admin token is configured."""
    token = read_ant_token()
    if not token or hours != 24:
        return None
    url = f"{ANT_BASE_URL}/api/admin/llm_costs?days=1"
    req = urllib.request.Request(url, headers={
        "X-Admin-Token": token,
        "User-Agent": "openrouter-credits-menubar/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    items = [{"label": k.get("kind") or "unknown", "calls": int(k.get("calls") or 0),
              "cost": float(k.get("cost") or 0.0)}
             for k in (data.get("by_kind") or [])]
    return {"source": "ant (app)", "total": float(data.get("total_cost") or 0.0),
            "items": items}


def cost_sources(hours=COST_WINDOW_HOURS):
    """Both per-source cost breakdowns that reported, biggest spend first.

    Computed once so the trailing-window total can feed the menu-bar title AND
    the dropdown section off a single ant fetch. Never raises — a broken source
    is simply left out."""
    try:
        sources = [s for s in (meeting_recorder_costs(hours), ant_costs(hours)) if s]
    except Exception:
        return []
    return sorted(sources, key=lambda x: x["total"], reverse=True)


def render_cost_breakdown(sources, hours=COST_WINDOW_HOURS):
    """Print the 'where do costs happen' dropdown section from `cost_sources()`."""
    print("---")
    print(f"Where costs happen · last {hours}h | size=12 color={GREY}")
    if not sources:
        print(f"No per-source cost data yet | color={GREY}")
        print(f"--Runs once the meeting recorder transcribes, or once | color={GREY} size=11")
        print(f"--an ant admin token is stored (see README) | color={GREY} size=11")
        return
    tracked = sum(s["total"] for s in sources)
    for s in sources:
        print(f"{s['source']}  ·  {usd(s['total'])}")
        for it in s["items"][:6]:
            print(f"--{it['label']}  {usd(it['cost'])}  ({it['calls']} calls) | font=Menlo size=11")
        if not s["items"]:
            print(f"--nothing in the last {hours}h | color={GREY} size=11")
    print(f"Tracked total  ·  {usd(tracked)} | color={GREY}")


def write_cache(data):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"fetched_at": time.time(), "data": data}, f)
    except Exception:
        pass


def read_cache():
    """Return (data, age_seconds) from the last good fetch, or (None, None)."""
    try:
        with open(CACHE_FILE) as f:
            blob = json.load(f)
        return blob["data"], time.time() - blob.get("fetched_at", 0)
    except Exception:
        return None, None


def fmt_age(secs):
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins}m ago"
    if mins < 1440:
        return f"{mins // 60}h {mins % 60}m ago"
    return f"{mins // 1440}d ago"


def fail(headline, *detail):
    print("⚠ OR | color=" + AMBER)
    print("---")
    print(headline)
    for d in detail:
        print(d)
    print("---")
    print("Open OpenRouter credits | href=https://openrouter.ai/settings/credits")
    print("Refresh | refresh=true")
    sys.exit(0)


def color_for(remaining):
    if remaining is None:
        return GREY
    if remaining <= CRITICAL_USD:
        return RED
    if remaining <= LOW_USD:
        return AMBER
    return GREEN


def dot_for(remaining):
    if remaining is None:
        return "⚪"
    if remaining <= CRITICAL_USD:
        return "🔴"
    if remaining <= LOW_USD:
        return "🟡"
    return "🟢"


def usd(v):
    return f"${v:,.2f}" if v is not None else "—"


def fetch(url, key):
    """Fetch a JSON endpoint, retrying briefly to ride out a startup network gap.

    Raises HTTPError on auth/HTTP errors (no point retrying those); retries
    only transient connection failures.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": "openrouter-credits-menubar/1.0",
        },
    )
    last_err = None
    for attempt in range(FETCH_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode()).get("data") or {}
        except urllib.error.HTTPError:
            raise  # auth / HTTP status — retrying won't help
        except Exception as e:
            last_err = e
            if attempt < FETCH_RETRIES - 1:
                time.sleep(FETCH_BACKOFF)
    raise last_err


def fetch_all(key):
    credits = fetch(CREDITS_URL, key)
    try:
        keyinfo = fetch(KEY_URL, key)
    except Exception:
        keyinfo = {}  # per-key detail is a bonus; the balance is the headline
    return {"credits": credits, "key": keyinfo}


def remaining_of(credits):
    total = credits.get("total_credits")
    used = credits.get("total_usage")
    if total is None or used is None:
        return None
    return float(total) - float(used)


def render(data, stale_age=None):
    credits = data.get("credits") or {}
    keyinfo = data.get("key") or {}
    left = remaining_of(credits)

    # Computed once and reused: the trailing-window total goes in the title, the
    # per-source breakdown fills the dropdown, off a single ant fetch.
    sources = cost_sources()
    tracked = sum(s["total"] for s in sources)

    # ---- menu bar title ----
    # Balance (with the traffic-light dot) plus what's been spent in the window,
    # so "where's the money going" is visible without opening the menu.
    title = f":creditcard: {dot_for(left)}{usd(left)}"
    if tracked > 0:
        title += f" · {usd(tracked)}/{COST_WINDOW_HOURS}h"
    # xbar/SwiftBar colors the whole status item at once, so the title stays
    # neutral and the balance state is carried by the dot.
    title_params = "font=Menlo size=13"
    if stale_age is not None:
        title_params = f"color={GREY} " + title_params
    print(f"{title} | {title_params}")

    # ---- dropdown ----
    print("---")
    print(f"OpenRouter credits | size=12 color={GREY}")
    print("---")
    if left is None:
        print(f"Balance unavailable | color={GREY}")
    else:
        print(f"Remaining  ·  {usd(left)} | color={color_for(left)}")
        print(f"--{usd(credits.get('total_usage'))} used of {usd(credits.get('total_credits'))}")

    if keyinfo:
        print(f"This key  ·  {usd(keyinfo.get('usage'))} total | color={GREY}")
        print(f"--today {usd(keyinfo.get('usage_daily'))}")
        print(f"--this week {usd(keyinfo.get('usage_weekly'))}")
        print(f"--this month {usd(keyinfo.get('usage_monthly'))}")
        limit_left = keyinfo.get("limit_remaining")
        if limit_left is not None:
            reset = keyinfo.get("limit_reset") or "period"
            print(f"Key limit  ·  {usd(limit_left)} left of {usd(keyinfo.get('limit'))} "
                  f"| color={color_for(float(limit_left))}")
            print(f"--resets {reset}")
        if keyinfo.get("is_free_tier"):
            print(f"Free tier | color={GREY}")

    # Where the shared balance actually goes, merged from the tools that log
    # their own OpenRouter cost (meeting recorder + ant).
    try:
        render_cost_breakdown(sources)
    except Exception:
        pass  # a cost source must never break the balance readout

    print("---")
    if stale_age is not None:
        print(f"⚠ offline — showing data from {fmt_age(stale_age)} | color={AMBER} size=11")
    print(f"Updated {datetime.now().astimezone().strftime('%H:%M:%S')} | color={GREY} size=11")
    print("Buy credits | href=https://openrouter.ai/settings/credits")
    print("Activity | href=https://openrouter.ai/activity")
    print("Refresh | refresh=true")
    print_interval_menu()


def print_interval_menu():
    """A submenu to change how often the bar re-fetches the balance."""
    path = plugin_path()
    active = current_interval()
    print(f"Refresh interval | color={GREY}")
    for iv, label in INTERVAL_PRESETS:
        mark = "✓ " if iv == active else "   "
        print(
            f'--{mark}{label} | bash="{path}" param1=--set-interval param2={iv} '
            "terminal=false refresh=true"
        )
    if active and active not in {iv for iv, _ in INTERVAL_PRESETS}:
        print(f"--(currently every {active}) | color={GREY} size=11")


NOTIFIER = os.path.expanduser("~/Applications/Claude Usage.app/Contents/MacOS/notifly")
NOTIFY_STATE = os.path.expanduser("~/.local/state/menubar-notify/openrouter-credits.json")


def send_notification(title, message):
    """Prefer the bundled notifier if it's around (nicer icon), else fall back
    to osascript, which needs no app bundle."""
    if os.path.exists(NOTIFIER):
        try:
            subprocess.run([NOTIFIER, "--title", title, "--message", message],
                           capture_output=True, timeout=10)
            return
        except Exception:
            pass
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def maybe_notify(data):
    """Notify once when the balance crosses into 'low' or 'critical'. State
    resets when it recovers (i.e. after a top-up), so the next crossing
    notifies again."""
    left = remaining_of(data.get("credits") or {})
    if left is None:
        return
    level = "critical" if left <= CRITICAL_USD else "low" if left <= LOW_USD else "ok"
    try:
        state = json.loads(open(NOTIFY_STATE).read())
    except Exception:
        state = {}
    if state.get("level") == level:
        return
    if level != "ok":
        send_notification("OpenRouter credits", f"{usd(left)} left — running {level}")
    try:
        os.makedirs(os.path.dirname(NOTIFY_STATE), exist_ok=True)
        open(NOTIFY_STATE, "w").write(json.dumps({"level": level}))
    except Exception:
        pass


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--set-interval":
        try:
            set_interval(sys.argv[2])
        except Exception:
            pass  # best-effort; a failed rename just leaves the cadence as-is
        return

    key = read_key()
    if not key:
        fail("No OpenRouter API key found",
             "Store one in the keychain:",
             "security add-generic-password -s openrouter-api-key -a $USER -w sk-or-...",
             f"…or write it to {KEY_FILE}")

    try:
        data = fetch_all(key)
    except urllib.error.HTTPError as e:
        # Auth/HTTP errors are real (not a startup blip) — surface them, but
        # prefer showing last-good numbers over a bare warning when we have them.
        cached, age = read_cache()
        if cached is not None:
            render(cached, stale_age=age)
            return
        if e.code in (401, 403):
            fail("Auth rejected", "The stored OpenRouter key is invalid or revoked.")
        fail(f"HTTP {e.code} from OpenRouter")
    except Exception as e:
        # Transient network failure (most common at login / wake) — fall back
        # to the last good fetch instead of flashing the warning.
        cached, age = read_cache()
        if cached is not None:
            render(cached, stale_age=age)
            return
        fail("Could not reach OpenRouter", str(e))

    write_cache(data)
    maybe_notify(data)
    render(data)


if __name__ == "__main__":
    main()
