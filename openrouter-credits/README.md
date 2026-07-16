# OpenRouter credits

Shows what's left of your OpenRouter balance in the menu bar, so a job doesn't
fail at 2am because the account quietly ran dry.

The title is the remaining balance with a traffic-light dot — green above $20,
amber at or below it, red at or below $5 — followed by what's been spent in the
last 24h once any is tracked (e.g. `$7.00 · $0.57/24h`), so "where's the money
going" is visible without opening the menu. The dropdown breaks it down:

- **Remaining** — `total_credits - total_usage` for the whole account.
- **This key** — the spend attributed to the key the plugin authenticates with,
  split into today / this week / this month, plus its lifetime total. If the key
  has a spend limit set, its remaining headroom shows too.

- **Where costs happen · last 24h** — the shared balance broken down by *which
  tool* burned it, since the account is used by more than one. Each source logs
  its own per-call cost (asking OpenRouter for the authoritative `usage.cost`):
  - **Meeting recorder** — read from its local SQLite ledger
    `~/.local/state/meeting-recorder/openrouter-costs.db`, grouped by model.
  - **ant (app)** — fetched from `GET /api/admin/llm_costs?days=1`, grouped by
    feature (`meeting_prep`, `email`, `seed`, …).

  Each source is best-effort: a missing ledger or an unset ant token just drops
  that row (the other still shows), and any failure here never affects the
  balance readout above. The "Tracked total" sums only the sources that report —
  it can be less than the account's daily usage if something spends off-ledger.

Two endpoints back the balance itself:

- `GET https://openrouter.ai/api/v1/credits` — account-wide credits and usage.
- `GET https://openrouter.ai/api/v1/key` — the key's own spend and limit. This
  one is a bonus; if it fails the balance still renders.

The result is cached to `~/.openrouter-credits-cache.json`, so a transient API
hiccup or a wake-from-sleep still shows the last-known numbers (greyed out, with
an "offline" note) rather than a bare warning.

## The API key

No secret lives in this repo — the key is read from your machine at runtime, in
this order:

1. `OPENROUTER_API_KEY` in the environment.
2. The macOS Keychain item **`openrouter-api-key`**.
3. The file `~/.config/openrouter/key`.

**Use the keychain.** xbar is launched by launchd with a bare environment, so it
does *not* inherit the `OPENROUTER_API_KEY` you export from your shell profile —
that path only works when xbar happens to be started from a terminal.

```sh
security add-generic-password -s openrouter-api-key -a "$USER" -w sk-or-v1-...
```

A read-only key is plenty; the plugin only ever GETs.

## The ant admin token (for the 24h cost breakdown)

The **Meeting recorder** row needs nothing — it reads the recorder's local
ledger, which fills the first time the recorder transcribes on the
cost-logging version (`mrec costs` shows the same numbers).

The **ant (app)** row needs the operator admin token, resolved the same
env → keychain → file way (absent = ant is simply left out):

1. `ANT_ADMIN_TOKEN` in the environment.
2. The macOS Keychain item **`ant-admin-token`**.
3. The file `~/.config/ant/admin-token`.

```sh
security add-generic-password -s ant-admin-token -a "$USER" -w <ANT_ADMIN_TOKEN>
```

The token is ant's `ADMIN_TOKEN` env var (Render service `ant.finant.ai`).
Override the app URL with `ANT_BASE_URL` (default `https://ant.finant.ai`) and
the window with `OPENROUTER_COST_WINDOW_HOURS` (default `24`; ant only reports a
24h window, so it drops out for other values).

## Install

```sh
ln -sf "$PWD/openrouter-credits.10m.py" \
  "$HOME/Library/Application Support/xbar/plugins/"
```

Refresh xbar. Use the **Refresh interval** submenu to change the cadence — it
renames the symlink, leaving the committed filename alone.

## Low-balance notifications

When the balance crosses into low (≤$20) or critical (≤$5) it fires one
notification — once per crossing, not once per refresh. Topping up resets it, so
the next time you run down you get told again. Thresholds are overridable:

```sh
OPENROUTER_LOW=50 OPENROUTER_CRITICAL=10
```

## Uninstall

```sh
rm "$HOME/Library/Application Support/xbar/plugins/openrouter-credits.10m.py"
rm -f ~/.openrouter-credits-cache.json
rm -f ~/.local/state/menubar-notify/openrouter-credits.json
security delete-generic-password -s openrouter-api-key
security delete-generic-password -s ant-admin-token   # if you stored one
```
