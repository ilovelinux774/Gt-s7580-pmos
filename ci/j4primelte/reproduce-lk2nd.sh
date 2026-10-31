#!/usr/bin/env bash
# Reconstruct the experimental lk2nd + M1 debug BOOT without rebuilding Linux.
# This extracts the Linux kernel/config from a PREVIOUSLY FAILED M1 image, never
# for flashing that old image. It compiles the current mainline DTB and packages
# that kernel + the unchanged #37 initramfs behind the pinned J4-specific lk2nd.
# This is a local reproduction, not an Actions build or proof of device boot.
#
# Usage: bash ci/j4primelte/reproduce-lk2nd.sh /path/to/output
# Optional: J4_LK2ND_TOOLCHAIN_PREFIX, J4_LK2ND_SOURCE, J4_LK2ND_JOBS,
# J4_REPRO_KERNEL_SOURCE (existing pinned Linux checkout), J4_REPRO_EXPECT_SHA256
# (only use the latter for the exact same compiler/dtc/zlib inputs).
set -euo pipefail
export GIT_TERMINAL_PROMPT=0 GIT_HTTP_LOW_SPEED_LIMIT=1 GIT_HTTP_LOW_SPEED_TIME=30
ROOT=$(git rev-parse --show-toplevel)
[ -z "$(git -C "$ROOT" status --porcelain)" ] || { echo 'repository has uncommitted changes; use a clean source commit' >&2; exit 1; }
OUT=${1:?give a new output directory}
mkdir -p "$OUT"; OUT=$(cd "$OUT" && pwd)
K="${J4_REPRO_KERNEL_SOURCE:-$OUT/linux-dts}"; C="$OUT/artifacts"; CACHE="$OUT/inputs"
export ROOT K C CACHE
for tool in python3 git wget cpp dtc fdtget fdtput sha256sum; do
    command -v "$tool" >/dev/null || { echo "missing $tool" >&2; exit 1; }
done
mkdir -p "$C/mainline/boot" "$C/logs" "$CACHE"
REV=15c904f9ffe013d23bbd71d3d2113b4e8ef12687
if [ ! -d "$K/.git" ]; then
    if [ -e "$K" ] && [ -n "$(ls -A "$K")" ]; then
        echo 'refusing to initialize a nonempty linux-dts directory' >&2; exit 1
    fi
    mkdir -p "$K"
    git -C "$K" init -q
    git -C "$K" remote add origin https://github.com/msm89x7-mainline/linux
    git -C "$K" sparse-checkout init --no-cone
    git -C "$K" sparse-checkout set --no-cone '/arch/arm64/boot/dts/qcom/**' '/include/dt-bindings/**' '/include/uapi/**' '/scripts/dtc/include-prefixes/**'
    git -C "$K" fetch -q --depth=1 --filter=blob:none origin "$REV"
    git -C "$K" checkout -q --detach FETCH_HEAD
fi
if [ "$(git -C "$K" rev-parse HEAD)" != "$REV" ]; then
    echo 'linux DTS tree HEAD differs from the pinned commit' >&2
    exit 1
