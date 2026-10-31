#!/usr/bin/env bash
# J415F-only, hardware-untested HLOS-SMC and stage-marker experiment.
# Run AFTER build-mainline.sh, verify-mainline.py, and build-lk2nd.sh.
# This compiles no new Linux kernel and emits one combined debug BOOT only.
# It never writes the phone, primary aboot, recovery, GPT, or rootfs image.
# pmsg RAM is intentionally overwritten; recovery layout/retention are unknown.
set -euo pipefail

if [ "${1:-}" = '-n' ] || [ "${1:-}" = '--dry-run' ]; then
    here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    bash -n "${BASH_SOURCE[0]}"
    for helper in "$here"/lk2nd/diag/*.py; do
        python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$helper"
    done
    echo 'HLOS diagnostic build pipeline parses OK'
    exit 0
fi

ROOT=$(git rev-parse --show-toplevel)
HERE="$ROOT/ci/j4primelte"
DIAG="$HERE/lk2nd/diag"
ARTIFACTS="${J4_MAINLINE_ARTIFACT_DIR:-$ROOT/artifacts}"
MAIN="$ARTIFACTS/mainline"
LOGS="$ARTIFACTS/logs"
DONOR="${J4_MAINLINE_BASE_BOOT:?set J4_MAINLINE_BASE_BOOT to the pinned #37 donor image}"
SRC="${J4_LK2ND_SOURCE:-$HOME/j4-lk2nd-geckyn}"
PREFIX="${J4_LK2ND_TOOLCHAIN_PREFIX:-arm-none-eabi-}"
JOBS="${J4_LK2ND_JOBS:-$(nproc)}"
REV='7bc82f56d1339b1d081c48ad3c664ec6119257af'
OUT="$MAIN/boot/lk2nd-hlos-diagnostic.img"
SAVED="$MAIN/lk2nd-hlos-diagnostic"
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo 'J4_LK2ND_JOBS must be positive' >&2; exit 1; }
for tool in git make dtc fdtget fdtput python3 "${PREFIX}gcc" "${PREFIX}ld" "${PREFIX}objcopy" aarch64-linux-gnu-as aarch64-linux-gnu-ld aarch64-linux-gnu-objcopy; do
    command -v "$tool" >/dev/null || { echo "missing $tool" >&2; exit 1; }
done
python3 -c 'import unicorn; assert unicorn.__version__ == "2.1.4"' || {
    echo 'diagnostic ARM/ARM64 tests require unicorn 2.1.4' >&2; exit 1;
}
for input in "$DONOR" "$MAIN/m1-original-Image.gz" "$MAIN/m1.dtb" "$MAIN/boot/boot-m1-debug.img" "$MAIN/lk2nd/j4-outer.dtb"; do
    [ -s "$input" ] || { echo "missing previously verified input: $input" >&2; exit 1; }
done
[ "$(git -C "$SRC" rev-parse HEAD)" = "$REV" ] || { echo 'lk2nd checkout not at pinned revision' >&2; exit 1; }
[ -z "$(git -C "$SRC" status --porcelain)" ] || { echo 'lk2nd checkout is not clean' >&2; exit 1; }
mkdir -p "$LOGS" "$SAVED" "$MAIN/boot"
# Invalidate old output first; no failed build should leave a flashable file.
rm -f "$OUT" "$LOGS/lk2nd-hlos-verify.txt"
TEMP=$(mktemp -d "${TMPDIR:-/tmp}/j4-hlos.XXXXXXXX")
WORK="$TEMP/lk2nd"
cleanup() {
    git -C "$SRC" worktree remove --force "$WORK" >/dev/null 2>&1 || true
    rm -rf "$TEMP"
}
trap cleanup EXIT

# Revalidate the unchanged ordinary mainline inputs before deriving the special
# tree/wrapper. Its verifier intentionally rejects the experimental differences.
panel=(); [ -f "$MAIN/panel.dtb" ] && panel=(panel)
python3 "$HERE/verify-mainline.py" "$MAIN" "$DONOR" "${panel[@]}" > "$LOGS/lk2nd-hlos-baseline-verify.txt"
git -C "$SRC" worktree add --detach "$WORK" "$REV" > "$LOGS/lk2nd-hlos-worktree.txt" 2>&1
git -C "$WORK" apply --check "$DIAG/0001-j4-hlos-scm-pmsg.patch"
git -C "$WORK" apply "$DIAG/0001-j4-hlos-scm-pmsg.patch"
make -C "$WORK" -j"$JOBS" TOOLCHAIN_PREFIX="$PREFIX" LKLE_CFLAGS='-fcommon' msm8917-secondary > "$LOGS/lk2nd-hlos-build.txt" 2>&1
BUILT="$WORK/build-msm8917-secondary"
for def in '#define SAFE_MODE 1' '#define DEVICE_TREE 1' '#define ABOOT_IGNORE_BOOT_HEADER_ADDRS 1'; do
    grep -Fxq "$def" "$BUILT/config.h" || { echo "missing safety/config: $def" >&2; exit 1; }
done
cp "$BUILT/lk.bin" "$SAVED/lk.bin"
cp "$BUILT/lk" "$SAVED/lk.elf"
cp "$MAIN/lk2nd/j4-outer.dtb" "$SAVED/j4-outer.dtb"

python3 "$DIAG/test-scm-hlos.py" "$SAVED/lk.elf" "$SAVED/lk.bin" > "$LOGS/lk2nd-hlos-scm-emulation.txt"
python3 "$DIAG/diag-dtb.py" --base "$MAIN/m1.dtb" --out "$SAVED/m1-diagnostic.dtb" > "$LOGS/lk2nd-hlos-dtb.txt"
python3 "$DIAG/test-diag-dtb.py" --base "$MAIN/m1.dtb" >> "$LOGS/lk2nd-hlos-dtb.txt"
python3 "$DIAG/diag-wrap.py" --image "$MAIN/m1-original-Image.gz" --source "$DIAG/entry-pmsg.S" --out "$TEMP/wrapped.gz"
python3 "$DIAG/test-entry-pmsg.py" --wrapped "$TEMP/wrapped.gz" --original "$MAIN/m1-original-Image.gz" > "$LOGS/lk2nd-hlos-entry-emulation.txt"
python3 "$HERE/mainline-payload.py" --image "$TEMP/wrapped.gz" --dtb "$SAVED/m1-diagnostic.dtb" --out "$TEMP/payload"
python3 "$HERE/mainline-repack-boot.py" --base "$DONOR" --kernel "$TEMP/payload" --out "$TEMP/inner.img" --debug-shell > "$LOGS/lk2nd-hlos-repack.txt"
python3 "$HERE/lk2nd-image.py" --donor "$DONOR" --lk-binary "$SAVED/lk.bin" --lk-dtb "$SAVED/j4-outer.dtb" --inner "$TEMP/inner.img" --out "$TEMP/combined.img" > "$LOGS/lk2nd-hlos-pack.txt"
python3 "$DIAG/verify-diag.py" --donor "$DONOR" --normal-inner "$MAIN/boot/boot-m1-debug.img" --image "$TEMP/combined.img" --lk "$SAVED/lk.bin" --outer-dtb "$SAVED/j4-outer.dtb" --base-dtb "$MAIN/m1.dtb" --original "$MAIN/m1-original-Image.gz" | tee "$LOGS/lk2nd-hlos-verify.txt"
cp "$TEMP/combined.img" "$OUT"
cmp -s "$TEMP/combined.img" "$OUT" || { rm -f "$OUT"; echo 'output copy mismatch' >&2; exit 1; }
{
    echo 'EXPERIMENTAL J415F HLOS SCM + pmsg diagnostic: hardware boot unproven'
    echo 'No rootfs image. Debug shell is intended to stop before normal rootfs mount; early hooks unverified.'
    echo 'Marker overwrites prior volatile pmsg data. Recovery compatibility and retention are unverified.'
    echo "j4_repository_commit=$(git -C "$ROOT" rev-parse HEAD)"
    echo "j4_repository_status=$(if [ -z "$(git -C "$ROOT" status --porcelain)" ]; then echo clean; else echo dirty; fi)"
    echo "lk2nd_repository_commit=$REV"
    echo 'project=msm8917-secondary; split_offset=1048576; LKLE_CFLAGS=-fcommon'
    "${PREFIX}gcc" --version | head -1
    "${PREFIX}ld" --version | head -1
    aarch64-linux-gnu-as --version | head -1
    dtc --version
    python3 -c 'import sys,unicorn,zlib; print("Python="+sys.version.split()[0]); print("unicorn="+unicorn.__version__); print("zlib="+zlib.ZLIB_RUNTIME_VERSION)'
    sha256sum "$DIAG/0001-j4-hlos-scm-pmsg.patch" "$DIAG/entry-pmsg.S" "$SAVED/lk.bin" "$SAVED/lk.elf" "$SAVED/m1-diagnostic.dtb" "$MAIN/m1-original-Image.gz" "$OUT"
} > "$SAVED/SOURCE.txt"
echo "Experimental combined BOOT diagnostic built: $OUT"
