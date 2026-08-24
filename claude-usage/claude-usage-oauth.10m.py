#!/usr/bin/python3
# <xbar.title>Claude + Codex Usage</xbar.title>
# <xbar.version>v1.0</xbar.version>
# <xbar.author>Nick</xbar.author>
# <xbar.desc>Claude Code subscription usage plus live Codex rate-limit windows.</xbar.desc>
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
# Codex usage comes from its own chain of four sources — the wham usage API, the
# `codex app-server` JSON-RPC interface, the local rollout logs, and a cache of
# the last good fetch. See the block comment above codex_window() for why, and
# run `claude-usage-oauth.10m.py --codex-debug` to see what each one returns.
#
# No secret is ever printed; only usage percentages and reset times.

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from glob import glob

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDS_FILE = os.path.expanduser("~/.claude/.credentials.json")
BETA_HEADER = "oauth-2025-04-20"
CACHE_FILE = os.path.expanduser("~/.claude-usage-oauth-cache.json")
HISTORY_FILE = os.path.expanduser("~/.claude-usage-history.json")
# Samples of the 5-hour window kept for the burn-rate regression. Only samples
# from the *current* window are useful (a reset makes older ones meaningless),
# so the history is keyed on the window boundary — see same_window.
HISTORY_MAX_SAMPLES = 200
HISTORY_MIN_SAMPLES = 3
HISTORY_MIN_SPAN_MIN = 6  # need a bit of a baseline before a slope means anything
WINDOW_MATCH_TOLERANCE = 300  # seconds of slack when matching a window boundary
CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
CODEX_ARCHIVED_SESSIONS_DIR = os.path.expanduser("~/.codex/archived_sessions")
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_USAGE_PAGE = "https://chatgpt.com/codex/settings/usage"
CODEX_CACHE_FILE = os.path.expanduser("~/.codex-usage-cache.json")
CODEX_PREFERRED_WINDOW_MINUTES = 5 * 60
CODEX_FALLBACK_WINDOW_MINUTES = 7 * 24 * 60
CODEX_API_TIMEOUT = 12
CODEX_RPC_TIMEOUT = 20
TASKS_STATUS_FILE = os.path.expanduser("~/.local/state/tasks/run-status.json")

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


def tasks_failure():
    """Return the latest blocking tasks/Claude failure, if any."""
    try:
        with open(TASKS_STATUS_FILE) as f:
            s = json.load(f)
    except Exception:
        return None
    if s.get("state") != "failed":
        return None
    category = s.get("failure_category")
    if category not in ("subscription_access_disabled", "usage_limit"):
        return None
    label = ("subscription access disabled" if category == "subscription_access_disabled"
             else "usage limit reached")
    return {"label": label, "at": s.get("finished_at"), "message": s.get("message")}


def window_key(resets_at):
    """Identity of the current 5-hour window, as an epoch timestamp of its
    boundary (None if unknown). Compared with a tolerance — see same_window."""
    if not resets_at:
        return None
    try:
        return datetime.fromisoformat(resets_at.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def same_window(a, b):
    """Whether two window boundaries are the same window.

    The API recomputes resets_at per request, so consecutive fetches for one
    window jitter either side of the boundary (…12:59:59.7 vs …13:00:00.2) —
    an exact or minute-truncated match splits the history in half. A genuine
    reset moves the boundary by hours, so a few minutes of slack is safe."""
    if a is None or b is None:
        return a == b
    return abs(a - b) <= WINDOW_MATCH_TOLERANCE


def read_history(resets_at):
    """Samples for the current 5-hour window: [[epoch_seconds, used_pct], ...].

    Returns [] if the stored history belongs to an earlier window."""
    try:
        with open(HISTORY_FILE) as f:
            blob = json.load(f)
    except Exception:
        return []
    if not same_window(blob.get("window"), window_key(resets_at)):
        return []
    samples = blob.get("samples")
    if not isinstance(samples, list):
        return []
    # Belt and braces: nothing older than the window itself can be relevant.
    cutoff = time.time() - 5 * 3600
    return [s for s in samples if isinstance(s, list) and len(s) == 2 and s[0] >= cutoff]


def record_sample(w):
    """Append the current 5-hour utilization to the history and return the
    updated sample list. Called only on a fresh fetch — replaying a cached
    response would fabricate data points."""
    if not w:
        return []
    resets_at = w.get("resets_at")
    samples = read_history(resets_at)
    samples.append([time.time(), w["used"]])
    samples = samples[-HISTORY_MAX_SAMPLES:]
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump({"window": window_key(resets_at), "samples": samples}, f)
    except Exception:
        pass
    return samples


def burn_rate(samples):
    """Least-squares slope of utilization over time, in % used per hour.

    Returns (slope, n, span_minutes) or None when there isn't enough history."""
    if len(samples) < HISTORY_MIN_SAMPLES:
        return None
    xs = [s[0] / 3600.0 for s in samples]
    ys = [float(s[1]) for s in samples]
    span_min = (xs[-1] - xs[0]) * 60
    if span_min < HISTORY_MIN_SPAN_MIN:
        return None
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
    return slope, n, span_min


def fmt_duration(mins):
    mins = int(mins)
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60}m"


