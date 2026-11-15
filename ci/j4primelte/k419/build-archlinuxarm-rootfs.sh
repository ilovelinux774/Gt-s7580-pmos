#!/usr/bin/env bash
# Fetch and validate the official Arch Linux ARM aarch64 rootfs archive.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <output-ArchLinuxARM-aarch64.tar.gz>" >&2
  exit 2
fi

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
source "$HERE/sources.env"
out=$(realpath -m "$1")
mkdir -p "$(dirname "$out")"
tmp=$(mktemp "${TMPDIR:-/tmp}/j4-archroot.XXXXXX")
trap 'rm -f "$tmp"' EXIT

command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

curl --fail --location --retry 4 --retry-delay 2 --connect-timeout 30 \
  --output "$tmp" "$ARCHLINUXARM_ROOTFS_URL"
test -s "$tmp"

# Validate the archive without extracting an untrusted tarball onto the runner.
# Arch Linux ARM aarch64 binaries use ELF64 with e_machine=183 (EM_AARCH64).
python3 - "$tmp" <<'PY'
import sys
import tarfile

archive = sys.argv[1]
with tarfile.open(archive, "r:gz") as tf:
    names = {member.name.lstrip("./"): member for member in tf.getmembers()}
    required = ("usr/bin/bash", "usr/lib/systemd/systemd", "etc/os-release")
    missing = [name for name in required if name not in names]
    if missing:
        raise SystemExit(f"rootfs archive is missing expected paths: {missing}")
    for path in required[:2]:
        stream = tf.extractfile(names[path])
        if stream is None:
            raise SystemExit(f"cannot read {path} from rootfs archive")
        header = stream.read(20)
        if len(header) < 20 or header[:4] != b"\x7fELF":
            raise SystemExit(f"{path} is not an ELF executable")
        elf_class = header[4]
        byte_order = "little" if header[5] == 1 else "big" if header[5] == 2 else None
        if elf_class != 2 or byte_order is None:
            raise SystemExit(f"{path} is not a valid ELF64 executable")
        machine = int.from_bytes(header[18:20], byte_order)
        if machine != 183:
            raise SystemExit(f"{path} has ELF machine {machine}, expected AArch64 (183)")
    payload_bytes = sum(member.size for member in names.values() if member.isfile())
    print(f"Validated Arch Linux ARM aarch64 rootfs archive ({len(names)} entries)")
    print(f"Uncompressed regular-file payload: {payload_bytes} bytes")
PY

install -m 0644 "$tmp" "$out"
sha256sum "$out" > "$out.sha256"
printf 'rootfs: %s\nlabel required by boot initramfs: %s\n' \
  "$out" "$ARCHLINUXARM_ROOT_LABEL"
cat "$out.sha256"
