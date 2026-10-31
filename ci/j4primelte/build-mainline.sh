#!/usr/bin/env bash
# Build the arm64 mainline kernel for the J4+ and pack it into BOOT images.
#
# This is the mainline twin of ci/j4primelte/build-kernel.sh, and it deliberately
# produces no rootfs: the phone keeps the postmarketOS userspace it already has,
# and the boot image reuses that job's initramfs as a donor. Packing preserves
# the donor ramdisk, but device boot and normal rootfs use remain unproven.
#
#   J4_MAINLINE_BASE_BOOT   SHA-256-pinned, known-good #37 donor boot.img
#   J4_MAINLINE_PANEL=1     also build the M2 panel variant
#   J4_MAINLINE_SKIP_APT=1  dependencies are already installed (CI does this)
#   J4_MAINLINE_KERNEL_TREE where the pinned tree lives (~/j4-mainline-kernel)
#   J4_MAINLINE_OUT_PREFIX  optional prefix for kernel build directories
#   J4_MAINLINE_ARTIFACT_DIR optional artifact directory (defaults to repo/artifacts)
#   J4_MAINLINE_JOBS        optional parallel build jobs (defaults to nproc)
#   J4_MAINLINE_DTBS_ONLY=1 reuse an existing output dir: device tree + packing
#                           only, for re-running the boot images after a bootarg
#                           or device tree edit
#
# Usage: bash ci/j4primelte/build-mainline.sh [-n]
set -euo pipefail

# -n is the CI pre-compile pass: parse the script and the helpers and stop, so a
# syntax slip never costs a kernel build minutes.
if [ "${1:-}" = "-n" ] || [ "${1:-}" = "--dry-run" ]; then
    here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    bash -n "${BASH_SOURCE[0]}"
    for helper in mainline-config.py mainline-kernel.py mainline-payload.py \
                  mainline-repack-boot.py verify-mainline.py donor-note.py test-mainline.py; do
        python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$here/$helper"
    done
    echo "mainline build/pack pipeline parses OK"
    exit 0
fi

ROOT=$(git rev-parse --show-toplevel)
here="$ROOT/ci/j4primelte"
# shellcheck source=ci/j4primelte/mainline-sources.env
source "$here/mainline-sources.env"

KERNEL="${J4_MAINLINE_KERNEL_TREE:-$HOME/j4-mainline-kernel}"
ARTIFACTS="${J4_MAINLINE_ARTIFACT_DIR:-$ROOT/artifacts}"
LOGS="$ARTIFACTS/logs"
MAIN="$ARTIFACTS/mainline"
OUT="$MAIN/boot"
mkdir -p "$LOGS" "$MAIN" "$OUT"
cp "$here/mainline-sources.env" "$LOGS/mainline-sources.env"

PANEL=0
if [ "${J4_MAINLINE_PANEL:-0}" = "1" ]; then
    PANEL=1
fi
DTBS_ONLY="${J4_MAINLINE_DTBS_ONLY:-0}"

apt_install() {
    sudo apt-get update -qq
    sudo apt-get install -y --no-install-recommends \
        build-essential bc bison flex libelf-dev libssl-dev cpio rsync xz-utils \
        device-tree-compiler kmod python3-yaml gcc-aarch64-linux-gnu \
        binutils-aarch64-linux-gnu
}
if [ "${J4_MAINLINE_SKIP_APT:-0}" != "1" ]; then
    apt_install
fi

# 1. The pinned tree, at exactly one commit. Not `git clone -b`: the branch moves
#    under us, and "it built on the runner" has to mean one specific tree.
if [ ! -f "$KERNEL/Makefile" ]; then
    echo "cloning $J4_MAINLINE_REPO at $J4_MAINLINE_SHA into $KERNEL"
    mkdir -p "$KERNEL"
    git -C "$KERNEL" init -q
    git -C "$KERNEL" remote add origin "$J4_MAINLINE_REPO"
    git -C "$KERNEL" fetch -q --depth=1 origin "$J4_MAINLINE_SHA"
    git -C "$KERNEL" checkout -q --force --detach FETCH_HEAD
elif [ -d "$KERNEL/.git" ]; then
    have=$(git -C "$KERNEL" rev-parse HEAD)
    if [ "$have" != "$J4_MAINLINE_SHA" ]; then
        echo "$KERNEL is at $have, not the pinned $J4_MAINLINE_SHA;" >&2
        echo "delete it or point J4_MAINLINE_KERNEL_TREE at another directory" >&2
        exit 1
    fi
    echo "kernel tree already at $J4_MAINLINE_SHA"
else
    # A tarball or an exported tree: usable, but nothing can be verified against
    # a commit, so say so instead of pretending the pin holds.
    echo "note: $KERNEL has no .git; trusting its contents as the pinned tree" >&2
fi