def eta_line(w, samples):
    """The 'time to run out' line for the 5-hour window, as a submenu item
    printed under the 5-hour section beside "% used" and "resets ...".

    The projection is a straight-line extrapolation of the fitted burn rate up
    to 100% used. The window is rolling, so usage also ages *out* of it — treat
    this as a trend, not a promise."""
    fit = burn_rate(samples)
    if not fit:
        have = len(samples)
        if have < HISTORY_MIN_SAMPLES:
            need = f"{have}/{HISTORY_MIN_SAMPLES} samples"
        else:
            need = f"need ~{HISTORY_MIN_SPAN_MIN}m of history"
        return f"--est. run-out: collecting data ({need}) | color={GREY}"
    slope, n, span_min = fit
    detail = f"{n} samples over {fmt_duration(span_min)}"

    if slope <= 0.5:  # flat or recovering — nothing meaningful to project
        return f"--est. run-out: not on this trend ({slope:+.0f}%/h, {detail}) | color={GREY}"

    hours_left = w["remaining"] / slope
    mins_left = hours_left * 60
    eta = datetime.now(timezone.utc) + timedelta(hours=hours_left)

    # If the window resets before we'd hit the cap, the cap is never reached.
    reset_dt = None
    if w.get("resets_at"):
        try:
            reset_dt = datetime.fromisoformat(w["resets_at"].replace("Z", "+00:00"))
        except Exception:
            reset_dt = None
    if reset_dt and eta > reset_dt:
        return (f"--est. run-out: after reset at {slope:.0f}%/h — window clears first "
                f"| color={GREEN}")

    when = eta.astimezone().strftime("%H:%M")
    color = RED if mins_left <= 30 else (AMBER if mins_left <= 90 else GREEN)
    return f"--est. run-out: {when} (in {fmt_duration(mins_left)}) · {slope:.0f}%/h | color={color}"


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


# ---------------------------------------------------------------------------
# Codex usage
#
# Four sources, tried in order, first usable answer wins:
#   1. api   — GET /backend-api/wham/usage with the OAuth token the Codex CLI
#              already stores in ~/.codex/auth.json. Authoritative, always current.
#   2. rpc   — `codex app-server` over JSON-RPC (account/rateLimits/read). Slower,
#              since it spawns a process, but it is the CLI's own path: it still
#              works when the stored token needs refreshing, and refreshes
#              auth.json on the way through, repairing source 1 for next time.
#   3. logs  — replay the newest usable record from the local rollout logs. Works
#              offline, but is only ever as fresh as the last Codex run.
#   4. cache — the last good api/rpc answer.
#
# A log record is easily days old while the cache may hold a reading from minutes
# ago, so 3 and 4 compete on captured_at rather than in a fixed order.
# ---------------------------------------------------------------------------

def _codex_home():
    return os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")


