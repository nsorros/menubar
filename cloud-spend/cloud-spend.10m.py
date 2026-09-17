#!/usr/bin/python3
# <xbar.title>Cloud Spend</xbar.title>
# <xbar.version>v2.0</xbar.version>
# <xbar.author>Nick</xbar.author>
# <xbar.desc>What the cloud is costing: OpenRouter credit balance + spend, and AWS daily/monthly spend against budget.</xbar.desc>
# <xbar.dependencies>python3,awscli</xbar.dependencies>
#
# How it works — two independent sources, either can fail without the other:
#
#   OpenRouter
#     1. Resolves an OpenRouter API key (env var -> keychain -> config file).
#     2. GET https://openrouter.ai/api/v1/credits  -> account-wide purchased
#        credits + lifetime usage; remaining = total_credits - total_usage.
#     3. GET https://openrouter.ai/api/v1/key      -> this key's own spend
#        (daily/weekly/monthly) and its optional spend limit.
#
#   AWS (the Finant organization, read from its management account)
#     1. Resolves read-only billing credentials (env -> keychain), falling back
#        to an SSO profile (`finant`) when none are stored.
#     2. `aws budgets describe-budgets` -> actual spend against each daily /
#        monthly budget. Free, and AWS refreshes it a few times a day.
#     3. `aws ce get-cost-and-usage` -> month-to-date spend by service. Cost
#        Explorer bills $0.01 per request, so this runs every few hours, not
#        on every refresh.
#
# No secret is ever printed; only balances and spend figures.

import json
import os
import re
import shutil
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

STATE_DIR = os.path.expanduser("~/.local/state/cloud-spend")
CACHE_FILE = os.path.join(STATE_DIR, "openrouter.json")
AWS_CACHE_FILE = os.path.join(STATE_DIR, "aws.json")
# Where these lived when the plugin was openrouter-credits; moved on first run.
LEGACY_CACHE_FILE = os.path.expanduser("~/.openrouter-credits-cache.json")

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

# ---- AWS -----------------------------------------------------------------
# Budgets and Cost Explorer are only org-wide from the management account, so
# that's where the read-only billing user lives. Its access key is stored in
# the keychain as "<AccessKeyId> <SecretAccessKey>" (tab or space separated).
AWS_KEYCHAIN = "aws-billing-readonly"
AWS_SSO_PROFILE = os.environ.get("AWS_BILLING_PROFILE", "finant")
AWS_CONFIG = os.path.expanduser(os.environ.get("AWS_CONFIG_FILE", "~/.aws/config"))
# Both APIs are global and served from us-east-1, whatever the profile says.
AWS_REGION = "us-east-1"
# Cost Explorer costs $0.01 a call; every 6h is ~$1.20 a month.
AWS_CE_EVERY_SECS = float(os.environ.get("AWS_CE_EVERY_HOURS", "6")) * 3600
# A budget reads amber from this share of its limit, red once it's spent.
AWS_WARN_PCT = float(os.environ.get("AWS_BUDGET_WARN_PCT", "80"))
AWS_CONSOLE = "https://console.aws.amazon.com/costmanagement/home"

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

# Health levels, ordered so the title can show the worst across sources.
LEVELS = ["unknown", "ok", "low", "critical"]
LEVEL_COLOR = {"unknown": GREY, "ok": GREEN, "low": AMBER, "critical": RED}
LEVEL_DOT = {"unknown": "⚪", "ok": "🟢", "low": "🟡", "critical": "🔴"}

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


def keychain(service):
    """A generic-password secret from the login keychain, or None."""
    try:
        out = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


