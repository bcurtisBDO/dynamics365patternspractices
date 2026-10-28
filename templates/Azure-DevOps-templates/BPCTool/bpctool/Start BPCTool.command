#!/bin/bash
# Double-click launcher for macOS Finder.
set -euo pipefail
cd "$(dirname "$0")"

fail() {
  echo ""
  echo "$1"
  read -r -p "Press Enter to close this window..." _ || true
  exit 1
}

echo "=== BPCTool ==="
echo "Preparing environment (first launch may take a minute)..."
if ! command -v node >/dev/null 2>&1; then
  fail "Node.js was not found. Install Node.js 20.19+ or 22.12+ from nodejs.org, then double-click Start BPCTool again."
fi
if ! node scripts/ensure-env.mjs; then
  fail "Setup failed (see the messages above). Ensure Node.js 20.19+ or 22.12+ and Python 3.11+ are installed, then double-click Start BPCTool again."
fi

echo ""
echo "Starting BPCTool (UI + local runner)..."
echo "A browser window should open shortly. Leave this window open while you work."
echo "Close this window to stop the app."
echo ""

npm start || fail "BPCTool stopped with an error (see the messages above)."
