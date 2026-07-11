# Net speed

Latest ↓/↑ internet speed in the menu bar, with 24h / 7d / 30d max & average in
the dropdown. Three pieces:

| File | Role |
|------|------|
| `net-speed-probe` | Runs Ookla's `speedtest` CLI and appends one JSON line per run to the log. Driven by a launchd job every 5 min. |
| `netspeed.1m.py` | The xbar plugin — reads the log and renders it. Holds no data. |
| `net-speed-stats` | CLI to print max/avg/min over a window (`net-speed-stats 7d`) or per location (`net-speed-stats loc`). Also the plugin's "Stats" buttons. |
| `name-location` | Seeds the current router's MAC into `locations.json` and opens it — backs the **Name this location** menu item. |
| `locations.example.json` | Sample router-MAC → location map. Copy to the data dir as `locations.json` to name your spots. |
| `com.nick.netspeed.plist` | launchd template for the background probe. |

## Menu controls

- **Refresh** — runs a fresh probe now (a full speedtest) and redraws, rather
  than waiting for the next scheduled probe.
- **Refresh interval** — how often the menu bar re-reads the log (1m…1h). Like
  the claude-usage plugin, this is encoded in the plugin filename, so choosing a
  preset renames the symlink (`netspeed.1m.py` → `netspeed.5m.py`). It changes
  the *display* cadence only; actual measurements run on the launchd probe's own
  schedule (every 5 min) — use **Refresh** to force a new one.

## Per-location stats

Each sample records the default gateway's IP + **router MAC** (`gateway_mac`).
macOS redacts the Wi-Fi SSID from CLI tools unless the caller holds Location
Services permission — which a launchd probe doesn't — but the router's ARP entry
reads with no permission and is unique per network, so it doubles as a stable
per-location fingerprint (home vs coffee vs office).

The quickest way to name where you are: click **Name this location** in the menu
(shown with a ⚠️ while the current spot is still `unknown`). It seeds the current
router's MAC into `locations.json` as `"rename me"` and opens the file — just
change the label and save. Or edit the map by hand: MAC → label in
`~/.local/share/netspeed/locations.json` (see `locations.example.json`):

```json
{
  "60:d2:48:86:ee:e3": "home",
  "aa:bb:cc:dd:ee:ff": "coffee",
  "11:22:33:44:55:66": "office"
}
```

Unnamed routers show up as `unknown (dd:ee:ff)` (last 3 octets) — probe from a
new spot, then read the MAC off the log or the menu and add a line. The plugin
shows the current location on the "Last" row, a "By location (30d)" breakdown,
and a **Stats by location** button (`net-speed-stats loc [window]`). Samples
logged before this feature have no fingerprint and group under `unknown`.

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
