#!/usr/bin/env bash
# Synthetic regression test for the Arch rootfs boot-image assembly path.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <repack-boot>" >&2
  exit 2
fi
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
repack=$(realpath "$1")
tmp=$(mktemp -d "${TMPDIR:-/tmp}/j4-arch-boot-test.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

printf 'synthetic J4+ kernel payload\0' > "$tmp/kernel"
printf 'synthetic root-switch initramfs\0' > "$tmp/initramfs"
"$repack" build \
  --kernel "$tmp/kernel" \
  --ramdisk "$tmp/initramfs" \
  --out "$tmp/donor.img" \
  --page-size 2048 \
  --cmdline 'console=ttyMSM0,115200n8 root=UUID=old pmos.debug-shell=1 quiet'
python3 "$HERE/test_qcdt.py" --fixture-out "$tmp/qcdt.img" >/dev/null
bash "$HERE/build-arch-systemd-boot.sh" \
  "$repack" "$tmp/donor.img" "$tmp/kernel" "$tmp/initramfs" \
  "$tmp/qcdt.img" ARCHROOT 33554432 "$tmp/arch.img" >/dev/null

python3 - "$tmp/arch.img" "$tmp/kernel" "$tmp/initramfs" <<'PY'
import struct
import sys
from pathlib import Path
image, kernel_path, ramdisk_path = map(Path, sys.argv[1:])
data = image.read_bytes()
assert data[:8] == b"ANDROID!"
kernel_size, ramdisk_size = struct.unpack_from("<II", data, 8)[0], struct.unpack_from("<I", data, 16)[0]
page_size = struct.unpack_from("<I", data, 36)[0]
dt_size = struct.unpack_from("<I", data, 40)[0]
assert page_size == 2048, page_size
assert dt_size > 0, dt_size
kernel_off = page_size
ramdisk_off = kernel_off + ((kernel_size + page_size - 1) // page_size) * page_size
qcdt_off = ramdisk_off + ((ramdisk_size + page_size - 1) // page_size) * page_size
assert data[kernel_off:kernel_off + kernel_size] == kernel_path.read_bytes()
assert data[ramdisk_off:ramdisk_off + ramdisk_size] == ramdisk_path.read_bytes()
assert data[qcdt_off:qcdt_off + dt_size][:4] == b"QCDT"
cmdline = data[64:64 + 512].split(b"\0", 1)[0].decode()
assert "root=LABEL=ARCHROOT" in cmdline, cmdline
assert "rootfstype=ext4" in cmdline and "rootwait" in cmdline and " rw" in cmdline
assert "root=UUID=old" not in cmdline and "pmos." not in cmdline
assert "quiet" not in cmdline
extra = data[64 + 512 + 32:64 + 512 + 32 + 512].split(b"\0", 1)[0]
assert not extra
print("Arch systemd boot-image test passed: kernel, root-switch initramfs, QCDT, root label and sanitized cmdline")
PY

echo "Arch systemd boot-image synthetic test passed"
