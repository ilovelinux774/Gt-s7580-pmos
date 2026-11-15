#!/usr/bin/env bash
# Build a Linux 4.19.325 J4+ kernel/DTB and package the Arch ARM root-switch initramfs.
set -euo pipefail

tree=${1:?usage: build-boot-image.sh <kernel-tree> <out-dir> <J4-donor-boot.img> <arch-root-initramfs.cpio.xz> <systemd-debug-initramfs.cpio.xz>}
out=${2:?output directory}
donor=${3:?J4+ donor boot image}
ramdisk=${4:?Arch root-switch initramfs}
debug_ramdisk=${5:?systemd USB diagnostic initramfs}

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
[[ -s "$donor" && -s "$ramdisk" && -s "$debug_ramdisk" ]] || {
  echo "donor boot image or initramfs is empty/missing" >&2
  exit 1
}
version=$(sed -n 's/^VERSION *= *//p' "$tree/Makefile" | head -1)
patchlevel=$(sed -n 's/^PATCHLEVEL *= *//p' "$tree/Makefile" | head -1)
sublevel=$(sed -n 's/^SUBLEVEL *= *//p' "$tree/Makefile" | head -1)
[[ "$version.$patchlevel.$sublevel" == "$RIVA_KERNEL_VERSION" ]] || {
  echo "expected Linux $RIVA_KERNEL_VERSION, found $version.$patchlevel.$sublevel" >&2
  exit 1
}
echo "$J4_BOOT_DONOR_SHA256  $donor" | sha256sum -c -
xz -t "$ramdisk"
xz -t "$debug_ramdisk"

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
  MMC MMC_BLOCK EXT4_FS CGROUPS INOTIFY_USER SIGNALFD TIMERFD EPOLL FHANDLE
  NET UNIX TTY VT VT_CONSOLE FRAMEBUFFER_CONSOLE USB_GADGET USB_CI13XXX_MSM
  USB_CONFIGFS USB_CONFIGFS_RNDIS USB_CONFIGFS_SERIAL)
for symbol in "${required[@]}"; do
  "$cfg" --file "$out/.config" --enable "$symbol" 2>/dev/null || true
done
"$cfg" --file "$out/.config" --set-str LOCALVERSION '-j4-k419-arch'
"$cfg" --file "$out/.config" --disable LOCALVERSION_AUTO 2>/dev/null || true
# GCC 11 tightened FORTIFY_SOURCE diagnostics for this 4.19 vendor tree and
# promotes known-size legacy copy warnings to hard errors. This kernel remains
# a bring-up build; record the compatibility tradeoff rather than silently
# accepting a broken C build.
"$cfg" --file "$out/.config" --disable FORTIFY_SOURCE 2>/dev/null || true
# The IPA debugfs driver has GCC-11 object-size build failures and is not part
# of this initramfs's generic USB RNDIS path; omit this optional accelerator.
"$cfg" --file "$out/.config" --disable IPA 2>/dev/null || true
# The vendor tree has manual KSU call sites but no KSU implementation source.
"$cfg" --file "$out/.config" --disable KSU_MANUAL_HOOK 2>/dev/null || true
make -C "$tree" O="$out" "${make_args[@]}" olddefconfig
for symbol in "${required[@]}"; do
  if ! grep -qx "CONFIG_${symbol}=y" "$out/.config"; then
    echo "required Arch rootfs boot option is unavailable: CONFIG_${symbol}" >&2
    exit 1
  fi
done
if ! grep -Eq '^CONFIG_MMC_(MSM|SDHCI_MSM)=y$' "$out/.config"; then
  echo "J4+ Qualcomm MMC host driver is not built in; the root partition cannot be mounted" >&2
  exit 1
fi
for symbol in FORTIFY_SOURCE IPA KSU_MANUAL_HOOK; do
  if grep -qx "CONFIG_${symbol}=y" "$out/.config"; then
    echo "CONFIG_${symbol} remained enabled; the 4.19 bring-up compatibility trim did not apply" >&2
    exit 1
  fi
done
cp "$out/.config" "$out/config.full"

