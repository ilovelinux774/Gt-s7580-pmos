#!/usr/bin/env bash
# Keep the phone's known-good pmOS boot kernel/header and replace its ramdisk
# with the minimal Artix/OpenRC root handoff.
set -euo pipefail

if [[ $# -ne 5 ]]; then
  echo "usage: $0 <repack-boot> <pmos-donor.img> <artix-initramfs> <output.img> <root-label>" >&2
  exit 2
fi

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/../../.." && pwd)
# shellcheck disable=SC1091
source "$ROOT/ci/j4primelte/k318-artix/sources.env"
# shellcheck disable=SC1091
source "$ROOT/ci/j4primelte/k419/sources.env"
repack=$(realpath "$1")
donor=$(realpath "$2")
ramdisk=$(realpath "$3")
out=$(realpath -m "$4")
label=$5
[[ -x $repack && -s $donor && -s $ramdisk ]] || {
  echo "repacker, donor, or Artix initramfs is missing" >&2; exit 1;
}
[[ $label =~ ^[A-Za-z0-9_-]+$ ]] || { echo "invalid root label: $label" >&2; exit 1; }
echo "$J4_ARTIX_PMO_DONOR_SHA256  $donor" | sha256sum -c -
xz -t "$ramdisk"
mkdir -p "$(dirname "$out")"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/j4-pmos-artix-boot.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

# repack-boot currently requires a kernel input even when preserving the
# donor. Extract that payload byte-for-byte and pass it back unchanged.
python3 - "$donor" "$tmp/donor-kernel" "$tmp/cmdline" "$label" <<'PY'
from pathlib import Path
import struct
import sys

image_path, kernel_path, cmdline_path, label = sys.argv[1:]
data = Path(image_path).read_bytes()
if len(data) < 1632 or data[:8] != b"ANDROID!":
    raise SystemExit("pmOS donor is not a complete Android v0 boot image")
get = lambda offset: struct.unpack_from("<I", data, offset)[0]
page = get(36)
kernel_size = get(8)
if page not in (2048, 4096) or kernel_size == 0:
    raise SystemExit(f"invalid donor page/kernel sizes: page={page}, kernel={kernel_size}")
kernel_start = page
kernel_end = kernel_start + kernel_size
if kernel_end > len(data):
    raise SystemExit("donor kernel payload extends beyond boot image")
Path(kernel_path).write_bytes(data[kernel_start:kernel_end])

primary = data[64:64 + 512].split(b"\0", 1)[0]
extra_off = 64 + 512 + 32
extra = data[extra_off:extra_off + 512].split(b"\0", 1)[0]
try:
    inherited = (primary + b" " + extra).decode("ascii")
except UnicodeDecodeError as exc:
    raise SystemExit("donor boot command line is not ASCII") from exc

# Retain hardware/console options, but remove pmOS's root and PID-1 selection
# so the new initramfs and its ARCHROOT switch_root remain in control.
remove = {"root", "rootfstype", "rootflags", "init", "rdinit", "rootwait",
          "ro", "rw", "quiet", "splash"}
kept = []
for token in inherited.split():
    key = token.split("=", 1)[0]
    if (key in remove or key.startswith(("pmos.", "pmos_", "plymouth.",
                                         "systemd.", "rd.systemd."))):
        continue
    if token not in kept:
        kept.append(token)
kept.extend((f"root=LABEL={label}", "rootfstype=ext4", "rootwait", "rw", "loglevel=8"))
cmdline = " ".join(kept)
if len(cmdline.encode("ascii")) >= 512:
    raise SystemExit(f"filtered donor command line too long: {len(cmdline)} bytes")
if any(t.split("=", 1)[0] in {"init", "rdinit"} for t in cmdline.split()):
    raise SystemExit("pmOS init override survived filtering")
Path(cmdline_path).write_text(cmdline + "\n", encoding="ascii")
print(f"Preserving pmOS donor kernel unchanged ({kernel_size} bytes)")
print(f"Artix root-switch command line: {cmdline}")
PY

cmdline=$(cat "$tmp/cmdline")
"$repack" repack "$donor" \
  --kernel-new "$tmp/donor-kernel" \
  --ramdisk-new "$ramdisk" \
  --cmdline-replace "$cmdline" \
  --cmdline-clear-extra \
  --out "$out" | tee "$out.build-report.txt"
"$repack" inspect "$out" | tee "$out.report.txt"
python3 - "$donor" "$out" "$ramdisk" "$tmp/cmdline" <<'PY'
from pathlib import Path
import struct
import sys

old_path, new_path, ramdisk_path, cmdline_path = map(Path, sys.argv[1:])
old, new = old_path.read_bytes(), new_path.read_bytes()
ramdisk = ramdisk_path.read_bytes()
expected_cmdline = cmdline_path.read_text(encoding="ascii").rstrip("\n")

def fields(image):
    if image[:8] != b"ANDROID!":
        raise SystemExit("output lost Android boot magic")
    u32 = lambda off: struct.unpack_from("<I", image, off)[0]
    page, ksize, rsize = u32(36), u32(8), u32(16)
    kstart = page
    rstart = kstart + ((ksize + page - 1) // page) * page
    return page, ksize, rsize, kstart, rstart

op, oks, ors, oks0, ors0 = fields(old)
np, nks, nrs, nks0, nrs0 = fields(new)
if op != np or oks != nks or old[oks0:oks0 + oks] != new[nks0:nks0 + nks]:
    raise SystemExit("pmOS donor kernel/header geometry was not retained exactly")
if nrs != len(ramdisk) or new[nrs0:nrs0 + nrs] != ramdisk:
    raise SystemExit("Artix initramfs did not replace the pmOS ramdisk exactly")
cmdline = new[64:64 + 512].split(b"\0", 1)[0].decode("ascii")
extra_off = 64 + 512 + 32
extra = new[extra_off:extra_off + 512].split(b"\0", 1)[0].decode("ascii")
if cmdline != expected_cmdline or extra:
    raise SystemExit("output command line does not match filtered Artix root handoff")
print("Verified: donor kernel and boot layout retained; Artix initramfs and root cmdline installed")
PY
bytes=$(stat -c %s "$out")
if ((bytes > J4_BOOT_PARTITION_BYTES)); then
  echo "BOOT image exceeds J4+ partition limit: $bytes > $J4_BOOT_PARTITION_BYTES" >&2
  exit 1
fi
sha256sum "$out" > "$out.sha256"
printf 'pmOS donor kernel + Artix/OpenRC BOOT image: %s / %s bytes\n' \
  "$bytes" "$J4_BOOT_PARTITION_BYTES"
cat "$out.sha256"
