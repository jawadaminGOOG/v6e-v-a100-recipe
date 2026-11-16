#!/usr/bin/env bash
# Copy the HTML deck (g4_colo/deck) into g4_colo/demo/deck so the container image (build context g4_colo/demo,
# Dockerfile `COPY . /app/`) serves it at /deck/. Run it before building the image. The copy is git-ignored.
# Excludes build inputs and generated files: *.pptx, *.py, __pycache__, dotfiles.
#   tools/sync_deck.sh            # from ../deck
#   tools/sync_deck.sh /path/deck # from another checkout
set -euo pipefail
DEMO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$(cd "${1:-$DEMO/../deck}" && pwd)"
DST="$DEMO/deck"
[ -f "$SRC/index.html" ] || { echo "sync_deck: no index.html in $SRC" >&2; exit 1; }
rm -rf "$DST"
mkdir -p "$DST"
(cd "$SRC" && tar --create --file=- --exclude='*.pptx' --exclude='*.py' --exclude='*.pyc' \
    --exclude='__pycache__' --exclude='*/.[!.]*' .) | (cd "$DST" && tar --extract --file=-)
echo "sync_deck: $(find "$DST" -type f | wc -l) files -> $DST"
