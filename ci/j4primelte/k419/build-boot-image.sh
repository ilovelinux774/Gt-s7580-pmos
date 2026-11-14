#!/usr/bin/env bash
# Build a Linux 4.19.325 J4+ kernel/DTB and package a standalone debug initramfs.
set -euo pipefail

tree=${1:?usage: build-boot-image.sh <kernel-tree> <out-dir> <J4-donor-boot.img> <debug-initramfs.cpio.xz>}
out=${2:?output directory}
donor=${3:?J4+ donor boot image}
ramdisk=${4:?standalone debug initramfs}

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/../../.." && pwd)
# shellcheck disable=SC1091
source "$HERE/sources.env"
jobs=${JOBS:-$(nproc)}
cross=${CROSS_COMPILE:-aarch64-linux-gnu-}
make_args=(ARCH=arm64 "CROSS_COMPILE=$cross" PYTHON=python3
  'HOSTCFLAGS=-O2 -Wall -Wmissing-prototypes -Wstrict-prototypes -fomit-frame-pointer -std=gnu89 -fcommon')

[[ -f "$tree/Makefile" ]] || { echo "not a kernel source tree: $tree" >&2; exit 1; }
[[ -x "$(command -v "${cross}gcc" || true)" ]] || {
  echo "missing cross compiler: ${cross}gcc" >&2; exit 1;
}
[[ -s "$donor" && -s "$ramdisk" ]] || { echo "donor boot image or initramfs is empty/missing" >&2; exit 1; }
version=$(sed -n 's/^VERSION *= *//p' "$tree/Makefile" | head -1)
patchlevel=$(sed -n 's/^PATCHLEVEL *= *//p' "$tree/Makefile" | head -1)
sublevel=$(sed -n 's/^SUBLEVEL *= *//p' "$tree/Makefile" | head -1)
[[ "$version.$patchlevel.$sublevel" == "$RIVA_KERNEL_VERSION" ]] || {
  echo "expected Linux $RIVA_KERNEL_VERSION, found $version.$patchlevel.$sublevel" >&2
  exit 1
}
echo "$J4_BOOT_DONOR_SHA256  $donor" | sha256sum -c -
xz -t "$ramdisk"

export ARCH=arm64 CROSS_COMPILE="$cross"
export KBUILD_BUILD_USER=pmos-ci KBUILD_BUILD_HOST=j4primelte
export KBUILD_BUILD_VERSION=1 SOURCE_DATE_EPOCH=0
export KBUILD_BUILD_TIMESTAMP='Thu, 01 Jan 1970 00:00:00 +0000' LC_ALL=C
mkdir -p "$out"

python3 "$HERE/import-j4-dts.py" "$tree"
python3 "$HERE/patch-riva-kernel.py" "$tree"
make -C "$tree" O="$out" "${make_args[@]}" vendor/msm8937-perf_defconfig
cfg="$tree/scripts/config"
[[ -x "$cfg" || -f "$cfg" ]] || { echo "missing kernel scripts/config" >&2; exit 1; }
required=(BLK_DEV_INITRD RD_XZ DEVTMPFS DEVTMPFS_MOUNT PROC_FS SYSFS TMPFS
  CGROUPS INOTIFY_USER SIGNALFD TIMERFD EPOLL FHANDLE NET UNIX TTY VT
  VT_CONSOLE FRAMEBUFFER_CONSOLE USB_GADGET USB_CI13XXX_MSM USB_CONFIGFS
  USB_CONFIGFS_RNDIS USB_CONFIGFS_SERIAL)
for symbol in "${required[@]}"; do
  "$cfg" --file "$out/.config" --enable "$symbol" 2>/dev/null || true
