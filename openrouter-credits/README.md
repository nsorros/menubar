# OpenRouter credits

Shows what's left of your OpenRouter balance in the menu bar, so a job doesn't
fail at 2am because the account quietly ran dry.

The title is the remaining balance with a traffic-light dot — green above $20,
amber at or below it, red at or below $5. The dropdown breaks it down:

- **Remaining** — `total_credits - total_usage` for the whole account.
- **This key** — the spend attributed to the key the plugin authenticates with,
  split into today / this week / this month, plus its lifetime total. If the key
  has a spend limit set, its remaining headroom shows too.

Two endpoints back it:

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
```
