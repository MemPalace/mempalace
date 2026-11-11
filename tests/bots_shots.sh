#!/usr/bin/env bash
# Product-proof screenshots of the bots screens from tests/bots_harness.html (real bots.js + real CSS,
# scripted fake API and stream):
#   bash tests/bots_shots.sh <output-dir>
# Writes <scene>-desktop.png (1280x800) and <scene>-phone.png (390x844 at 2x) for each scene.
set -euo pipefail
out="${1:?usage: bots_shots.sh <output-dir>}"
mkdir -p "$out"
root="$(cd "$(dirname "$0")/.." && pwd)"
chrome="${CHROME:-google-chrome}"
profile="${BOTS_BROWSER_PROFILE:-$(mktemp -d)}"
port="${BOTS_HARNESS_PORT:-$((20000 + RANDOM % 20000))}"
python3 -m http.server "$port" --bind 127.0.0.1 --directory "$root" >/dev/null 2>&1 &
server=$!
trap 'kill "$server" 2>/dev/null || true' EXIT
sleep 1
shot() { # scene kind width height scale
  "$chrome" --headless=new --no-sandbox --disable-gpu --hide-scrollbars --user-data-dir="$profile" \
    --force-device-scale-factor="$5" --window-size="$3,$4" --virtual-time-budget=4000 \
    --screenshot="$out/$1-$2.png" "http://127.0.0.1:$port/tests/bots_harness.html?scene=$1" >/dev/null 2>&1
}
for scene in roster stream done sheet; do
  shot "$scene" desktop 1280 800 1
  shot "$scene" phone 390 844 2
done
ls -l "$out"
