# Claude + Codex usage

Shows your Claude subscription usage in the menu bar — the rolling 5-hour
window and the weekly window — plus the latest local Codex rate-limit window, so
you can see how close you are to a limit without opening anything.

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

For Codex, the plugin reads the latest `rate_limits` event from local Codex
session files under `~/.codex/sessions`, prefers the 5-hour window when Codex
records one, and falls back to the weekly window otherwise.

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
rm -f ~/.claude-usage-oauth-cache.json ~/.claude-usage-history.json
```
