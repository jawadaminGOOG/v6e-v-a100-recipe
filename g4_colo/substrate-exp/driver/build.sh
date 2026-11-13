#!/usr/bin/env bash
# Build colo-exp-driver inside a pinned Substrate checkout (it imports Substrate's internal client package).
#   SUBSTRATE_ROOT=/path/to/substrate@v1.0.0 OUT=/tmp/colo-exp-driver ./build.sh
# Copies *.go to $SUBSTRATE_ROOT/cmd/colo-exp-driver/ (a scratch checkout; nothing upstream is modified).
set -euo pipefail
: "${SUBSTRATE_ROOT:?set SUBSTRATE_ROOT to a substrate checkout at v1.0.0}"
OUT="${OUT:-$PWD/colo-exp-driver}"
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$SUBSTRATE_ROOT/cmd/colo-exp-driver"
cp "$here"/*.go "$SUBSTRATE_ROOT/cmd/colo-exp-driver/"
(cd "$SUBSTRATE_ROOT" && GOTOOLCHAIN=auto go build -o "$OUT" ./cmd/colo-exp-driver)
echo "built $OUT"