# The panel driver is staged for M1 as well. It costs nothing (its Kconfig entry
# is simply not selected) and it means a broken stanza or a bad #include fails
# every build rather than only the M2 one, which is the only way to find out
# before someone waits twenty minutes on a panel run.
python3 "$here/mainline-kernel.py" "$KERNEL" --panel

# Only the files this pipeline staged may differ from the pin. Anything else means
# somebody patched the tree by hand, and then the artifact no longer describes
# "pinned commit + this repository", which is the whole basis of the build.
if [ -d "$KERNEL/.git" ]; then
    mapfile -t allowed < <(python3 "$here/mainline-kernel.py" "$KERNEL" --list-staged)
    unexpected=()
    while read -r status path; do
        [ -z "${path:-}" ] && continue
        ok=0
        for entry in "${allowed[@]}"; do
            [ "$path" = "$entry" ] && ok=1 && break
        done
        [ "$ok" = 1 ] || unexpected+=("$status $path")
    done < <(git -C "$KERNEL" status --porcelain)
    if [ "${#unexpected[@]}" -gt 0 ]; then
        echo 'the mainline tree carries changes this pipeline did not stage:' >&2
        printf '  %s\n' "${unexpected[@]}" >&2
        exit 1
    fi
fi

CROSS=aarch64-linux-gnu-
if ! command -v "${CROSS}gcc" >/dev/null 2>&1; then
    if [ "$(uname -m)" = "aarch64" ]; then
        CROSS=
    else
        echo "no ${CROSS}gcc and not on arm64: cannot build this kernel" >&2
        exit 1
    fi
fi
# Kconfig probes compiler capabilities even during defconfig/olddefconfig.
# Export the SAME cross compiler used by make; otherwise an x86 host gcc can
# produce a .config different from the one later used for the arm64 Image.
export CROSS_COMPILE="$CROSS"
"${CROSS}gcc" --version | head -1 | tee "$LOGS/toolchain.txt"
dtc --version | tee -a "$LOGS/toolchain.txt"