def read_key():
    """Resolve the API key. xbar launches from launchd with a bare environment,
    so the shell export is only a fallback — the keychain is the reliable one."""
    env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env:
        return env
    key = keychain(KEYCHAIN_SERVICE)
    if key:
        return key
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
    token = keychain(ANT_TOKEN_KEYCHAIN)
    if token:
        return token
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

    ant's endpoint windows in whole days; days=1 == the last 24h. Returns None
    when there's nothing to ask for (no admin token, or a window ant can't
    answer), and raises when a configured token was there but the fetch failed —
    the caller needs that difference to explain itself accurately.

    Retries like `fetch()` does: this fires at login/wake too, and a one-shot
    attempt would lose the race with the network coming up."""
    token = read_ant_token()
    if not token or hours != 24:
        return None
    url = f"{ANT_BASE_URL}/api/admin/llm_costs?days=1"
    req = urllib.request.Request(url, headers={
        "X-Admin-Token": token,
        "User-Agent": "cloud-spend-menubar/2.0",
    })
    last_err = None
    for attempt in range(FETCH_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError:
            raise  # bad/expired token — retrying won't help
        except Exception as e:
            last_err = e
            if attempt < FETCH_RETRIES - 1:
                time.sleep(FETCH_BACKOFF)
    else:
        raise last_err
    items = [{"label": k.get("kind") or "unknown", "calls": int(k.get("calls") or 0),
              "cost": float(k.get("cost") or 0.0)}
             for k in (data.get("by_kind") or [])]
    return {"source": "ant (app)", "total": float(data.get("total_cost") or 0.0),
            "items": items}


def cost_sources(hours=COST_WINDOW_HOURS):
    """Both per-source cost breakdowns that reported, biggest spend first.

    Computed once so the trailing-window total can feed the menu-bar title AND
    the dropdown section off a single ant fetch. Never raises — a broken source
    is simply left out, but we report *that* it broke: a source that's failing
    and a source that isn't configured look identical downstream otherwise, and
    the dropdown used to blame a missing token for what was really a dropped
    connection.

    Returns (sources, failed) where `failed` names the sources that errored."""
    sources, failed = [], []
    for name, get in (("recorder", meeting_recorder_costs), ("ant", ant_costs)):
        try:
            s = get(hours)
        except Exception:
            failed.append(name)
            continue
        if s:
            sources.append(s)
    return sorted(sources, key=lambda x: x["total"], reverse=True), failed


def render_cost_breakdown(sources, failed=(), hours=COST_WINDOW_HOURS):
    """Print the 'where do costs happen' dropdown section from `cost_sources()`."""
    print(f"Where costs happen · last {hours}h | size=12 color={GREY}")
    if not sources:
        if "ant" in failed:
            # A stored token that couldn't be used is a very different problem
            # from no token at all; don't send anyone to the README over a
            # network blip at wake.
            print(f"ant didn't answer | color={GREY}")
            print(f"--Its costs are missing from this window. Retries on the | color={GREY} size=11")
            print(f"--next refresh; use Refresh to retry now. | color={GREY} size=11")
            return
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
    if "ant" in failed:
        # The total below is real but partial — say so rather than quietly
        # under-reporting the spend.
        print(f"ant didn't answer — total excludes it | color={GREY} size=11")
    print(f"Tracked total  ·  {usd(tracked)} | color={GREY}")


def migrate_legacy_state():
    """Carry the cache and notification state over from openrouter-credits, so
    the rename doesn't cost the offline fallback or re-fire a low-balance alert."""
    for old, new in ((LEGACY_CACHE_FILE, CACHE_FILE), (LEGACY_NOTIFY_STATE, NOTIFY_STATE)):
        try:
            if os.path.exists(old) and not os.path.exists(new):
                os.makedirs(os.path.dirname(new), exist_ok=True)
                os.replace(old, new)
        except Exception:
            pass


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def write_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f)
    except Exception:
        pass


def write_cache(data):
    write_json(CACHE_FILE, {"fetched_at": time.time(), "data": data})


def read_cache():
    """Return (data, age_seconds) from the last good fetch, or (None, None)."""
    blob = read_json(CACHE_FILE)
    try:
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


def or_level(remaining):
    if remaining is None:
        return "unknown"
    if remaining <= CRITICAL_USD:
        return "critical"
    if remaining <= LOW_USD:
        return "low"
    return "ok"


def color_for(remaining):
    return LEVEL_COLOR[or_level(remaining)]


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
            "User-Agent": "cloud-spend-menubar/2.0",
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


