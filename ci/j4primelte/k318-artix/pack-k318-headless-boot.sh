#!/usr/bin/env bash
# Package the headless (no CONFIG_FB) 3.18.120 kernel with the same appended-DTB
# v0 layout, page size and load addresses as the Medusa image that was reported
# to boot on this handset. Only the gzip kernel payload differs.
set -euo pipefail

if [[ $# -lt 4 || $# -gt 5 ]]; then
  echo "usage: $0 <repack-boot> <Image.gz> <r06.dtb> <root-switch-initramfs.cpio.xz> <output-boot.img>" >&2
  echo "   or: $0 <repack-boot> --merge-dtb <Image.gz> <r06.dtb> <output-payload>" >&2
  exit 2
fi

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/../../.." && pwd)
# shellcheck disable=SC1091
source "$ROOT/ci/j4primelte/k419/sources.env"
# shellcheck disable=SC1091
source "$ROOT/ci/j4primelte/k318/sources.env"

repack=$(realpath "$1")

if [[ $2 == --merge-dtb ]]; then
  [[ $# -eq 5 ]] || { echo "usage: $0 <repack-boot> --merge-dtb <Image.gz> <r06.dtb> <out-payload>" >&2; exit 2; }
  image=$(realpath "$3")
  dtb=$(realpath "$4")
  payload=$(realpath -m "$5")
  [[ -s $image && -s $dtb ]] || { echo "kernel image or DTB is missing" >&2; exit 1; }
  # A gzip Image.gz must never already contain the DTB, or the appended copy
  # would be duplicated and the kernel would pick the wrong tree.
  if tail -c 4 "$image" | od -An -tx1 | tr -d ' \n' | grep -qi d00dfeed; then
    echo "kernel image already ends in an FDT; refusing to append a second one" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$payload")"
  cat "$image" "$dtb" > "$payload"
  printf 'merged payload: %s (%s bytes = Image.gz %s + DTB %s)\n' \
    "$payload" "$(stat -c %s "$payload")" "$(stat -c %s "$image")" "$(stat -c %s "$dtb")"
  exit 0
fi

image=$(realpath "$2")
dtb=$(realpath "$3")
ramdisk=$(realpath "$4")
out=$(realpath -m "$5")

[[ -x $repack ]] || { echo "repacker is missing: $repack" >&2; exit 1; }
[[ -s $image && -s $dtb && -s $ramdisk ]] || {
  echo "kernel image, DTB, or Artix initramfs is missing" >&2; exit 1; }
xz -t "$ramdisk"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/j4-k318-headless.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

"$HERE/pack-k318-headless-boot.sh" "$repack" --merge-dtb "$image" "$dtb" "$tmp/kernel-with-dtb"

cmdline="console=ttyMSM0,115200n8 loglevel=8 root=LABEL=${J4_K318_HEADLESS_ROOT_LABEL} rootfstype=ext4 rootwait rw"

mkdir -p "$(dirname "$out")"
"$repack" build \
  --kernel "$tmp/kernel-with-dtb" \
  --ramdisk "$ramdisk" \
  --out "$out" \
  --page-size "$MEDUSA_BOOT_PAGE_SIZE" \
  --kernel-addr "$MEDUSA_BOOT_KERNEL_ADDR" \
  --ramdisk-addr "$MEDUSA_BOOT_RAMDISK_ADDR" \
  --second-addr "$MEDUSA_BOOT_SECOND_ADDR" \
  --tags-addr "$MEDUSA_BOOT_TAGS_ADDR" \
  --cmdline "$cmdline" | tee "$out.build-report.txt"

bytes=$(stat -c %s "$out")
if ((bytes > J4_BOOT_PARTITION_BYTES)); then
  echo "BOOT image exceeds the J4+ limit: $bytes > $J4_BOOT_PARTITION_BYTES bytes" >&2
  exit 1
fi

"$repack" inspect "$out" | tee "$out.inspect.txt"
sha256sum "$out" > "$out.sha256"
printf 'J4+ headless 3.18.120 + Artix OpenRC BOOT image: %s / %s bytes\n' \
  "$bytes" "$J4_BOOT_PARTITION_BYTES"
cat "$out.sha256"
