#!/usr/bin/env bash
# Build the arm64 mainline kernel for the J4+ and pack it into BOOT images.
#
# This is the mainline twin of ci/j4primelte/build-kernel.sh, and it deliberately
# produces no rootfs: the phone keeps the postmarketOS userspace it already has,
# and the boot image reuses that job's initramfs as a donor. See
# mainline-repack-boot.py for why swapping only the kernel is safe.
#
#   J4_MAINLINE_BASE_BOOT   donor boot.img (from j4primelte-boot-only-*)
#   J4_MAINLINE_PANEL=1     also build the M2 panel variant
#   J4_MAINLINE_SKIP_APT=1  dependencies are already installed (CI does this)
#   J4_MAINLINE_KERNEL_TREE where the pinned tree lives (~/j4-mainline-kernel)
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
    for helper in mainline-config.py mainline-kernel.py mainline-repack-boot.py \
                  verify-mainline.py donor-note.py test-mainline.py; do
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
LOGS="$ROOT/artifacts/logs"
MAIN="$ROOT/artifacts/mainline"
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
"${CROSS}gcc" --version | head -1 | tee "$LOGS/toolchain.txt"
dtc --version | tee -a "$LOGS/toolchain.txt"

build_variant() {
    local name=$1
    local dtb_name=$2
    local out="$HOME/j4-mainline-out-$name"
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
        cp "$out/.config" "$MAIN/kernel-config-$name"
    fi
    test -s "$MAIN/kernel-config-$name" || {
        echo "no kernel-config-$name to pack against: run without J4_MAINLINE_DTBS_ONLY" >&2
        exit 1
    }
    # Word-split on purpose: the target list is one or two words.
    local targets='Image.gz dtbs'
    [ "$DTBS_ONLY" = 1 ] && targets=dtbs
    make -C "$KERNEL" O="$out" ARCH=arm64 CROSS_COMPILE="$CROSS" \
        -j"$(nproc)" $targets > "$LOGS/kernel-$name.log" 2>&1
    local dtb="$out/arch/arm64/boot/dts/qcom/$dtb_name.dtb"
    test -s "$out/arch/arm64/boot/Image.gz"
    test -s "$dtb"
    cp "$dtb" "$MAIN/$name.dtb"
    # Appended DTB, no QCDT: Samsung LK only looks for a separate dt image when
    # the header says dt_size != 0, and a v0 header from this vendor says 0, so
    # the tree it will use is the one glued to the end of the kernel payload.
    python3 - "$MAIN/$name-Image.gz-dtb" "$out/arch/arm64/boot/Image.gz" "$dtb" <<'PY'
import sys
from pathlib import Path
dest, image_path, dtb_path = (Path(a) for a in sys.argv[1:4])
image, dtb = image_path.read_bytes(), dtb_path.read_bytes()
# A bare `cat` would leave the tree at an arbitrary offset; the devicetree spec
# requires 8-byte alignment in memory, and LK locates the appended tree by
# scanning the kernel tail for d00dfeed on an aligned boundary. Zero-padding the
# compressed kernel is what kbuild's own Image.gz-dtb rule does for the same
# reason, so the tree starts at a multiple of 8 and ends at the last byte.
pad = (-len(image)) % 8
payload = image + b'\0' * pad + dtb
dest.write_bytes(payload)
data = dest.read_bytes()
assert data == payload, 'written payload does not match'
assert data[len(image) + pad:len(image) + pad + 4] == bytes.fromhex('d00dfeed')
assert len(data) - (len(image) + pad) == len(dtb), 'DTB must reach the last byte'
print(f'{len(image)} byte kernel + {pad} B padding + {len(dtb)} byte DTB appended, verified')
PY
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
python3 "$here/donor-note.py" "$J4_MAINLINE_BASE_BOOT" "$MAIN/ramdisk.txt"
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
from pathlib import Path
data = Path(sys.argv[1]).read_bytes()
size, addr, rsize, raddr, ssize, saddr, tags, page, ver = struct.unpack_from('<9I', data, 8)
assert data[:8] == b'ANDROID!' and ver == 0 and page in (2048, 4096)
assert 0 < size < len(data) and rsize > 0, 'empty kernel or ramdisk payload'
align = lambda n: (n + page - 1) // page * page
assert len(data) >= page + align(size) + align(rsize) + align(ssize), 'truncated'
assert len(data) <= 32 * 1024 * 1024, 'boot image over the 32 MiB BOOT partition'
# The tree is tens of KiB, so its magic is nowhere near the last byte: use the
# DTB's own totalsize, which must account for exactly the tail of the kernel.
kern = data[page:page + size]
at = kern.rfind(bytes.fromhex('d00dfeed'))
assert at >= 0 and at % 4 == 0, 'no DTB header in the kernel payload'
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