def get_openrouter():
    """The OpenRouter balance, falling back to the last good fetch.

    Returns (data, stale_age, problem): `data` is None only when there's
    nothing at all to show, and `problem` is a list of lines explaining why."""
    key = read_key()
    if not key:
        return None, None, [
            "No OpenRouter API key found",
            "--Store one in the keychain:",
            "--security add-generic-password -s openrouter-api-key -a $USER -w sk-or-...",
            f"--…or write it to {KEY_FILE}",
        ]
    try:
        data = fetch_all(key)
    except urllib.error.HTTPError as e:
        # Auth/HTTP errors are real (not a startup blip) — surface them, but
        # prefer showing last-good numbers over a bare warning when we have them.
        cached, age = read_cache()
        if cached is not None:
            return cached, age, None
        if e.code in (401, 403):
            return None, None, ["OpenRouter auth rejected",
                                "--The stored key is invalid or revoked."]
        return None, None, [f"HTTP {e.code} from OpenRouter"]
    except Exception as e:
        # Transient network failure (most common at login / wake) — fall back
        # to the last good fetch instead of flashing the warning.
        cached, age = read_cache()
        if cached is not None:
            return cached, age, None
        return None, None, ["Could not reach OpenRouter", f"--{e}"]
    write_cache(data)
    return data, None, None


# ---- AWS -----------------------------------------------------------------

class AwsError(Exception):
    def __init__(self, msg, auth=False):
        super().__init__(msg)
        self.auth = auth


# What the CLI says when the credentials, rather than the request, are the
# problem — an expired SSO session most of all.
AWS_AUTH_HINTS = re.compile(
    r"sso|expired|unable to locate credentials|invalidclienttokenid|"
    r"signaturedoesnotmatch|unrecognizedclient|could not be found", re.I)


def aws_bin():
    """launchd hands xbar a bare PATH, so look where Homebrew puts the CLI too."""
    for p in (shutil.which("aws"), "/opt/homebrew/bin/aws", "/usr/local/bin/aws"):
        if p and os.path.exists(p):
            return p
    return None


def read_aws_creds():
    """Read-only billing key as (id, secret), from env or keychain, else None.

    `security -w` prints a secret that contains a tab as hex, and the key is
    stored tab-separated (that's how `--output text` prints it), so undo that."""
    kid = os.environ.get("AWS_BILLING_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("AWS_BILLING_SECRET_ACCESS_KEY", "").strip()
    if kid and secret:
        return kid, secret
    raw = keychain(AWS_KEYCHAIN)
    if not raw:
        return None
    if re.fullmatch(r"(?:[0-9a-f]{2})+", raw):
        try:
            raw = bytes.fromhex(raw).decode()
        except Exception:
            return None
    parts = raw.split()
    return (parts[0], parts[1]) if len(parts) == 2 else None


def sso_profile_exists():
    try:
        with open(AWS_CONFIG) as f:
            return re.search(rf"^\[profile {re.escape(AWS_SSO_PROFILE)}\]", f.read(), re.M) is not None
    except Exception:
        return False


def aws(args, creds):
    """Run one read-only AWS CLI call and return its parsed JSON."""
    env = dict(os.environ)
    for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID",
              "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        env.pop(k, None)
    cmd = [aws_bin(), *args, "--region", AWS_REGION, "--output", "json"]
    if creds:
        env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"] = creds
    else:
        cmd += ["--profile", AWS_SSO_PROFILE]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40, env=env)
    except subprocess.TimeoutExpired:
        raise AwsError("AWS didn't answer in time")
    if r.returncode != 0:
        lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
        msg = lines[-1] if lines else f"aws exited {r.returncode}"
        msg = re.sub(r"^aws: \[ERROR\]: ", "", msg)
        raise AwsError(msg, auth=bool(AWS_AUTH_HINTS.search(msg)))
    return json.loads(r.stdout or "{}")


def aws_budgets(creds, account_id):
    """Every cost budget with its actual (and forecast) spend."""
    data = aws(["budgets", "describe-budgets", "--account-id", account_id], creds)
    out = []
    for b in data.get("Budgets") or []:
        if b.get("BudgetType", "COST") != "COST":
            continue
        spend = b.get("CalculatedSpend") or {}
        forecast = (spend.get("ForecastedSpend") or {}).get("Amount")
        name = b.get("BudgetName") or "budget"
        out.append({
            "name": name,
            # "ant-ant-daily" and "ant-ant-monthly" both belong to "ant-ant".
            "scope": re.sub(r"[-_ ](daily|monthly|quarterly|annually)$", "", name, flags=re.I),
            "unit": b.get("TimeUnit"),
            # A budget without an account filter covers the whole organization.
            "org": not (b.get("CostFilters") or b.get("FilterExpression")),
            "limit": float((b.get("BudgetLimit") or {}).get("Amount") or 0),
            "actual": float((spend.get("ActualSpend") or {}).get("Amount") or 0),
            "forecast": float(forecast) if forecast is not None else None,
            "updated": b.get("LastUpdatedTime"),
        })
    return out


