#!/usr/bin/env bash
# Fast, Actions-compatible reconstruction of the J415F HLOS diagnostic.
# The previous device-test-FAILED combined BOOT is downloaded ONLY to extract
# SHA-pinned inputs. The kernel is NOT rebuilt in this workflow. The ARM32 LK
# is rebuilt at a pinned source revision with the experimental patch applied.
# Outputs are BOOT-only and do not modify the user's rootfs, recovery or GPT.
set -euo pipefail

if [ "${1:-}" = '-n' ] || [ "${1:-}" = '--dry-run' ]; then
    bash -n "${BASH_SOURCE[0]}"
    echo 'HLOS diagnostic reconstruction parses OK'
    exit 0
fi
export GIT_TERMINAL_PROMPT=0 GIT_HTTP_LOW_SPEED_LIMIT=1 GIT_HTTP_LOW_SPEED_TIME=30
ROOT=$(git rev-parse --show-toplevel)
HERE="$ROOT/ci/j4primelte"
OUT=${1:?give a dedicated artifacts directory}
mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
mkdir -p "$OUT/inputs" "$OUT/logs" "$OUT/mainline/boot"
DONOR="${J4_MAINLINE_BASE_BOOT:-$OUT/inputs/boot-37.img}"
FAILED="$OUT/inputs/failed-lk2nd-input.img"
SRC="${J4_LK2ND_SOURCE:-$HOME/j4-lk2nd-geckyn}"
REV='7bc82f56d1339b1d081c48ad3c664ec6119257af'
# Refuse to overwrite real files: this script creates and later removes the
# internal baseline debug/rootfs-mode BOOT images after successful validation.
for f in "$OUT/mainline/boot/boot-m1-debug.img" "$OUT/mainline/boot/boot-m1-rootfs.img" "$OUT/mainline/boot/lk2nd-hlos-diagnostic.img"; do
    [ ! -e "$f" ] || { echo "refusing to overwrite existing $f" >&2; exit 1; }
done
for tool in git wget python3 sha256sum; do command -v "$tool" >/dev/null || { echo "missing $tool" >&2; exit 1; }; done
if [ ! -s "$DONOR" ]; then
    mkdir -p "$(dirname "$DONOR")"
    wget -q --timeout=30 --tries=3 -O "$DONOR.part" 'https://github.com/ilovelinux774/Gt-s7580-pmos/releases/download/j4-39/boot-37-KNOWN-GOOD-rollback.img'
    mv "$DONOR.part" "$DONOR"
fi
if [ ! -s "$FAILED" ]; then
    wget -q --timeout=30 --tries=3 -O "$FAILED.part" 'https://github.com/ilovelinux774/Gt-s7580-pmos/releases/download/j4-mainline-m1-lk2nd-1/lk2nd-m1-debug.img'
    mv "$FAILED.part" "$FAILED"
fi
printf '%s  %s\n' '3ba9fa06e0d410dbd2e1f8e700090ab1eebc68f491a40dcfde53d805373ae80e' "$DONOR" '3d9edd580d500598c83fe4143a6efba83a7ce6f5d1116305e82e216fd1243513' "$FAILED" | sha256sum -c -
python3 "$HERE/lk2nd/diag/prepare-inputs.py" --failed-image "$FAILED" --donor "$DONOR" --out "$OUT" | tee "$OUT/logs/lk2nd-hlos-inputs.txt"
# Internal verifier fixture only; never upload or flash this rootfs-mode BOOT.
python3 "$HERE/mainline-repack-boot.py" --base "$DONOR" --kernel "$OUT/mainline/m1-Image.gz-dtb" --out "$OUT/mainline/boot/boot-m1-rootfs.img" > "$OUT/logs/lk2nd-hlos-baseline-repack.txt"
if ! git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
    if [ -e "$SRC" ] && [ -n "$(ls -A "$SRC")" ]; then
        echo "refusing to initialize nonempty untracked source directory $SRC" >&2; exit 1
    fi
    mkdir -p "$SRC"
    git -C "$SRC" init -q
    git -C "$SRC" remote add origin https://github.com/geckyn/lk2nd.git
    git -C "$SRC" fetch -q --depth=1 origin "$REV"
    git -C "$SRC" checkout -q --detach FETCH_HEAD
fi
J4_LK2ND_SOURCE="$SRC" J4_MAINLINE_ARTIFACT_DIR="$OUT" J4_MAINLINE_BASE_BOOT="$DONOR" bash "$HERE/build-lk2nd-hlos-diag.sh"
# Only the NEW diagnostic BOOT is left in the user-facing boot-artifact dir.
rm -f "$OUT/mainline/boot/boot-m1-debug.img" "$OUT/mainline/boot/boot-m1-rootfs.img"
echo 'One experimental BOOT-only HLOS diagnostic staged; no hardware test has been made.'
echo 'The mainline kernel was reused from a previously device-test-FAILED build, not rebuilt in this run.'
