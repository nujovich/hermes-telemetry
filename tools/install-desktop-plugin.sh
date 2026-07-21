#!/usr/bin/env bash
# install-desktop-plugin.sh -- Compile and install the hermes-telemetry Desktop plugin
#
# Usage:
#   bash tools/install-desktop-plugin.sh
#
# Compiles dashboard/plugin.tsx to a single JS bundle and copies it to the
# Hermes Desktop disk door at $HERMES_HOME/desktop-plugins/hermes-telemetry/.
# Requires esbuild on PATH.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DASHBOARD_DIR="$REPO_ROOT/dashboard"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/desktop-plugins/hermes-telemetry"

echo "==> Compiling $DASHBOARD_DIR/plugin.tsx ..."
esbuild "$DASHBOARD_DIR/plugin.tsx" --bundle --outfile="$DASHBOARD_DIR/plugin.js" \
  --external:@hermes/plugin-sdk \
  --external:react \
  --external:react/jsx-runtime

echo "==> Installing to $PLUGIN_DIR ..."
mkdir -p "$PLUGIN_DIR"
cp "$DASHBOARD_DIR/plugin.js" "$PLUGIN_DIR/plugin.js"

echo "==> Done. Restart Hermes Desktop to load the plugin."
