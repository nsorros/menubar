#!/usr/bin/python3
# <xbar.title>Claude + Codex Usage</xbar.title>
# <xbar.version>v1.0</xbar.version>
# <xbar.author>Nick</xbar.author>
# <xbar.desc>Claude Code subscription usage plus the latest local Codex rate-limit window.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
#
# How it works:
#   1. Reads the OAuth access token from the macOS keychain item
#      "Claude Code-credentials" (falls back to ~/.claude/.credentials.json).
#   2. GET https://api.anthropic.com/api/oauth/usage with that bearer token
#      and the `anthropic-beta: oauth-2025-04-20` header.
#   3. Response gives utilization (% USED, 0-100) + resets_at per window:
#        five_hour  -> the 5-hour rolling session window
#        seven_day  -> the weekly window (all models)
#        seven_day_sonnet / seven_day_opus -> per-model weekly windows
#        extra_usage -> pay-as-you-go overage (if enabled)
#   We display % REMAINING = 100 - utilization.
#
# No secret is ever printed; only usage percentages and reset times.

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from glob import glob

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDS_FILE = os.path.expanduser("~/.claude/.credentials.json")
BETA_HEADER = "oauth-2025-04-20"
CACHE_FILE = os.path.expanduser("~/.claude-usage-oauth-cache.json")
CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
CODEX_PREFERRED_WINDOW_MINUTES = 5 * 60
CODEX_FALLBACK_WINDOW_MINUTES = 7 * 24 * 60

# At login / wake-from-sleep the network often isn't up yet when SwiftBar fires
# the plugin. Retry a few times to ride out that gap before giving up.
FETCH_RETRIES = 3
FETCH_BACKOFF = 2  # seconds between attempts

GREEN = "#30d158"
AMBER = "#ffd60a"
RED = "#ff453a"
GREY = "#8e8e93"