echo '=== Build the J4+ Rev06 DTB and 4.19 kernel image'
make -C "$tree" O="$out" "${make_args[@]}" -j"$jobs" Image.gz dtbs
kernel="$out/arch/arm64/boot/Image.gz"
dtb="$out/arch/arm64/boot/dts/samsung/msm8917-sec-j4primelte-sea-open-r06.dtb"
[[ -s "$kernel" ]] || { echo "missing kernel image: $kernel" >&2; exit 1; }
[[ -s "$dtb" ]] || { echo "missing J4+ Rev06 DTB: $dtb" >&2; exit 1; }
cp "$kernel" "$out/kernel.payload"
cp "$dtb" "$out/j4primelte-rev06.dtb"

# Use the Qualcomm QCDT v3 payload after the ramdisk for the standard images.
# A separate experimental image below mirrors the kernel-appended FDT layout
# observed in the user's known-booting Medusa image.
python3 "$ROOT/ci/j4primelte/k510/qcdt.py" --dtb "$out/j4primelte-rev06.dtb" \
  --out "$out/j4primelte-rev06.qcdt" --page-size "$J4_BOOT_PAGE_SIZE" \
  | tee "$out/qcdt-report.txt"
gcc -O2 -Wall -Wextra -Werror -o "$out/repack-boot" "$ROOT/ci/j4primelte/k510/repack-boot.c"
"$out/repack-boot" inspect "$donor" > "$out/donor-report.txt"
python3 "$HERE/arch-root-cmdline.py" "$donor" "$ARCHLINUXARM_ROOT_LABEL" > "$out/arch-root-cmdline.txt"
"$out/repack-boot" repack "$donor" \
  --kernel-new "$out/kernel.payload" \
  --ramdisk-new "$ramdisk" \
  --dt "$out/j4primelte-rev06.qcdt" \
  --cmdline-replace "$(cat "$out/arch-root-cmdline.txt")" \
  --cmdline-clear-extra \
  --out "$out/j4primelte-k419-arch-boot.img" | tee "$out/repack-report.txt"
"$out/repack-boot" inspect "$out/j4primelte-k419-arch-boot.img" \
  | tee "$out/boot-report.txt"
bytes=$(stat -c %s "$out/j4primelte-k419-arch-boot.img")
if (( bytes > J4_BOOT_PARTITION_BYTES )); then
  echo "BOOT image exceeds the J4+ limit: $bytes > $J4_BOOT_PARTITION_BYTES bytes" >&2
  exit 1
fi
if ! grep -Fq "root=LABEL=$ARCHLINUXARM_ROOT_LABEL" "$out/arch-root-cmdline.txt"; then
  echo 'Arch root label is missing from the kernel command line' >&2
  exit 1
fi
if grep -Eq '(^| )(init|rdinit)=' "$out/arch-root-cmdline.txt"; then
  echo 'init/rdinit override survived Arch command-line filtering' >&2
  exit 1
fi
sha256sum "$out/j4primelte-k419-arch-boot.img" > "$out/j4primelte-k419-arch-boot.img.sha256"
printf 'Arch BOOT image size: %s / %s bytes\n' "$bytes" "$J4_BOOT_PARTITION_BYTES"
cat "$out/j4primelte-k419-arch-boot.img.sha256"

# Build a temporary diagnostic BOOT image from the exact same kernel and QCDT.
# Its initramfs never mounts ARCHROOT; it starts systemd in emergency mode and
# exposes USB ACM/RNDIS for bring-up diagnosis when the framebuffer is silent.
python3 "$HERE/debug-cmdline.py" "$donor" > "$out/debug-cmdline.txt"
"$out/repack-boot" repack "$donor" \
  --kernel-new "$out/kernel.payload" \
  --ramdisk-new "$debug_ramdisk" \
  --dt "$out/j4primelte-rev06.qcdt" \
  --cmdline-replace "$(cat "$out/debug-cmdline.txt")" \
  --cmdline-clear-extra \
  --out "$out/j4primelte-k419-debug-boot.img" | tee "$out/debug-repack-report.txt"
"$out/repack-boot" inspect "$out/j4primelte-k419-debug-boot.img" \
  | tee "$out/debug-boot-report.txt"
debug_bytes=$(stat -c %s "$out/j4primelte-k419-debug-boot.img")
if (( debug_bytes > J4_BOOT_PARTITION_BYTES )); then
  echo "diagnostic BOOT image exceeds the J4+ limit: $debug_bytes > $J4_BOOT_PARTITION_BYTES bytes" >&2
  exit 1
