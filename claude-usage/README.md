# Claude + Codex usage

Shows your Claude subscription usage in the menu bar — the rolling 5-hour
window and the weekly window — plus live Codex rate-limit windows, so you can see
how close you are to a limit without opening anything.

It reads your Claude Code OAuth token and calls the usage API:

- Token source: the macOS Keychain item **`Claude Code-credentials`**, falling
  back to `~/.claude/.credentials.json`.
- `GET https://api.anthropic.com/api/oauth/usage` with that bearer token and the
  `anthropic-beta: oauth-2025-04-20` header.
- Result is cached to `~/.claude-usage-oauth-cache.json` so a transient API
  hiccup still shows the last-known numbers.

No secrets live in this repo — the token is read from your machine at runtime.

## Estimated run-out

The 5-hour window's submenu carries an **est. run-out** line beside "% used"
and "resets …": each fetch
appends the current utilization to `~/.claude-usage-history.json`, and the
plugin fits a least-squares line through the samples of the *current* window to
get a burn rate in %/hour, then extrapolates to 100%.

- History is keyed on the window's `resets_at`, so it's discarded the moment the
  window rolls over — no stale window bleeding into the fit.
- Needs 3 samples spanning at least 6 minutes; until then the line reads
  "collecting data". At the default 10m refresh that's ~20 minutes after a reset.
- If the projection lands past the reset, it says so instead of naming a time.
  Flat or recovering usage reads "not on this trend".
- The 5-hour window is *rolling* — usage also ages out of it — so a straight-line
  fit is a trend, not a promise. It's most useful mid-burn, when you're pushing
  hard and want to know whether you'll hit the wall before the window clears.

## Codex

Codex usage has its own chain of four sources, tried in order until one answers.
Each window is reported in a different dialect, so they're normalised to a common
shape before display.

| # | Source | How | Freshness |
|---|--------|-----|-----------|
| 1 | Usage API | `GET https://chatgpt.com/backend-api/wham/usage`, bearer token from `~/.codex/auth.json` | live |
| 2 | App server | `codex -s read-only -a never app-server`, JSON-RPC `account/rateLimits/read` | live |
| 3 | Session logs | newest usable `rate_limits` event under `~/.codex/sessions` and `~/.codex/archived_sessions` | as old as your last Codex run |
| 4 | Cache | the last good API/app-server answer | as old as your last successful fetch |

Source 2 is worth the process spawn as a fallback because it's the CLI's own
path: it keeps working when the stored token needs refreshing, and refreshes
`auth.json` on the way through, repairing source 1 for next time. Sources 3 and 4
compete on recency rather than order — a cached reading from minutes ago beats a
log record from days ago.

The menu bar headlines the 5-hour session window when your plan has one and the
weekly window otherwise; the dropdown lists every window, plus credits, available
rate-limit resets, and which source the number came from. A replayed (non-live)
reading is marked with a `~` before the percentage and an amber line in the
dropdown spelling out how old it is — so a stale number can't quietly pass for a
current one.

To see what each source returns independently:

```sh
./claude-usage-oauth.10m.py --codex-debug
```

Set `CODEX_USAGE_SOURCE=api|rpc|logs` to force one source (useful for testing a
fallback path). `CODEX_HOME` is honoured if your Codex home isn't `~/.codex`.

### Why the Codex number used to read as a dash

The plugin originally had only source 3, and it stopped at the first session file
containing *any* `rate_limits` object. Codex emits several limit buckets, and the
non-`codex` ones (`limit_id: "premium"`) carry null windows with a credits-only
payload — so one such record in the newest session masked every older usable one
and the plugin fell through to `—`. Being log-only, it was also structurally
blind to a quota reset: nothing changed until you next ran Codex. The scanner now
keeps walking until a record actually yields a window, and the live sources above
mean a reset shows up immediately.

## Install

```sh
ln -sf "$PWD/claude-usage-oauth.10m.py" \
  "$HOME/Library/Application Support/xbar/plugins/"
```

Refresh xbar. Requires that you've signed in with Claude Code at least once (so
the credential exists). If it shows **"Auth expired"**, run `claude` once to
refresh the token, then refresh the plugin.

## Uninstall

```sh
rm "$HOME/Library/Application Support/xbar/plugins/claude-usage-oauth.10m.py"
rm -f ~/.claude-usage-oauth-cache.json ~/.claude-usage-history.json \
      ~/.codex-usage-cache.json
```
