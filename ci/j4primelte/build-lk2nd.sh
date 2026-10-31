#!/usr/bin/env bash
# Build Geckyn's J4+-specific lk2nd and package it with the M1 debug image.
# No rootfs image, recovery image, primary aboot, or standalone lk2nd image is
# published by this script. BOOT is a 32-MiB partition on the recorded J415F.
#
# Inputs (a normal successful build-mainline.sh has already produced the M1):
#   J4_MAINLINE_BASE_BOOT       the pinned #37 donor Android boot image
#   J4_MAINLINE_ARTIFACT_DIR    defaults to repository/artifacts
#   J4_LK2ND_SOURCE             optional clean checkout at the pinned revision
#   J4_LK2ND_TOOLCHAIN_PREFIX   defaults to arm-none-eabi-
#   J4_LK2ND_JOBS               defaults to nproc
# Outputs:
#   artifacts/mainline/boot/lk2nd-m1-debug.img (combined, experimental)
#   artifacts/mainline/lk2nd/{lk.bin,lk.elf,j4-outer.dtb,SOURCE.txt}
#   artifacts/logs/lk2nd-{build,verify,tests}.txt
set -euo pipefail

if [ "${1:-}" = '-n' ] || [ "${1:-}" = '--dry-run' ]; then
    here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    bash -n "${BASH_SOURCE[0]}"
    for f in lk2nd-image.py test-lk2nd.py; do
        python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$here/$f"
    done
    echo 'lk2nd build/pack pipeline parses OK'
    exit 0
fi

ROOT=$(git rev-parse --show-toplevel)
here="$ROOT/ci/j4primelte"
ARTIFACTS="${J4_MAINLINE_ARTIFACT_DIR:-$ROOT/artifacts}"
MAIN="$ARTIFACTS/mainline"
LOGS="$ARTIFACTS/logs"
LK_OUT="$MAIN/lk2nd"
DONOR="${J4_MAINLINE_BASE_BOOT:?set J4_MAINLINE_BASE_BOOT to the pinned #37 donor image}"
INNER="$MAIN/boot/boot-m1-debug.img"
# The standalone M1 checker scans boot-*.img. This different name makes the
# combined image use its own two-layer verifier, including on local re-runs.
OUT="$MAIN/boot/lk2nd-m1-debug.img"
SRC="${J4_LK2ND_SOURCE:-$HOME/j4-lk2nd-geckyn}"
REMOTE='https://github.com/geckyn/lk2nd.git'
REV='7bc82f56d1339b1d081c48ad3c664ec6119257af'
PREFIX="${J4_LK2ND_TOOLCHAIN_PREFIX:-arm-none-eabi-}"
JOBS="${J4_LK2ND_JOBS:-$(nproc)}"
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo 'J4_LK2ND_JOBS must be positive' >&2; exit 1; }
for tool in git make dtc fdtget python3 "${PREFIX}gcc" "${PREFIX}ld" "${PREFIX}objcopy"; do
    command -v "$tool" >/dev/null || { echo "missing $tool" >&2; exit 1; }
done
if [ ! -s "$DONOR" ] || [ ! -s "$INNER" ]; then
    echo 'need the #37 donor and verified M1 debug image first' >&2
    exit 1
fi
mkdir -p "$LOGS" "$LK_OUT" "$MAIN/boot"
# A failed rebuild must not leave a stale flashable candidate behind.
rm -f "$OUT" "$LOGS/lk2nd-verify.txt"

if [ ! -d "$SRC/.git" ]; then
    if [ -e "$SRC" ] && [ -n "$(ls -A "$SRC")" ]; then
        echo "refusing to initialize nonempty untracked source directory $SRC" >&2; exit 1
    fi
    mkdir -p "$SRC"
    git -C "$SRC" init -q
    git -C "$SRC" remote add origin "$REMOTE"
    git -C "$SRC" fetch -q --depth=1 origin "$REV"
    git -C "$SRC" checkout -q --detach FETCH_HEAD
fi
[ "$(git -C "$SRC" rev-parse HEAD)" = "$REV" ] || { echo 'lk2nd checkout not at pinned revision' >&2; exit 1; }
[ -z "$(git -C "$SRC" status --porcelain)" ] || { echo 'lk2nd checkout is not clean' >&2; exit 1; }
grep -Fq 'unsigned long long lk_size = (1 * 1024 * 1024) / block_size;' "$SRC/platform/msm_shared/partition_parser.c" || {
    echo 'lk2nd source does not have the expected 1-MiB split' >&2; exit 1;
}

# GCC 10 (Ubuntu 22.04 Actions) defaults to -fno-common, but this old LK tree
# declares several shared globals in headers; it was originally built with a
# compiler whose default was -fcommon. Do not alter the original source. Drop
# this project's generated build directory so a prior compiler cannot leak
# stale object files into the reported build.
rm -rf "$SRC/build-msm8917-secondary"
make -C "$SRC" -j"$JOBS" TOOLCHAIN_PREFIX="$PREFIX" LKLE_CFLAGS='-fcommon' msm8917-secondary 2>&1 | tee "$LOGS/lk2nd-build.txt"
BUILT="$SRC/build-msm8917-secondary"
for def in '#define SAFE_MODE 1' '#define DEVICE_TREE 1' '#define ABOOT_IGNORE_BOOT_HEADER_ADDRS 1'; do
    grep -Fxq "$def" "$BUILT/config.h" || { echo "missing lk2nd build safety/config: $def" >&2; exit 1; }
done
if [ ! -s "$BUILT/lk.bin" ] || [ ! -s "$BUILT/lk" ]; then
    echo 'missing built lk2nd payload' >&2
    exit 1
fi
cp "$BUILT/lk.bin" "$LK_OUT/lk.bin"
cp "$BUILT/lk" "$LK_OUT/lk.elf"
dtc -I dts -O dtb -i "$SRC/dts" -o "$LK_OUT/j4-outer.dtb" \
    "$here/lk2nd/msm8917-j4primelte.dts" 2> "$LOGS/lk2nd-dtc.txt"
J4_LK2ND_DTB="$LK_OUT/j4-outer.dtb" python3 "$here/test-lk2nd.py" > "$LOGS/lk2nd-tests.txt" 2>&1
python3 "$here/lk2nd-image.py" --donor "$DONOR" --lk-binary "$LK_OUT/lk.bin" \
    --lk-dtb "$LK_OUT/j4-outer.dtb" --inner "$INNER" --out "$OUT" \
    --report "$LOGS/lk2nd-verify.txt"
{
    echo "lk2nd_repository=$REMOTE"
    echo "lk2nd_commit=$REV"
    echo 'project=msm8917-secondary'
    echo 'split_offset=1048576'
    echo 'LKLE_CFLAGS=-fcommon'
    "${PREFIX}gcc" --version | head -1
    "${PREFIX}ld" --version | head -1
    dtc --version
    python3 -c 'import sys,zlib; print("Python=" + sys.version.split()[0]); print("zlib=" + zlib.ZLIB_RUNTIME_VERSION)'
    sha256sum "$LK_OUT/lk.bin" "$LK_OUT/lk.elf" "$LK_OUT/j4-outer.dtb" "$INNER" "$OUT"
    echo 'Static checks only; Samsung loader, lk2nd, Linux, USB and retention remain hardware-untested.'
} > "$LK_OUT/SOURCE.txt"
echo "Experimental combined BOOT image built: $OUT"