fi
if grep -Eq '(^| )(root|rootfstype|rootflags|init|rdinit)=' "$out/debug-cmdline.txt"; then
  echo 'root/init override unexpectedly present in diagnostic command line' >&2
  exit 1
fi
sha256sum "$out/j4primelte-k419-debug-boot.img" > "$out/j4primelte-k419-debug-boot.img.sha256"
printf 'Diagnostic BOOT image size: %s / %s bytes\n' "$debug_bytes" "$J4_BOOT_PARTITION_BYTES"
cat "$out/j4primelte-k419-debug-boot.img.sha256"

# Matched Medusa diagnostic candidate: use the exact Linux 3.18.120 kernel
# payload, including its embedded J4 Rev06 DTB, from the user-supplied image that
# booted. Replace only the ramdisk/cmdline and reproduce its observed v0 header.
medusa_dtb="$HERE/medusa-j4primelte-sea-open-r06.dtb"
medusa_kernel="$HERE/medusa-kernel-with-dtb.payload"
echo "$MEDUSA_J4_DTB_SHA256  $medusa_dtb" | sha256sum -c -
echo "$MEDUSA_KERNEL_PAYLOAD_SHA256  $medusa_kernel" | sha256sum -c -
python3 - "$medusa_dtb" "$ROOT/ci/j4primelte/k510" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from qcdt import parse_root_cells
blob = Path(sys.argv[1]).read_bytes()
ids = parse_root_cells(blob)
if ids["qcom,board-id"] != [8, 6] or ids["qcom,pmic-id"] != [0x10019, 0, 0, 0]:
    raise SystemExit(f"wrong Medusa DTB board IDs: {ids}")
if b"Samsung J4PRIMELTE SEA OPEN PROJECT Rev06" not in blob:
    raise SystemExit("Medusa DTB model string is missing")
print(f"Medusa DTB: {len(blob)} bytes; ids={ids}")
PY
medusa_debug_cmdline='console=ttyMSM0,115200n8 console=tty0 loglevel=8 fbcon=map:0,scrollback:256k consoleblank=0 rd.systemd.unit=emergency.target systemd.gpt_auto=0 rd.fstab=no systemd.show_status=1'
printf '%s\n' "$medusa_debug_cmdline" > "$out/medusa-debug-cmdline.txt"
"$out/repack-boot" build \
  --kernel "$medusa_kernel" \
  --ramdisk "$debug_ramdisk" \
  --out "$out/j4primelte-medusa-matched-debug-boot.img" \
  --page-size "$MEDUSA_BOOT_PAGE_SIZE" \
  --kernel-addr "$MEDUSA_BOOT_KERNEL_ADDR" \
  --ramdisk-addr "$MEDUSA_BOOT_RAMDISK_ADDR" \
  --second-addr "$MEDUSA_BOOT_SECOND_ADDR" \
  --tags-addr "$MEDUSA_BOOT_TAGS_ADDR" \
  --cmdline "$medusa_debug_cmdline" | tee "$out/medusa-debug-build-report.txt"
python3 "$HERE/verify-medusa-dtb-boot.py" \
  "$out/j4primelte-medusa-matched-debug-boot.img" "$medusa_dtb" \
  --expect-kernel-version "$MEDUSA_KERNEL_VERSION" \
  | tee "$out/medusa-debug-boot-report.txt"
medusa_debug_bytes=$(stat -c %s "$out/j4primelte-medusa-matched-debug-boot.img")
if (( medusa_debug_bytes > J4_BOOT_PARTITION_BYTES )); then
  echo "Matched Medusa diagnostic image exceeds the BOOT limit: $medusa_debug_bytes > $J4_BOOT_PARTITION_BYTES bytes" >&2
  exit 1
fi
sha256sum "$out/j4primelte-medusa-matched-debug-boot.img" \
  > "$out/j4primelte-medusa-matched-debug-boot.img.sha256"
printf 'Matched Medusa diagnostic BOOT image size: %s / %s bytes\n' \
  "$medusa_debug_bytes" "$J4_BOOT_PARTITION_BYTES"
cat "$out/j4primelte-medusa-matched-debug-boot.img.sha256"