def _pick(d, *keys):
    """First non-null value among `keys` — the producers below spell the same
    field several different ways."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def codex_window(limit):
    """Normalise one rate-limit window into the same shape as `window()`.

    The dialects we have to swallow:
        wham API     used_percent / limit_window_seconds / reset_at
        app-server   usedPercent  / windowDurationMins   / resetsAt
        session log  used_percent / window_minutes       / resets_at
    """
    if not isinstance(limit, dict):
        return None
    used = _pick(limit, "used_percent", "usedPercent")
    if used is None:
        return None
    try:
        used = float(used)
    except (TypeError, ValueError):
        return None

    mins = _pick(limit, "window_minutes", "windowDurationMins")
    if mins is None:
        secs = _pick(limit, "limit_window_seconds", "limitWindowSeconds")
        if secs is not None:
            try:
                mins = float(secs) / 60.0
            except (TypeError, ValueError):
                mins = None
    try:
        mins = int(mins) if mins is not None else None
    except (TypeError, ValueError):
        mins = None

    resets_iso = None
    resets_at = _pick(limit, "resets_at", "resetsAt", "reset_at", "resetAt")
    if resets_at is not None:
        try:
            resets_iso = datetime.fromtimestamp(float(resets_at), timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            resets_iso = None

    return {
        "used": used,
        "remaining": max(0.0, 100.0 - used),
        "resets_at": resets_iso,
        "window_minutes": mins,
    }


def codex_collect_windows(*blobs):
    """Every usable window across the given containers, deduped on window length
    and ordered shortest-first, so the session lane precedes the weekly one."""
    found = []
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        for key in ("primary_window", "primary", "secondary_window", "secondary",
                    "individual_limit", "individualLimit"):
            w = codex_window(blob.get(key))
            if w:
                found.append(w)
        extra = _pick(blob, "additional_rate_limits", "additionalRateLimits") or []
        if isinstance(extra, list):
            for lim in extra:
                w = codex_window(lim)
                if w:
                    found.append(w)

    seen, deduped = set(), []
    for w in found:
        if w["window_minutes"] in seen:
            continue
        seen.add(w["window_minutes"])
        deduped.append(w)
    deduped.sort(key=lambda w: (w["window_minutes"] is None, w["window_minutes"] or 0))
    return deduped


def codex_headline(windows):
    """The window that goes in the menu bar: the 5-hour session lane when the
    plan has one, else the weekly lane, else the shortest we found."""
    for target in (CODEX_PREFERRED_WINDOW_MINUTES, CODEX_FALLBACK_WINDOW_MINUTES):
        for w in windows:
            if w["window_minutes"] == target:
                return w
    return windows[0] if windows else None


def codex_snapshot(windows, credits=None, source=None, captured_at=None,
                   plan=None, email=None, reset_credits=None):
    if not windows and not credits:
        return None
    return {
        "windows": windows,
        "headline": codex_headline(windows),
        "credits": credits if isinstance(credits, dict) else None,
        "reset_credits": reset_credits,
        "plan": plan,
        "email": email,
        "source": source,
        "captured_at": captured_at if captured_at is not None else time.time(),
    }


def codex_auth():
    """The OAuth bearer + account id the Codex CLI stores for itself."""
    try:
        with open(os.path.join(_codex_home(), "auth.json")) as f:
            blob = json.load(f)
    except Exception:
        return None
    tokens = blob.get("tokens") or {}
    token = tokens.get("access_token")
    if not token:
        return None
    return {"token": token, "account_id": tokens.get("account_id")}


def codex_from_api():
    """Source 1 — the endpoint the Codex usage dashboard itself reads."""
    auth = codex_auth()
    if not auth:
        return None
    headers = {
        "Authorization": f"Bearer {auth['token']}",
        "Accept": "application/json",
        "User-Agent": "claude-usage-menubar/1.0",
    }
    if auth.get("account_id"):
        headers["chatgpt-account-id"] = auth["account_id"]
    req = urllib.request.Request(CODEX_USAGE_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=CODEX_API_TIMEOUT) as resp:
        data = json.loads(resp.read().decode())

    # `additional_rate_limits` sits beside `rate_limit`, not inside it.
    windows = codex_collect_windows(data.get("rate_limit") or {}, data)
    return codex_snapshot(
        windows,
        credits=data.get("credits"),
        source="api",
        plan=data.get("plan_type"),
        email=data.get("email"),
        reset_credits=(data.get("rate_limit_reset_credits") or {}).get("available_count"),
    )


def codex_from_rpc():
    """Source 2 — drive the CLI's own JSON-RPC server.

    Read-only sandbox with approvals never, so it cannot touch anything. Worth
    the process spawn only as a fallback: it keeps working when the stored token
    has gone stale, because the CLI refreshes it on the way through.
    """
    if not shutil.which("codex"):
        return None
    proc = subprocess.Popen(
        ["codex", "-s", "read-only", "-a", "never", "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )
    # A dedicated reader thread keeps a silent server from wedging the plugin;
    # select() on a text-mode pipe can also hide already-buffered lines.
    lines = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:
                lines.put(line)
        except Exception:
            pass
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    limits, account = None, None
    try:
        for msg in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "claude-usage-menubar",
                                       "title": "claude-usage-menubar",
                                       "version": "1.0"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "account/read", "params": {}},
        ):
            proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

        deadline = time.time() + CODEX_RPC_TIMEOUT
        while limits is None:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:  # server closed its side
                break
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if obj.get("id") == 2:
                limits = obj.get("result") or {}
            elif obj.get("id") == 3:
                account = (obj.get("result") or {}).get("account") or {}
    except Exception:
        return None
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    if not limits:
        return None
    rl = limits.get("rateLimits") or {}
    # rateLimitsByLimitId can carry several buckets; "codex" is the coding quota,
    # others (e.g. "premium") are unrelated and are often reported all-null.
    by_id = limits.get("rateLimitsByLimitId") or {}
    windows = codex_collect_windows(by_id.get("codex") or {}, rl)
    if not windows:
        for bucket in by_id.values():
            windows = codex_collect_windows(bucket)
            if windows:
                break
    return codex_snapshot(
        windows,
        credits=rl.get("credits"),
        source="rpc",
        plan=rl.get("planType") or (account or {}).get("planType"),
        email=(account or {}).get("email"),
        reset_credits=(limits.get("rateLimitResetCredits") or {}).get("availableCount"),
    )


def _codex_log_records(path):
    """Every rate-limit payload in one rollout log, oldest first."""
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                payload = obj.get("payload") or {}
                if obj.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                rate_limits = payload.get("rate_limits")
                if rate_limits:
                    out.append((obj.get("timestamp"), rate_limits))
    except OSError:
        pass
    return out


def _codex_log_time(stamp, path):
    """When a log record was true — its own timestamp, else the file's mtime."""
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def codex_from_logs(max_files=40):
    """Source 3 — the newest *usable* record in the local rollout logs.

    Two traps the earlier scanner fell into, both of which surfaced as a bare
    dash in the menu bar:
      * it returned at the first file holding any `rate_limits` object, so a
        single session whose only record was unusable masked every older usable
        one;
      * Codex emits more than one limit bucket, and the non-"codex" buckets
        (limit_id "premium") carry null windows with a credits-only payload.
    So keep walking until a record actually yields a window.
    """
    files = glob(os.path.join(CODEX_SESSIONS_DIR, "*", "*", "*", "*.jsonl"))
    files += glob(os.path.join(CODEX_ARCHIVED_SESSIONS_DIR, "*.jsonl"))
    try:
        files.sort(key=os.path.getmtime, reverse=True)
    except OSError:
        pass

    fallback = None  # a credits-only record, if that is genuinely all there is
    for path in files[:max_files]:
        for stamp, rate_limits in reversed(_codex_log_records(path)):
            by_id = _pick(rate_limits, "rate_limits_by_limit_id", "rateLimitsByLimitId") or {}
            windows = codex_collect_windows(by_id.get("codex") or {}, rate_limits)
            if not windows:
                for bucket in by_id.values():
                    windows = codex_collect_windows(bucket)
                    if windows:
                        break
            captured = _codex_log_time(stamp, path)
            if windows:
                return codex_snapshot(
                    windows, credits=rate_limits.get("credits"), source="logs",
                    captured_at=captured, plan=rate_limits.get("plan_type"))
            if fallback is None and rate_limits.get("credits"):
                fallback = codex_snapshot(
                    [], credits=rate_limits.get("credits"), source="logs",
                    captured_at=captured, plan=rate_limits.get("plan_type"))
    return fallback


