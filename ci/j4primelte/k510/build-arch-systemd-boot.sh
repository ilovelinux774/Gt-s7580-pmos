#!/usr/bin/env bash
# Replace the donor initramfs with the Arch root-switch initramfs and add QCDT.
set -euo pipefail

if [[ $# -ne 8 ]]; then
  echo "usage: $0 <repack-boot> <donor.img> <kernel> <root-initramfs> <qcdt> <root-label> <boot-limit-bytes> <output.img>" >&2
  exit 2
fi

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
repack=$(realpath "$1")
donor=$(realpath "$2")
kernel=$(realpath "$3")
ramdisk=$(realpath "$4")
qcdt=$(realpath "$5")
label=$6
limit=$7
out=$(realpath -m "$8")

[[ -x $repack ]] || { echo "repack-boot is not executable: $repack" >&2; exit 1; }
for input in "$donor" "$kernel" "$ramdisk" "$qcdt"; do
  [[ -s $input ]] || { echo "required input is empty/missing: $input" >&2; exit 1; }
done
[[ $label =~ ^[A-Za-z0-9_-]+$ ]] || { echo "invalid root label: $label" >&2; exit 1; }
[[ $limit =~ ^[0-9]+$ ]] && ((limit > 0)) || { echo "invalid BOOT limit: $limit" >&2; exit 1; }
mkdir -p "$(dirname "$out")"

cmdline=$(python3 "$HERE/../k419/arch-root-cmdline.py" "$donor" "$label")
if ! grep -Fqw "root=LABEL=$label" <<< "$cmdline"; then
  echo "root label missing from generated kernel command line" >&2
  exit 1
fi
if grep -Eq '(^| )(init|rdinit)=' <<< "$cmdline"; then
  echo "unexpected init/rdinit override in generated command line" >&2
  exit 1
fi
printf '%s\n' "$cmdline" > "$out.cmdline.txt"

"$repack" repack "$donor" \
  --kernel-new "$kernel" \
  --ramdisk-new "$ramdisk" \
  --dt "$qcdt" \
  --cmdline-replace "$cmdline" \
  --cmdline-clear-extra \
  --out "$out" | tee "$out.repack-report.txt"
"$repack" inspect "$out" | tee "$out.report.txt"
bytes=$(stat -c %s "$out")
if ((bytes > limit)); then
  echo "BOOT image exceeds partition limit: $bytes > $limit bytes" >&2
  exit 1
fi
sha256sum "$out" > "$out.sha256"
printf 'Arch Linux ARM systemd BOOT image: %s / %s bytes\n' "$bytes" "$limit"
cat "$out.sha256"