done
"$cfg" --file "$out/.config" --set-str LOCALVERSION '-j4-k419-debug'
"$cfg" --file "$out/.config" --disable LOCALVERSION_AUTO 2>/dev/null || true
# GCC 11 tightened FORTIFY_SOURCE diagnostics for this 4.19 vendor tree and
# promotes known-size legacy copy warnings to hard errors. This kernel remains
# a bring-up build; record the compatibility tradeoff rather than silently
# accepting a broken C build.
"$cfg" --file "$out/.config" --disable FORTIFY_SOURCE 2>/dev/null || true
make -C "$tree" O="$out" "${make_args[@]}" olddefconfig
for symbol in "${required[@]}"; do
  if ! grep -qx "CONFIG_${symbol}=y" "$out/.config"; then
    echo "required diagnostic-initramfs option is unavailable: CONFIG_${symbol}" >&2
    exit 1
  fi
done
if grep -qx 'CONFIG_FORTIFY_SOURCE=y' "$out/.config"; then
  echo 'CONFIG_FORTIFY_SOURCE remained enabled; this GCC 11 compatibility workaround did not apply' >&2
  exit 1
fi
cp "$out/.config" "$out/config.full"

echo '=== Build the J4+ Rev06 DTB and 4.19 kernel image'
make -C "$tree" O="$out" "${make_args[@]}" -j"$jobs" Image.gz dtbs
kernel="$out/arch/arm64/boot/Image.gz"
dtb="$out/arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-sea-open-r06.dtb"
[[ -s "$kernel" ]] || { echo "missing kernel image: $kernel" >&2; exit 1; }
[[ -s "$dtb" ]] || { echo "missing J4+ Rev06 DTB: $dtb" >&2; exit 1; }
cp "$kernel" "$out/kernel.payload"
cp "$dtb" "$out/j4primelte-rev06.dtb"

# Use the Qualcomm QCDT v3 payload after the ramdisk, as expected by the J4+
# v0 boot header. Never append a raw DTB to the compressed kernel.
python3 "$ROOT/ci/j4primelte/k510/qcdt.py" --dtb "$out/j4primelte-rev06.dtb" \
  --out "$out/j4primelte-rev06.qcdt" --page-size "$J4_BOOT_PAGE_SIZE" \
  | tee "$out/qcdt-report.txt"
gcc -O2 -Wall -Wextra -Werror -o "$out/repack-boot" "$ROOT/ci/j4primelte/k510/repack-boot.c"
"$out/repack-boot" inspect "$donor" > "$out/donor-report.txt"
python3 "$HERE/debug-cmdline.py" "$donor" > "$out/debug-cmdline.txt"
"$out/repack-boot" repack "$donor" \
  --kernel-new "$out/kernel.payload" \
  --ramdisk-new "$ramdisk" \
  --dt "$out/j4primelte-rev06.qcdt" \
  --cmdline-replace "$(cat "$out/debug-cmdline.txt")" \
  --cmdline-clear-extra \
  --out "$out/j4primelte-k419-debug-boot.img" | tee "$out/repack-report.txt"
"$out/repack-boot" inspect "$out/j4primelte-k419-debug-boot.img" \
  | tee "$out/boot-report.txt"
bytes=$(stat -c %s "$out/j4primelte-k419-debug-boot.img")
if (( bytes > J4_BOOT_PARTITION_BYTES )); then
  echo "BOOT image exceeds the J4+ limit: $bytes > $J4_BOOT_PARTITION_BYTES bytes" >&2
  exit 1
fi
if grep -Eq '(^| )(root|rootfstype|rootflags|init|rdinit)=' "$out/debug-cmdline.txt"; then
  echo 'root/init argument survived diagnostic command-line filtering' >&2
  exit 1
fi
sha256sum "$out/j4primelte-k419-debug-boot.img" > "$out/j4primelte-k419-debug-boot.img.sha256"
printf 'BOOT image size: %s / %s bytes\n' "$bytes" "$J4_BOOT_PARTITION_BYTES"
cat "$out/j4primelte-k419-debug-boot.img.sha256"