def aws_services_mtd(creds):
    """Month-to-date spend by service from Cost Explorer.

    Cost Explorer's data trails by a day or so and rejects a window reaching
    past it, so step the end back until it answers. Returns None on the 1st,
    when there's no finished day in the month yet."""
    today = datetime.now(timezone.utc).date()
    start = today.replace(day=1)
    for back in range(3):
        end = today - timedelta(days=back)
        if end <= start:
            return None
        try:
            data = aws(["ce", "get-cost-and-usage",
                        "--time-period", f"Start={start},End={end}",
                        "--granularity", "MONTHLY", "--metrics", "UnblendedCost",
                        "--group-by", "Type=DIMENSION,Key=SERVICE"], creds)
        except AwsError as e:
            if "DataUnavailable" in str(e) or "not available" in str(e):
                continue
            raise
        groups = (data.get("ResultsByTime") or [{}])[0].get("Groups") or []
        items = [{"label": g["Keys"][0],
                  "cost": float(g["Metrics"]["UnblendedCost"]["Amount"])} for g in groups]
        items = sorted((i for i in items if i["cost"] >= 0.005),
                       key=lambda i: i["cost"], reverse=True)
        return {"through": str(end - timedelta(days=1)), "items": items,
                "total": sum(i["cost"] for i in items)}
    return None


def get_aws():
    """AWS budgets (every refresh) + service breakdown (every few hours).

    Returns (data, stale_age, error): `data` is None when AWS isn't set up or
    has never answered; `error` is the AwsError from this attempt, if any."""
    cache = read_json(AWS_CACHE_FILE) or {}
    creds = read_aws_creds()
    if not aws_bin() or (not creds and not sso_profile_exists()):
        return None, None, None  # not configured on this machine
    try:
        account_id = (os.environ.get("AWS_BILLING_ACCOUNT_ID")
                      or cache.get("account_id")
                      or aws(["sts", "get-caller-identity"], creds)["Account"])
        budgets = aws_budgets(creds, account_id)
        services, services_at = cache.get("services"), cache.get("services_at", 0)
        if time.time() - services_at > AWS_CE_EVERY_SECS:
            try:
                services, services_at = aws_services_mtd(creds), time.time()
            except AwsError:
                pass  # keep the last breakdown; budgets are the headline
        fresh = {"fetched_at": time.time(), "account_id": account_id,
                 "budgets": budgets, "services": services, "services_at": services_at,
                 "auth": "key" if creds else "sso"}
        write_json(AWS_CACHE_FILE, fresh)
        return fresh, None, None
    except (AwsError, KeyError, ValueError) as e:
        if not isinstance(e, AwsError):
            e = AwsError(f"Unexpected AWS response: {e}")
        if cache.get("budgets") is not None:
            return cache, time.time() - cache.get("fetched_at", 0), e
        return None, None, e


def budget_level(b):
    if b["limit"] <= 0:
        return "unknown"
    pct = 100 * b["actual"] / b["limit"]
    if pct >= 100:
        return "critical"
    if pct >= AWS_WARN_PCT:
        return "low"
    return "ok"


def aws_level(data):
    if not data or not data.get("budgets"):
        return "unknown"
    return max((budget_level(b) for b in data["budgets"]), key=LEVELS.index)


def aws_month_to_date(data):
    """Org-wide spend this month: the org budget if there is one, else the sum
    of the per-account monthly budgets."""
    monthly = [b for b in data.get("budgets") or [] if b["unit"] == "MONTHLY"]
    org = [b for b in monthly if b["org"]]
    if org:
        return org[0]["actual"]
    return sum(b["actual"] for b in monthly) if monthly else None


def fmt_budget(b):
    return f"{usd(b['actual'])} of {usd(b['limit'])}"