# The refresh cadence is encoded in the plugin's filename (name.<interval>.py) —
# xbar/SwiftBar has no runtime API for it, so "changing" it means renaming the
# plugin (symlink) and letting the app's folder-watcher pick up the new token.
INTERVAL_PRESETS = [
    ("1m", "1 minute"),
    ("5m", "5 minutes"),
    ("10m", "10 minutes"),
    ("30m", "30 minutes"),
    ("1h", "1 hour"),
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


def read_creds():
    """Return the parsed credential blob, or None."""
    try:
        out = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return json.loads(out.stdout.strip())
    except Exception:
        pass
    try:
        with open(CREDS_FILE) as f:
            return json.load(f)
    except Exception:
        return None


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
    print("⚠ Claude | color=" + AMBER)
    print("---")
    print(headline)
    for d in detail:
        print(d)
    print("---")
    print("Open usage page | href=https://claude.ai/settings/usage")
    print("Refresh | refresh=true")
    sys.exit(0)


def window(d):
    if not isinstance(d, dict):
        return None
    u = d.get("utilization")
    if u is None:
        return None
    used = float(u)
    return {"used": used, "remaining": max(0.0, 100.0 - used), "resets_at": d.get("resets_at")}


def color_for(remaining):
    if remaining is None:
        return GREY
    if remaining <= 0:
        return RED
    if remaining <= 20:
        return AMBER
    return GREEN


def dot_for(remaining):
    if remaining is None:
        return "⚪"
    if remaining <= 0:
        return "🔴"
    if remaining <= 20:
        return "🟡"
    return "🟢"


def fmt_reset(iso):
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None
    delta = (t - datetime.now(timezone.utc)).total_seconds()
    when = t.astimezone().strftime("%a %d %b %H:%M")
    if delta <= 0:
        return f"{when} (due)"
    mins = int(delta // 60)
    if mins < 60:
        rel = f"{mins}m"
    elif mins < 1440:
        rel = f"{mins // 60}h {mins % 60}m"
    else:
        rel = f"{mins // 1440}d {(mins % 1440) // 60}h"
    return f"{when} (in {rel})"


def pct(w):
    return f"{w['remaining']:.0f}%" if w else "—"


def codex_window_from_limit(limit):
    if not isinstance(limit, dict):
        return None
    used = limit.get("used_percent")
    if used is None:
        return None
    try:
        used = float(used)
    except (TypeError, ValueError):
        return None
    resets_at = limit.get("resets_at")
    resets_iso = None
    if resets_at:
        try:
            resets_iso = datetime.fromtimestamp(float(resets_at), timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            resets_iso = None
    return {
        "used": used,
        "remaining": max(0.0, 100.0 - used),
        "resets_at": resets_iso,
        "window_minutes": limit.get("window_minutes"),
    }


def latest_codex_rate_limits(max_files=30):
    files = glob(os.path.join(CODEX_SESSIONS_DIR, "*", "*", "*", "*.jsonl"))
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    latest = None
    for path in files[:max_files]:
        try:
            with open(path) as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = obj.get("payload") or {}
                    if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
                        continue
                    rate_limits = payload.get("rate_limits")
                    if rate_limits:
                        latest = (obj.get("timestamp"), rate_limits)
        except OSError:
            continue
        if latest:
            return latest
    return None


def read_codex_window(preferred_minutes=CODEX_PREFERRED_WINDOW_MINUTES):
    latest = latest_codex_rate_limits()
    if not latest:
        return None

    _, rate_limits = latest
    for key in ("primary", "secondary", "individual_limit"):
        w = codex_window_from_limit(rate_limits.get(key))
        if w and w.get("window_minutes") == preferred_minutes:
            return {"kind": key, **w}

    for key in ("primary", "secondary", "individual_limit"):
        w = codex_window_from_limit(rate_limits.get(key))
        if w and w.get("window_minutes") == CODEX_FALLBACK_WINDOW_MINUTES:
            return {"kind": key, **w}
    return None


def codex_window_label(w):
    if not w:
        return "5h"
    if w.get("window_minutes") == CODEX_PREFERRED_WINDOW_MINUTES:
        return "5h"
    if w.get("window_minutes") == CODEX_FALLBACK_WINDOW_MINUTES:
        return "7d"
    mins = w.get("window_minutes")
    if isinstance(mins, (int, float)):
        return f"{int(mins)}m"
    return "usage"


def section(label, w):
    if not w:
        print(f"{label}: n/a | color={GREY}")
        return
    print(f"{label}  ·  {w['remaining']:.0f}% left | color={color_for(w['remaining'])}")
    print(f"--{w['used']:.0f}% used")
    r = fmt_reset(w["resets_at"])
    if r:
        print(f"--resets {r}")


def fetch(token):
    """Fetch usage, retrying briefly to ride out a startup network gap.

    Raises HTTPError on auth/HTTP errors (no point retrying those); retries
    only transient connection failures.
    """
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
            "User-Agent": "claude-usage-menubar/1.0",
        },
    )
    last_err = None
    for attempt in range(FETCH_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError:
            raise  # auth / HTTP status — retrying won't help
        except Exception as e:
            last_err = e
            if attempt < FETCH_RETRIES - 1:
                time.sleep(FETCH_BACKOFF)
    raise last_err


def render(data, plan="", stale_age=None):
    five = window(data.get("five_hour"))
    week = window(data.get("seven_day"))
    sonnet = window(data.get("seven_day_sonnet"))
    opus = window(data.get("seven_day_opus"))
    extra = data.get("extra_usage") or {}
    codex = read_codex_window()
    codex_label = codex_window_label(codex)

    # ---- menu bar title ----
    title = (
        f":sparkle: {dot_for(five['remaining'] if five else None)}5h{pct(five)}"
        f"·{dot_for(week['remaining'] if week else None)}7d{pct(week)}"
        f"·{dot_for(codex['remaining'] if codex else None)}C{codex_label}{pct(codex)}"
    )
    # SwiftBar only supports one color for the whole status item, so the title
    # stays neutral and the per-window state is carried by the dots.
    title_color = GREY if stale_age is not None else None
    title_params = "font=Menlo size=13"
    if title_color:
        title_params = f"color={title_color} " + title_params
    print(f"{title} | {title_params}")

    # ---- dropdown ----
    print("---")
    print(f"Claude{(' ' + plan) if plan else ''} usage | size=12 color={GREY}")
    print("---")
    section("5-hour session", five)
    section("Weekly · all models", week)
    if sonnet:
        section("Weekly · Sonnet", sonnet)
    if opus:
        section("Weekly · Opus", opus)

    print("---")
    print(f"Codex usage | size=12 color={GREY}")
    if codex:
        section(f"Codex · {codex_label} window", codex)
    else:
        print(f"Codex · 5h/7d window: n/a | color={GREY}")

    if extra.get("is_enabled"):
        used = extra.get("used_credits")
        limit = extra.get("monthly_limit")
        cur = extra.get("currency") or ""
        if used is not None and limit is not None:
            print(f"Extra usage  ·  {cur}{used:.2f} / {cur}{limit:.0f} | color={GREY}")
        else:
            print(f"Extra usage enabled | color={GREY}")

    print("---")
    if stale_age is not None:
        print(f"⚠ offline — showing data from {fmt_age(stale_age)} | color={AMBER} size=11")
    print(f"Updated {datetime.now().astimezone().strftime('%H:%M:%S')} | color={GREY} size=11")
    print("Open usage page | href=https://claude.ai/settings/usage")
    print("Refresh | refresh=true")
    print_interval_menu()


def print_interval_menu():
    """A submenu to change how often the bar re-fetches usage."""
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
NOTIFY_STATE = os.path.expanduser("~/.local/state/menubar-notify/claude-usage.json")
LOW_THRESHOLD = 10  # % remaining


def send_notification(title, message):
    if os.path.exists(NOTIFIER):
        try:
            subprocess.run([NOTIFIER, "--title", title, "--message", message],
                           capture_output=True, timeout=10)
        except Exception:
            pass


def maybe_notify(data):
    """Notify once when a window crosses into 'low' (<=10% remaining). Resets when
    it recovers, so the next crossing (e.g. after a reset) notifies again."""
    try:
        state = json.loads(open(NOTIFY_STATE).read())
    except Exception:
        state = {}
    changed = False
    for key, raw, label in (("five_low", "five_hour", "5-hour session"),
                            ("week_low", "seven_day", "Weekly limit")):
        w = window(data.get(raw))
        if not w:
            continue
        low = w["remaining"] <= LOW_THRESHOLD
        if low and not state.get(key, False):
            send_notification("Claude Usage", f"{label} at {w['remaining']:.0f}% — running low")
        if low != state.get(key, False):
            state[key] = low
            changed = True
    if changed:
        try:
            os.makedirs(os.path.dirname(NOTIFY_STATE), exist_ok=True)
            open(NOTIFY_STATE, "w").write(json.dumps(state))
        except Exception:
            pass


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--set-interval":
        try:
            set_interval(sys.argv[2])
        except Exception:
            pass  # best-effort; a failed rename just leaves the cadence as-is
        return

    creds = read_creds()
    if not creds:
        fail("No Claude credentials found", "Sign in once via the claude CLI, then refresh.")

    oauth = creds.get("claudeAiOauth", creds)
    token = oauth.get("accessToken")
    plan = (oauth.get("subscriptionType") or "").replace("_", " ").title()
    if not token:
        fail("Credential blob had no accessToken")

    try:
        data = fetch(token)
    except urllib.error.HTTPError as e:
        # Auth/HTTP errors are real (not a startup blip) — surface them, but
        # prefer showing last-good numbers over a bare warning when we have them.
        cached, age = read_cache()
        if cached is not None:
            render(cached, plan, stale_age=age)
            return
        if e.code in (401, 403):
            fail("Auth expired", "Run `claude` once to refresh the token, then refresh.")
        fail(f"HTTP {e.code} from usage endpoint")
    except Exception as e:
        # Transient network failure (most common at login / wake) — fall back
        # to the last good fetch instead of flashing the warning.
        cached, age = read_cache()
        if cached is not None:
            render(cached, plan, stale_age=age)
            return
        fail("Could not reach usage endpoint", str(e))

    write_cache(data)
    maybe_notify(data)
    render(data, plan)


if __name__ == "__main__":
    main()
