#!/usr/bin/env bash
# Set up the net-speed background probe: create the data dir, install the
# launchd job pointed at this repo's net-speed-probe, and load it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="$HERE/net-speed-probe"
DATA_DIR="${NETSPEED_DATA_DIR:-$HOME/.local/share/netspeed}"
PLIST_DST="$HOME/Library/LaunchAgents/com.nick.netspeed.plist"
LABEL="com.nick.netspeed"

mkdir -p "$DATA_DIR" "$HOME/Library/LaunchAgents"
chmod +x "$PROBE" "$HERE/net-speed-stats" "$HERE/netspeed.1m.py"

# Render the plist template with the absolute probe path.
sed "s|__PROBE_PATH__|$PROBE|" "$HERE/com.nick.netspeed.plist" > "$PLIST_DST"

# Reload (bootout is a no-op if it isn't loaded yet).
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"

echo "Installed $LABEL — probing every 5 min into $DATA_DIR/log.jsonl"
