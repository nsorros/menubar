# menubar

A small collection of macOS menu-bar widgets I actually run, packaged as
[xbar](https://xbarapp.com) plugins. Each lives in its own folder with its own
README; this top-level file just covers the shared setup.

| Plugin | What it shows | Folder |
|--------|---------------|--------|
| **Claude usage** | Your Claude subscription usage (5-hour + weekly windows) from the OAuth usage API | [`claude-usage/`](claude-usage/) |
| **Net speed** | Latest / max / avg internet speed from a local Ookla speedtest probe | [`netspeed/`](netspeed/) |
| **OpenRouter credits** | Credit balance left on your OpenRouter account, plus this key's spend | [`openrouter-credits/`](openrouter-credits/) |

## Prerequisites

- **[xbar](https://xbarapp.com)** — `brew install --cask xbar`. It runs the
  scripts in `~/Library/Application Support/xbar/plugins` and renders their
  stdout in the menu bar.
- **Python 3** — every plugin is stdlib-only (no `pip install` needed).

## Install

Each plugin is enabled by symlinking its script into the xbar plugins
directory. The filename encodes the refresh interval (`.10m.py` = every 10
minutes), so keep it intact.

```sh
git clone https://github.com/nsorros/menubar.git
cd menubar

PLUGINS="$HOME/Library/Application Support/xbar/plugins"

# Claude usage
ln -sf "$PWD/claude-usage/claude-usage-oauth.10m.py" "$PLUGINS/"

# Net speed (also sets up the background probe — see netspeed/README.md)
ln -sf "$PWD/netspeed/netspeed.1m.py" "$PLUGINS/"
./netspeed/install.sh

# OpenRouter credits (needs an API key in the keychain — see its README)
ln -sf "$PWD/openrouter-credits/openrouter-credits.10m.py" "$PLUGINS/"
```

Then open xbar (or **xbar → Refresh all**). Per-plugin details, caveats, and
uninstall steps are in each folder's README.

## Why symlinks?

The scripts stay in this repo (version-controlled) and xbar just points at
them, so editing here updates the live menu bar with no copy step.