def render_aws(data, stale_age, err):
    print(f"AWS · Finant organization | size=12 color={GREY}")
    if err is not None and data is None:
        render_aws_error(err)
        return
    budgets = data.get("budgets") or []
    if not budgets:
        print(f"No budgets set up | color={GREY}")
    unit_label = {"DAILY": "today", "MONTHLY": "this month",
                  "QUARTERLY": "this quarter", "ANNUALLY": "this year"}

    # The org-wide budget first, then one line per account with its budgets
    # (today, this month) nested underneath.
    for b in (b for b in budgets if b["org"]):
        print(f"Organization {unit_label.get(b['unit'], '')}  ·  {fmt_budget(b)} "
              f"| color={LEVEL_COLOR[budget_level(b)]}")
        if b["forecast"] is not None:
            print(f"--forecast {usd(b['forecast'])}")
    scopes = {}
    for b in budgets:
        if not b["org"]:
            scopes.setdefault(b["scope"], []).append(b)
    order = ["DAILY", "MONTHLY", "QUARTERLY", "ANNUALLY"]
    for scope, bs in sorted(scopes.items()):
        bs.sort(key=lambda b: order.index(b["unit"]) if b["unit"] in order else 99)
        worst = max((budget_level(b) for b in bs), key=LEVELS.index)
        summary = "  ·  ".join(f"{unit_label.get(b['unit'], b['unit'])} {usd(b['actual'])}" for b in bs)
        print(f"{scope}  ·  {summary} | color={LEVEL_COLOR[worst]}")
        for b in bs:
            line = f"--{unit_label.get(b['unit'], b['unit'])}  {fmt_budget(b)}"
            if b["forecast"] is not None and b["unit"] != "DAILY":
                line += f"  (forecast {usd(b['forecast'])})"
            print(f"{line} | color={LEVEL_COLOR[budget_level(b)]}")

    services = data.get("services")
    if services and services.get("items"):
        print(f"By service · month to date | color={GREY}")
        print(f"--through {services['through']}  ·  {usd(services['total'])} | color={GREY} size=11")
        for it in services["items"][:8]:
            print(f"--{it['label']}  {usd(it['cost'])} | font=Menlo size=11")

    updated = [b["updated"] for b in budgets if b.get("updated")]
    if updated:
        try:
            last = datetime.fromisoformat(max(updated)).astimezone().strftime("%H:%M")
            print(f"Budgets as of {last} · AWS refreshes them a few times a day | color={GREY} size=11")
        except ValueError:
            pass
    if err is not None:
        render_aws_error(err, stale_age)


def render_aws_error(err, stale_age=None):
    if stale_age is not None:
        print(f"⚠ AWS: showing data from {fmt_age(stale_age)} | color={AMBER} size=11")
    if err.auth and not read_aws_creds():
        print(f"AWS SSO sign-in needed — log in | color={AMBER} bash={aws_bin()} "
              f"param1=sso param2=login param3=--profile param4={AWS_SSO_PROFILE} "
              "terminal=true refresh=true")
        print(f"--Or store a read-only key to stop this (see README) | color={GREY} size=11")
    elif err.auth:
        print(f"AWS rejected the billing key | color={AMBER}")
        print(f"--{err} | color={GREY} size=11")
    else:
        print(f"AWS didn't answer | color={AMBER}")
        print(f"--{err} | color={GREY} size=11")


