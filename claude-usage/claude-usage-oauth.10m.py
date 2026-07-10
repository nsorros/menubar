#!/usr/bin/python3
# <xbar.title>Claude Usage (5h + Weekly)</xbar.title>
# <xbar.version>v1.0</xbar.version>
# <xbar.author>Nick</xbar.author>
# <xbar.desc>Claude Code subscription usage: 5-hour session + weekly % remaining, read from the Anthropic OAuth usage endpoint using the local Claude Code credentials (keychain).</xbar.desc>
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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDS_FILE = os.path.expanduser("~/.claude/.credentials.json")
BETA_HEADER = "oauth-2025-04-20"
CACHE_FILE = os.path.expanduser("~/.claude-usage-oauth-cache.json")

# At login / wake-from-sleep the network often isn't up yet when SwiftBar fires
# the plugin. Retry a few times to ride out that gap before giving up.
FETCH_RETRIES = 3
FETCH_BACKOFF = 2  # seconds between attempts

GREEN = "#30d158"
AMBER = "#ffd60a"
RED = "#ff453a"
GREY = "#8e8e93"


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

    # ---- menu bar title ----
    title = f":sparkle: {dot_for(five['remaining'] if five else None)}5h{pct(five)}·{dot_for(week['remaining'] if week else None)}7d{pct(week)}"
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


def main():
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
    render(data, plan)


if __name__ == "__main__":
    main()