fi
# The compiled DTB hash below authenticates the exact output even if an
# existing checkout was modified. Avoid git status on old partial clones: it
# can initiate an implicit network fetch for missing blobs and hang.
[ -s "$CACHE/boot-37.img" ] || wget -q --timeout=30 --tries=3 -O "$CACHE/boot-37.img" 'https://github.com/ilovelinux774/Gt-s7580-pmos/releases/download/j4-39/boot-37-KNOWN-GOOD-rollback.img'
[ -s "$CACHE/old-m1.img" ] || wget -q --timeout=30 --tries=3 -O "$CACHE/old-m1.img" 'https://github.com/ilovelinux774/Gt-s7580-pmos/releases/download/j4-mainline-m1-local-1/boot-m1-debug.img'
printf '%s  %s\n' '3ba9fa06e0d410dbd2e1f8e700090ab1eebc68f491a40dcfde53d805373ae80e' "$CACHE/boot-37.img" 'af7b2046cdf37d031869a9534962f1db88e34bc9eddc455505ddd017292a5a00' "$CACHE/old-m1.img" | sha256sum -c -
python3 - <<'PY'
import gzip,hashlib,importlib.util,os,pathlib,zlib
repo,out,cache=(pathlib.Path(os.environ[x]) for x in ('ROOT','C','CACHE'))
spec=importlib.util.spec_from_file_location('verify',repo/'ci/j4primelte/verify-mainline.py')
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
old=v.read_boot_independently((cache/'old-m1.img').read_bytes());assert old['valid_id']
d=zlib.decompressobj(31);raw=d.decompress(old['kernel'])+d.flush();assert d.eof and d.unused_data[:4]==bytes.fromhex('d00dfeed')
assert hashlib.sha256(raw).hexdigest()=='e78f359e9a6e32d52e184eb0c671ae35451d68fb9f27943ab10cd32ea505508a'
off=len(old['kernel'])-len(d.unused_data);(out/'mainline/m1-original-Image.gz').write_bytes(old['kernel'][:off])
a=raw.find(b'IKCFG_ST');b=raw.find(b'IKCFG_ED',a+8);assert a>=0 and b>a
config=gzip.decompress(raw[a+8:b]);assert hashlib.sha256(config).hexdigest()=='5fe238cbe76f3edea4abe4b4f1f1c7726f3e027e1171cd34308531a5ec0e4bf1'
(out/'mainline/kernel-config-m1').write_bytes(config)
print('Reused kernel and embedded config matched their pinned SHA-256 values; old BOOT is not a flash target.')
PY
cpp -nostdinc -I "$K/arch/arm64/boot/dts/qcom" -I "$K/include" -I "$K/scripts/dtc/include-prefixes" -I "$ROOT/ci/j4primelte/mainline" -undef -D__DTS__ -x assembler-with-cpp "$ROOT/ci/j4primelte/mainline/msm8917-samsung-j4primelte.dts" > "$C/m1.preprocessed.dts"
dtc -I dts -O dtb -b 0 -o "$C/mainline/m1.dtb" "$C/m1.preprocessed.dts" 2> "$C/logs/mainline-dtc.txt"
# Exact DTB used in the local J415F simulations, built with DTC 1.7.2. A
# different compiler may be semantically equivalent but is not this candidate.
printf '%s  %s\n' '58b55cc4662867802401dd9f2a6eb569d2d0d03ef15cc30dee9ad4c88762531a' "$C/mainline/m1.dtb" | sha256sum -c -
python3 "$ROOT/ci/j4primelte/mainline-wrap-arm64.py" --image "$C/mainline/m1-original-Image.gz" --out "$C/m1-wrapped-Image.gz"
python3 "$ROOT/ci/j4primelte/mainline-payload.py" --image "$C/m1-wrapped-Image.gz" --dtb "$C/mainline/m1.dtb" --out "$C/mainline/m1-Image.gz-dtb"
python3 "$ROOT/ci/j4primelte/donor-note.py" "$CACHE/boot-37.img" "$C/mainline/ramdisk.txt" --expect-sha256 3ba9fa06e0d410dbd2e1f8e700090ab1eebc68f491a40dcfde53d805373ae80e --expect-root-uuid 86e7262b-b628-4b89-8a6e-e96ef9b1aab6
python3 "$ROOT/ci/j4primelte/mainline-repack-boot.py" --base "$CACHE/boot-37.img" --kernel "$C/mainline/m1-Image.gz-dtb" --out "$C/mainline/boot/boot-m1-debug.img" --debug-shell --report "$C/logs/repack-debug.txt"
# This normal-mode image is an internal verifier input, not the release target.
python3 "$ROOT/ci/j4primelte/mainline-repack-boot.py" --base "$CACHE/boot-37.img" --kernel "$C/mainline/m1-Image.gz-dtb" --out "$C/mainline/boot/boot-m1-rootfs.img" --report "$C/logs/repack-rootfs.txt"
python3 "$ROOT/ci/j4primelte/verify-mainline.py" "$C/mainline" "$CACHE/boot-37.img" | tee "$C/logs/mainline-verify.txt"
J4_MAINLINE_BASE_BOOT="$CACHE/boot-37.img" J4_MAINLINE_ARTIFACT_DIR="$C" bash "$ROOT/ci/j4primelte/build-lk2nd.sh"
image="$C/mainline/boot/lk2nd-m1-debug.img"
if [ -n "${J4_REPRO_EXPECT_SHA256:-}" ]; then
    printf '%s  %s\n' "$J4_REPRO_EXPECT_SHA256" "$image" | sha256sum -c -
else
    sha256sum "$image"
fi
echo 'Reconstruction completed: BOOT-only, experimental; no phone test was performed.'