build_variant() {
    local name=$1
    local dtb_name=$2
    local out="${J4_MAINLINE_OUT_PREFIX:-$HOME/j4-mainline-out-}$name"
    if [ "$DTBS_ONLY" != 1 ]; then
        rm -rf "$out"
    fi
    mkdir -p "$out"
    local config_args=()
    [ "$name" = panel ] && config_args=(--panel)
    # DTBS_ONLY means "reuse the output directory exactly as the full run
    # configured it". Re-applying the fragment here would publish a
    # kernel-config-<name> (and a check against it) describing a .config whose
    # Image.gz is not the one being packed.
    if [ "$DTBS_ONLY" != 1 ]; then
        python3 "$here/mainline-config.py" apply \
            --tree "$KERNEL" --out "$out" "${config_args[@]}" \
            > "$LOGS/config-$name.txt" 2>&1
    else
        test -s "$MAIN/kernel-config-$name" && cmp -s "$out/.config" "$MAIN/kernel-config-$name" || {
            echo "kernel config missing or changed: run without J4_MAINLINE_DTBS_ONLY" >&2
            exit 1
        }
    fi
    # Build the two J4+ DTBs, not thousands of unrelated arm64 board DTBs.
    local targets=(Image.gz qcom/msm8917-samsung-j4primelte.dtb
                   qcom/msm8917-samsung-j4primelte-panel.dtb)
    [ "$DTBS_ONLY" = 1 ] && targets=(qcom/msm8917-samsung-j4primelte.dtb
                                      qcom/msm8917-samsung-j4primelte-panel.dtb)
    local jobs="${J4_MAINLINE_JOBS:-$(nproc)}"
    [[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { echo 'J4_MAINLINE_JOBS must be a positive integer' >&2; exit 1; }
    make -C "$KERNEL" O="$out" ARCH=arm64 CROSS_COMPILE="$CROSS" \
        -j"$jobs" "${targets[@]}" > "$LOGS/kernel-$name.log" 2>&1
    if [ "$DTBS_ONLY" = 1 ]; then
        cmp -s "$out/.config" "$MAIN/kernel-config-$name" || {
            echo 'dtbs-only changed .config; existing Image.gz is no longer auditable' >&2; exit 1;
        }
    else
        # Archive the actual post-make config, not a pre-syncconfig guess.
        cp "$out/.config" "$MAIN/kernel-config-$name"
    fi
    local dtb="$out/arch/arm64/boot/dts/qcom/$dtb_name.dtb"
    test -s "$out/arch/arm64/boot/Image.gz"
    test -s "$dtb"
    cp "$dtb" "$MAIN/$name.dtb"
    # Match the public J4+ ARM64 prebuilt: plain gzip, no optional FNAME,
    # followed directly by the DTB. Reference Qualcomm LK uses the exact
    # gzip end as the appended-DTB offset, even if that offset is unaligned.
    python3 "$here/mainline-payload.py" \
        --image "$out/arch/arm64/boot/Image.gz" --dtb "$dtb" \
        --out "$MAIN/$name-Image.gz-dtb"
    git -C "$KERNEL" show -s --format='%H %ci %s' HEAD > "$MAIN/$name-kernel-commit.txt" 2>/dev/null || \
        echo "$J4_MAINLINE_SHA (no git metadata)" > "$MAIN/$name-kernel-commit.txt"
    # A module can never be loaded here: the initramfs and the rootfs are armv7
    # and carry no arm64 modules for this kernel, so anything that leaked to =m
    # is a driver that will silently be absent at boot.
    if find "$out" -name '*.ko' | grep -q .; then
        echo 'CONFIG_MODULES leaked into a mainline build:' >&2
        find "$out" -name '*.ko' | head -5 >&2
        exit 1
    fi
    python3 "$here/mainline-config.py" check --config "$out/.config" "${config_args[@]}"
    echo "== $name built: $(ls -l "$MAIN/$name-Image.gz-dtb" | awk '{print $5}') bytes"
}

# 2. Build. The M1 tree and the M2 tree come out of the same `make dtbs`: the
# panel variant is a separate source file that #defines the milestone symbol,
# because dtc has no -D and the kernel preprocesses dts with cpp.
build_variant m1 msm8917-samsung-j4primelte
if [ "$PANEL" = 1 ]; then
    build_variant panel msm8917-samsung-j4primelte-panel
fi

# 3. Pack. Without a donor boot.img (no artifact access) the kernel payloads are
# still published and the job says so plainly instead of pretending.
if [ ! -f "${J4_MAINLINE_BASE_BOOT:-/nonexistent}" ]; then
    echo 'No base boot.img given: kernel payloads staged, boot images skipped.' \
        | tee "$OUT/skipped.txt"
    exit 0
fi
# A previous donor-less run leaves this file behind, and an artifact directory
# that says "boot images skipped" next to real boot images is a lie.
rm -f "$OUT/skipped.txt"
# Same parser the verifier and the packer use, so the note cannot disagree.
python3 "$here/donor-note.py" "$J4_MAINLINE_BASE_BOOT" "$MAIN/ramdisk.txt" \
    --expect-sha256 "$J4_MAINLINE_DONOR_SHA256" \
    --expect-root-uuid "$J4_MAINLINE_DONOR_ROOT_UUID"
cat "$MAIN/ramdisk.txt"

variants=(m1)
if [ "$PANEL" = 1 ]; then variants+=(panel); fi
for variant in "${variants[@]}"; do
    for mode in rootfs debug; do
        flags=()
        name="boot-$variant-$mode.img"
        [ "$mode" = debug ] && flags=(--debug-shell)
        python3 "$here/mainline-repack-boot.py" \
            --base "$J4_MAINLINE_BASE_BOOT" \
            --kernel "$MAIN/$variant-Image.gz-dtb" \
            --out "$OUT/$name" "${flags[@]}" \
            --report "$OUT/$name.txt" >> "$LOGS/boot-pack.log" 2>&1
        python3 - "$OUT/$name" <<'PY'
import struct
import sys
import zlib
from pathlib import Path
data = Path(sys.argv[1]).read_bytes()
size, addr, rsize, raddr, ssize, saddr, tags, page, ver = struct.unpack_from('<9I', data, 8)
assert data[:8] == b'ANDROID!' and ver == 0 and page in (2048, 4096)
assert 0 < size < len(data) and rsize > 0, 'empty kernel or ramdisk payload'
align = lambda n: (n + page - 1) // page * page
assert len(data) >= page + align(size) + align(rsize) + align(ssize), 'truncated'
assert len(data) <= 32 * 1024 * 1024, 'boot image over the 32 MiB BOOT partition'
# Locate the DTB by gzip EOF, not a magic search or an alignment assumption.
# The J4+ ARM64 reference puts the first FDT at offset 1 mod 8.
kern = data[page:page + size]
assert len(kern) >= 18 and kern[:4] == b'\x1f\x8b\x08\x00', 'not plain gzip'
stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
stream.decompress(kern)
stream.flush()
assert stream.eof, 'truncated gzip kernel'
at = len(kern) - len(stream.unused_data)
assert len(kern) - at >= 40 and kern[at:at + 4] == bytes.fromhex('d00dfeed'), \
    'no DTB header exactly at gzip EOF'
assert struct.unpack_from('>I', kern, at + 4)[0] == len(kern) - at, \
    'the appended DTB does not reach the end of the kernel payload'
print(f'{sys.argv[1]}: {len(data)} B, kernel {size} B, ramdisk {rsize} B, ok')
PY
    done
done
(
    cd "$OUT"
    sha256sum *.img > SHA256SUMS
)
cp "$LOGS/mainline-sources.env" "$OUT/"
du -sh "$MAIN" "$OUT"
echo "mainline build done: $(ls "$OUT" | tr '\n' ' ')"
