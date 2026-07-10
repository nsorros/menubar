# Claude usage

Shows your Claude subscription usage in the menu bar — the rolling 5-hour
window and the weekly window — so you can see how close you are to a limit
without opening anything.

It reads your Claude Code OAuth token and calls the usage API:

- Token source: the macOS Keychain item **`Claude Code-credentials`**, falling
  back to `~/.claude/.credentials.json`.
- `GET https://api.anthropic.com/api/oauth/usage` with that bearer token and the
  `anthropic-beta: oauth-2025-04-20` header.
- Result is cached to `~/.claude-usage-oauth-cache.json` so a transient API
  hiccup still shows the last-known numbers.

No secrets live in this repo — the token is read from your machine at runtime.

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
rm -f ~/.claude-usage-oauth-cache.json
```