def codex_write_cache(snap):
    try:
        with open(CODEX_CACHE_FILE, "w") as f:
            json.dump(snap, f)
    except Exception:
        pass


def codex_read_cache():
    try:
        with open(CODEX_CACHE_FILE) as f:
            snap = json.load(f)
    except Exception:
        return None
    if not isinstance(snap, dict) or not snap.get("captured_at"):
        return None
    snap["source"] = "cache"
    return snap


def read_codex_usage():
    """The best Codex reading available, with its provenance attached."""
    live = {"api": codex_from_api, "rpc": codex_from_rpc, "logs": codex_from_logs}
    forced = os.environ.get("CODEX_USAGE_SOURCE")
    if forced in live:  # for testing one source in isolation
        try:
            return live[forced]()
        except Exception:
            return None

    for fetch_one in (codex_from_api, codex_from_rpc):
        try:
            snap = fetch_one()
        except Exception:
            snap = None
        if snap:
            codex_write_cache(snap)
            return snap

    # Neither live source answered. A cached reading from minutes ago beats a log
    # record from days ago, so pick on recency rather than on order.
    candidates = []
    for fetch_one in (codex_read_cache, codex_from_logs):
        try:
            snap = fetch_one()
        except Exception:
            snap = None
        if snap:
            candidates.append(snap)
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.get("captured_at") or 0)


