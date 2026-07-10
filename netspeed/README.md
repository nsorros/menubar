# Net speed

Latest ↓/↑ internet speed in the menu bar, with 24h / 7d / 30d max & average in
the dropdown. Three pieces:

| File | Role |
|------|------|
| `net-speed-probe` | Runs Ookla's `speedtest` CLI and appends one JSON line per run to the log. Driven by a launchd job every 5 min. |
| `netspeed.1m.py` | The xbar plugin — reads the log and renders it. Holds no data. |
| `net-speed-stats` | CLI to print max/avg/min over a window (`net-speed-stats 7d`). Also the plugin's "Stats" button. |
| `com.nick.netspeed.plist` | launchd template for the background probe. |

## Data location

Samples are written to `~/.local/share/netspeed/log.jsonl` (one JSON object per
line). Override with the `NETSPEED_DATA_DIR` environment variable — all three
scripts honor it.

## Prerequisites

- Ookla speedtest CLI: `brew install speedtest` (the official Ookla one, not
  `speedtest-cli`). The probe expects it at `/opt/homebrew/bin/speedtest`.

## Install

```sh
# 1. enable the menu-bar plugin
ln -sf "$PWD/netspeed.1m.py" \
  "$HOME/Library/Application Support/xbar/plugins/"

# 2. set up the background probe (launchd job, every 5 min)
./install.sh
```

`install.sh` creates the data dir, writes the launchd plist with the correct
absolute path to `net-speed-probe`, and loads it. Run the probe once by hand to
seed a first sample:

```sh
./net-speed-probe && net-speed-stats
```

## Uninstall

```sh
launchctl bootout gui/$(id -u)/com.nick.netspeed 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.nick.netspeed.plist"
rm "$HOME/Library/Application Support/xbar/plugins/netspeed.1m.py"
# data is left in ~/.local/share/netspeed — remove it yourself if you want
```
