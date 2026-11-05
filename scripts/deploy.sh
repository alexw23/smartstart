#!/usr/bin/env bash
# Sync this checkout's integration into Home Assistant's config dir, copying only
# files whose content changed and removing files that no longer exist here.
# Home Assistant caches Python modules, so Python changes need an HA restart;
# frontend-only changes just need a browser refresh (the panel JS URL is cache-
# busted by its mtime).
#
#   scripts/deploy.sh            # sync
#   scripts/deploy.sh --dry-run  # list what would change
#
# HA_CUSTOM_COMPONENTS overrides the destination (default: the Samba mount).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)/custom_components/smartstart"
DEST="${HA_CUSTOM_COMPONENTS:-/mnt/ha/config/custom_components}/smartstart"
DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1

# Paths never synced either way (docs live at the repo root; caches are local).
skip() { case "$1" in */__pycache__/*|*/.pytest_cache/*|*/README.md|*/TODO.md) return 0;; esac; return 1; }

changed=0
act() { echo "$1"; changed=$((changed + 1)); }

while IFS= read -r -d '' f; do
  rel="${f#"$SRC"/}"; skip "/$rel" && continue
  if [[ ! -e "$DEST/$rel" ]] || ! cmp -s "$f" "$DEST/$rel"; then
    act "copy   $rel"
    (( DRY )) || { mkdir -p "$(dirname "$DEST/$rel")"; cp -p "$f" "$DEST/$rel"; }
  fi
done < <(find "$SRC" -type f -print0)

if [[ -d "$DEST" ]]; then
  while IFS= read -r -d '' f; do
    rel="${f#"$DEST"/}"; skip "/$rel" && continue
    if [[ ! -e "$SRC/$rel" ]]; then
      act "delete $rel"
      (( DRY )) || rm -f "$DEST/$rel"
    fi
  done < <(find "$DEST" -type f -print0)
fi

(( changed )) || echo "up to date"
