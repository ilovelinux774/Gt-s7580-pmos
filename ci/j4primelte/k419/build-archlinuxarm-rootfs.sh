#!/usr/bin/env bash
# Fetch and authenticate the official Arch Linux ARM aarch64 rootfs archive.
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
sig_tmp=$(mktemp "${TMPDIR:-/tmp}/j4-archroot-sig.XXXXXX")
curl_error=$(mktemp "${TMPDIR:-/tmp}/j4-archroot-curl.XXXXXX")
verify_error=$(mktemp "${TMPDIR:-/tmp}/j4-archroot-gpgv.XXXXXX")
verify_status=$(mktemp "${TMPDIR:-/tmp}/j4-archroot-status.XXXXXX")
keyring_tmp=$(mktemp "${TMPDIR:-/tmp}/j4-archroot-keyring.XXXXXX")
trap 'rm -f "$tmp" "$sig_tmp" "$curl_error" "$verify_error" "$verify_status" "$keyring_tmp"' EXIT

for tool in curl python3 gpgv sha256sum; do
  command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }
done

download() {
  local url=$1 destination=$2 description=$3 curl_rc detail
  : > "$curl_error"
  if curl --fail --location --retry 4 --retry-delay 2 --connect-timeout 30 \
    --output "$destination" "$url" 2>"$curl_error"; then
    cat "$curl_error" >&2
  else
    curl_rc=$?
    cat "$curl_error" >&2
    detail=$(tail -n 5 "$curl_error" | tr '\r\n' ' ' | sed 's/%/%25/g; s/::/%3A%3A/g')
    echo "::error title=Arch Linux ARM $description download::curl exited $curl_rc: $detail"
    if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
      printf '\nArch Linux ARM %s download failed (curl %s): `%s`\n' \
        "$description" "$curl_rc" "$detail" >> "$GITHUB_STEP_SUMMARY"
    fi
    return "$curl_rc"
  fi
  [[ -s $destination ]] || { echo "downloaded $description is empty" >&2; return 1; }
}

download "$ARCHLINUXARM_ROOTFS_URL" "$tmp" rootfs
download "$ARCHLINUXARM_ROOTFS_URL.sig" "$sig_tmp" signature

echo "$ARCHLINUXARM_KEYRING_SHA256  $HERE/archlinuxarm.asc" | sha256sum -c -
python3 - "$HERE/archlinuxarm.asc" "$keyring_tmp" <<'PY'
import base64
import sys
from pathlib import Path

source, target = map(Path, sys.argv[1:])
lines = source.read_text(encoding="ascii").splitlines()
inside = False
ended = False
payload = []
for line in lines:
    if line == "-----BEGIN PGP PUBLIC KEY BLOCK-----":
        if inside:
            raise SystemExit("nested ASCII-armored key block")
        inside = True
        continue
    if line == "-----END PGP PUBLIC KEY BLOCK-----":
        ended = True
        break
    if not inside or not line or line.startswith("=") or ":" in line:
        continue
    payload.append(line)
if not inside or not ended or not payload:
    raise SystemExit("invalid ASCII-armored Arch Linux ARM keyring")
data = base64.b64decode("".join(payload), validate=True)
if not data or data[0] not in (0x98, 0x99, 0xC6):
    raise SystemExit("dearmored keyring has no OpenPGP public-key packet")
target.write_bytes(data)
PY
set +e
gpgv --status-fd 1 --keyring "$keyring_tmp" \
  "$sig_tmp" "$tmp" >"$verify_status" 2>"$verify_error"
gpgv_rc=$?
set -e
cat "$verify_error" >&2
if ((gpgv_rc != 0)); then
  detail=$(tail -n 5 "$verify_error" | tr '\r\n' ' ' | sed 's/%/%25/g; s/::/%3A%3A/g')
  echo "::error title=Arch Linux ARM rootfs signature invalid::gpgv exited $gpgv_rc: $detail"
  exit "$gpgv_rc"
fi
if ! awk -v expected="$ARCHLINUXARM_BUILDER_FINGERPRINT" \
  '$1 == "[GNUPG:]" && $2 == "VALIDSIG" && (toupper($3) == expected || toupper($NF) == expected) { valid = 1 } END { exit !valid }' \
  "$verify_status"; then
  echo "::error title=Arch Linux ARM signer mismatch::signature is not by pinned builder $ARCHLINUXARM_BUILDER_FINGERPRINT"
  exit 1
fi
printf 'Verified rootfs detached signature from pinned Arch Linux ARM builder %s\n' \
  "$ARCHLINUXARM_BUILDER_FINGERPRINT"

# Validate the authenticated archive without extracting it yet. Arch Linux ARM
# aarch64 binaries use ELF64 with e_machine=183 (EM_AARCH64).
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
install -m 0644 "$sig_tmp" "$out.sig"
sha256sum "$out" > "$out.sha256"
printf 'rootfs: %s\nlabel required by boot initramfs: %s\n' \
  "$out" "$ARCHLINUXARM_ROOT_LABEL"
cat "$out.sha256"
