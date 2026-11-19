#!/usr/bin/env bash
# Package the reported booting 3.18.120 Medusa kernel/DTB with the Arch root switch.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: $0 <repack-boot> <root-switch-initramfs.cpio.xz> <output-boot.img> <root-label>" >&2
  exit 2
fi

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/../../.." && pwd)
# shellcheck disable=SC1091
source "$ROOT/ci/j4primelte/k419/sources.env"
repack=$(realpath "$1")
ramdisk=$(realpath "$2")
out=$(realpath -m "$3")
label=$4
[[ -x $repack && -s $ramdisk ]] || { echo "repacker/initramfs missing" >&2; exit 1; }
[[ $label =~ ^[A-Za-z0-9_-]+$ ]] || { echo "invalid root label: $label" >&2; exit 1; }

kernel="$ROOT/ci/j4primelte/k419/medusa-kernel-with-dtb.payload"
dtb="$ROOT/ci/j4primelte/k419/medusa-j4primelte-sea-open-r06.dtb"
echo "$MEDUSA_KERNEL_PAYLOAD_SHA256  $kernel" | sha256sum -c -
echo "$MEDUSA_J4_DTB_SHA256  $dtb" | sha256sum -c -
xz -t "$ramdisk"

cmdline="console=ttyMSM0,115200n8 loglevel=8 root=LABEL=$label rootfstype=ext4 rootwait rw"
mkdir -p "$(dirname "$out")"
"$repack" build \
  --kernel "$kernel" \
  --ramdisk "$ramdisk" \
  --out "$out" \
  --page-size "$MEDUSA_BOOT_PAGE_SIZE" \
  --kernel-addr "$MEDUSA_BOOT_KERNEL_ADDR" \
  --ramdisk-addr "$MEDUSA_BOOT_RAMDISK_ADDR" \
  --second-addr "$MEDUSA_BOOT_SECOND_ADDR" \
  --tags-addr "$MEDUSA_BOOT_TAGS_ADDR" \
  --cmdline "$cmdline" | tee "$out.build-report.txt"
python3 "$ROOT/ci/j4primelte/k419/verify-medusa-dtb-boot.py" \
  "$out" "$dtb" --expect-kernel-version "$MEDUSA_KERNEL_VERSION" \
  --root-label "$label" | tee "$out.report.txt"
bytes=$(stat -c %s "$out")
if ((bytes > J4_BOOT_PARTITION_BYTES)); then
  echo "BOOT image exceeds the J4+ limit: $bytes > $J4_BOOT_PARTITION_BYTES bytes" >&2
  exit 1
fi
sha256sum "$out" > "$out.sha256"
printf 'Medusa 3.18 + Artix OpenRC BOOT image: %s / %s bytes\n' \
  "$bytes" "$J4_BOOT_PARTITION_BYTES"
cat "$out.sha256"