def codex_window_label(w):
    if not w:
        return "5h"
    mins = w.get("window_minutes")
    if mins == CODEX_PREFERRED_WINDOW_MINUTES:
        return "5h"
    if mins == CODEX_FALLBACK_WINDOW_MINUTES:
        return "7d"
    if isinstance(mins, int):
        if mins % 1440 == 0:
            return f"{mins // 1440}d"
        if mins % 60 == 0:
            return f"{mins // 60}h"
        return f"{mins}m"
    return "usage"


def codex_window_title(w):
    """Long form of the label, for a dropdown heading."""
    label = codex_window_label(w)
    return {"5h": "5-hour session", "7d": "Weekly"}.get(label, f"{label} window")


CODEX_STALE_AFTER = 30 * 60  # a replayed reading older than this is worth flagging


def codex_stale_age(snap):
    """Seconds since the reading was true, when that is worth showing."""
    if not snap or snap.get("source") in ("api", "rpc"):
        return None
    captured = snap.get("captured_at")
    if not captured:
        return None
    age = time.time() - captured
    return age if age >= CODEX_STALE_AFTER else None


def codex_credits_line(credits):
    if not isinstance(credits, dict):
        return None
    if credits.get("unlimited"):
        return "Credits  ·  unlimited"
    balance = credits.get("balance")
    if balance is None:
        return None
    try:
        pretty = f"{float(balance):,.0f}"
    except (TypeError, ValueError):
        pretty = str(balance)
    if not credits.get("has_credits") and pretty == "0":
        return "Credits  ·  none"
    return f"Credits  ·  {pretty}"


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
    codex = read_codex_usage()
    codex_head = codex.get("headline") if codex else None
    codex_label = codex_window_label(codex_head)
    codex_stale = codex_stale_age(codex)
    tasks_problem = tasks_failure()

    # ---- menu bar title ----
    title = (
        f":sparkle: {dot_for(five['remaining'] if five else None)}5h{pct(five)}"
        f"·{dot_for(week['remaining'] if week else None)}7d{pct(week)}"
        f"·{dot_for(codex_head['remaining'] if codex_head else None)}C{codex_label}"
        # "~" marks a replayed reading — the live sources were unreachable, so
        # the number is as old as the age spelled out in the dropdown.
        f"{'~' if codex_stale else ''}{pct(codex_head)}"
    )
    if tasks_problem:
        title = "⚠ Claude paused · " + title
    # SwiftBar only supports one color for the whole status item, so the title
    # stays neutral and the per-window state is carried by the dots.
    title_color = GREY if stale_age is not None else None
    title_params = "font=Menlo size=13"
    if title_color:
        title_params = f"color={title_color} " + title_params
    print(f"{title} | {title_params}")

    # ---- dropdown ----
    print("---")
    if tasks_problem:
        when = ""
        try:
            when = " since " + datetime.fromisoformat(
                tasks_problem["at"].replace("Z", "+00:00")).astimezone().strftime("%H:%M")
        except Exception:
            pass
        print(f"⚠ tasks blocked: {tasks_problem['label']}{when} | color={RED}")
        if tasks_problem.get("message"):
            print(f"--{tasks_problem['message'][:180]} | color={GREY} size=11")
        print("---")
    print(f"Claude{(' ' + plan) if plan else ''} usage | size=12 color={GREY}")
    print("---")
    section("5-hour session", five)
    if five:
        print(eta_line(five, read_history(five.get("resets_at"))))
    section("Weekly · all models", week)
    if sonnet:
        section("Weekly · Sonnet", sonnet)
    if opus:
        section("Weekly · Opus", opus)

    print("---")
    codex_plan = (codex.get("plan") or "") if codex else ""
    print(f"Codex{(' ' + codex_plan) if codex_plan else ''} usage | size=12 color={GREY}")
    print("---")
    if codex and codex.get("windows"):
        for w in codex["windows"]:
            section(f"Codex · {codex_window_title(w)}", w)
    elif codex:
        print(f"Codex · no rate-limit window reported | color={GREY}")
    else:
        print(f"Codex · unavailable from all sources | color={GREY}")
        print(f"--tried the usage API, `codex app-server`, and the session logs "
              f"| color={GREY} size=11")

    if codex:
        credits = codex_credits_line(codex.get("credits"))
        if credits:
            print(f"{credits} | color={GREY}")
        resets_available = codex.get("reset_credits")
        if resets_available:
            plural = "s" if resets_available != 1 else ""
            print(f"Rate-limit reset{plural}  ·  {resets_available} available | color={GREY}")
        origin = {
            "api": "live · usage API",
            "rpc": "live · codex app-server",
            "logs": "replayed from session logs",
            "cache": "last good fetch",
        }.get(codex.get("source"), codex.get("source") or "unknown")
        if codex_stale:
            print(f"⚠ Codex reading is {fmt_age(codex_stale)} — {origin} "
                  f"| color={AMBER} size=11")
        else:
            print(f"source: {origin} | color={GREY} size=11")
    print(f"Open Codex usage page | href={CODEX_USAGE_PAGE}")

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


def codex_debug():
    """Report what each Codex source returns, independently — run by hand when
    the menu bar shows something you don't believe."""
    for name, fetch_one in (("api", codex_from_api), ("rpc", codex_from_rpc),
                            ("logs", codex_from_logs), ("cache", codex_read_cache)):
        try:
            snap = fetch_one()
        except Exception as e:
            print(f"{name:6} ERROR  {type(e).__name__}: {e}")
            continue
        if not snap:
            print(f"{name:6} —      no usable reading")
            continue
        age = time.time() - (snap.get("captured_at") or time.time())
        windows = ", ".join(
            f"{codex_window_label(w)} {w['remaining']:.0f}% left" for w in snap["windows"]
        ) or "no windows"
        print(f"{name:6} OK     {windows}  ({fmt_age(age)}, plan={snap.get('plan')})")
    chosen = read_codex_usage()
    print(f"\nchosen: {chosen.get('source') if chosen else None}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--codex-debug":
        codex_debug()
        return

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
    record_sample(window(data.get("five_hour")))
    maybe_notify(data)
    render(data, plan)


if __name__ == "__main__":
    main()