def render(or_data, or_stale, or_problem, aws_data, aws_stale, aws_err):
    credits = (or_data or {}).get("credits") or {}
    keyinfo = (or_data or {}).get("key") or {}
    left = remaining_of(credits)

    # Computed once and reused: the trailing-window total goes in the title, the
    # per-source breakdown fills the dropdown, off a single ant fetch.
    sources, failed_sources = cost_sources()
    tracked = sum(s["total"] for s in sources)
    aws_configured = aws_data is not None or aws_err is not None

    # ---- menu bar title ----
    # OpenRouter balance (+ its trailing spend), then AWS month-to-date. One
    # dot for the worst state across both: xbar colors the whole status item
    # at once, so the text stays neutral.
    level = max(or_level(left), aws_level(aws_data), key=LEVELS.index)
    parts = []
    if or_data is None:
        parts.append("OR ⚠")
    else:
        or_part = f"OR {usd(left)}"
        if tracked > 0:
            or_part += f" ({usd(tracked)}/{COST_WINDOW_HOURS}h)"
        parts.append(or_part)
    if aws_configured:
        mtd = aws_month_to_date(aws_data) if aws_data else None
        parts.append(f"AWS {usd(mtd)}/mo" if mtd is not None else "AWS ⚠")
    title_params = "font=Menlo size=13"
    if or_stale is not None or aws_stale is not None:
        title_params = f"color={GREY} " + title_params
    print(f":creditcard: {LEVEL_DOT[level]}{' · '.join(parts)} | {title_params}")

    # ---- dropdown: OpenRouter ----
    print("---")
    print(f"OpenRouter | size=12 color={GREY}")
    if or_problem:
        print(f"{or_problem[0]} | color={AMBER}")
        for line in or_problem[1:]:
            print(line)
    elif left is None:
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
    if or_stale is not None:
        print(f"⚠ OpenRouter offline — showing data from {fmt_age(or_stale)} | color={AMBER} size=11")

    # Where the shared balance actually goes, merged from the tools that log
    # their own OpenRouter cost (meeting recorder + ant).
    print("---")
    try:
        render_cost_breakdown(sources, failed_sources)
    except Exception:
        pass  # a cost source must never break the balance readout

    # ---- dropdown: AWS ----
    print("---")
    if aws_configured:
        try:
            render_aws(aws_data, aws_stale, aws_err)
        except Exception as e:
            print(f"AWS section failed: {e} | color={GREY} size=11")
    else:
        print(f"AWS · not set up | size=12 color={GREY}")
        print(f"--Store a read-only billing key (see README) | color={GREY} size=11")

    print("---")
    print(f"Updated {datetime.now().astimezone().strftime('%H:%M:%S')} | color={GREY} size=11")
    print("OpenRouter: buy credits | href=https://openrouter.ai/settings/credits")
    print("OpenRouter: activity | href=https://openrouter.ai/activity")
    print(f"AWS: billing console | href={AWS_CONSOLE}")
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
NOTIFY_STATE = os.path.expanduser("~/.local/state/menubar-notify/cloud-spend.json")
LEGACY_NOTIFY_STATE = os.path.expanduser("~/.local/state/menubar-notify/openrouter-credits.json")


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


def maybe_notify(or_data, aws_data):
    """Notify once when something crosses into 'low' or 'critical' — the
    OpenRouter balance, or any AWS budget. State resets when it recovers (a
    top-up, a new day/month), so the next crossing notifies again."""
    state = read_json(NOTIFY_STATE) or {}
    if "level" in state:  # openrouter-credits kept a single level
        state = {"openrouter": state.pop("level")}
    changed = False

    def check(key, level, title, message):
        nonlocal changed
        if level == "unknown" or state.get(key) == level:
            return
        if level != "ok":
            send_notification(title, message)
        state[key] = level
        changed = True

    if or_data is not None:
        left = remaining_of(or_data.get("credits") or {})
        check("openrouter", or_level(left), "OpenRouter credits",
              f"{usd(left)} left — running {or_level(left)}")
    for b in (aws_data or {}).get("budgets") or []:
        lvl = budget_level(b)
        check(f"aws:{b['name']}", lvl, "AWS budget",
              f"{b['name']}: {fmt_budget(b)}"
              + (" — over budget" if lvl == "critical" else " — nearly spent"))
    if changed:
        write_json(NOTIFY_STATE, state)


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--set-interval":
        try:
            set_interval(sys.argv[2])
        except Exception:
            pass  # best-effort; a failed rename just leaves the cadence as-is
        return

    migrate_legacy_state()
    or_data, or_stale, or_problem = get_openrouter()
    try:
        aws_data, aws_stale, aws_err = get_aws()
    except Exception as e:  # AWS must never take the OpenRouter readout down
        aws_data, aws_stale, aws_err = None, None, AwsError(str(e))
    maybe_notify(or_data if or_stale is None else None,
                 aws_data if aws_stale is None else None)
    render(or_data, or_stale, or_problem, aws_data, aws_stale, aws_err)


if __name__ == "__main__":
    main()
