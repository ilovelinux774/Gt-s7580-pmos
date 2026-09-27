#!/usr/bin/env bash
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
# shellcheck source=ci/j4primelte/sources.env
source "$ROOT/ci/j4primelte/sources.env"
PMB="$HOME/j4-pmbootstrap"
PORTS="$HOME/j4-pmaports"
WORK="$HOME/j4-pmos-work"
CFG="$HOME/j4-pmbootstrap.cfg"
IMAGES="$ROOT/artifacts/images"
LOGS="$ROOT/artifacts/logs"
mkdir -p "$IMAGES" "$LOGS"
checkout_pin() {
    local url=$1 sha=$2 dest=$3
    git init "$dest"
    git -C "$dest" remote add origin "$url"
    git -C "$dest" fetch --depth=1 origin "$sha"
    git -C "$dest" checkout --detach FETCH_HEAD
    test "$(git -C "$dest" rev-parse HEAD)" = "$sha"
}
checkout_pin "$PMBOOTSTRAP_URL" "$PMBOOTSTRAP_COMMIT" "$PMB"
checkout_pin "$PMAPORTS_URL" "$PMAPORTS_COMMIT" "$PORTS"
# pmbootstrap reads channel metadata from origin/main, even when the working
# tree is a pinned release commit. Fetch the pinned metadata into that ref
# without changing the release checkout.
git -C "$PORTS" fetch --depth=1 origin \
    "$PMAPORTS_CHANNELS_COMMIT:refs/remotes/origin/main"
test "$(git -C "$PORTS" rev-parse HEAD)" = "$PMAPORTS_COMMIT"
test "$(git -C "$PORTS" rev-parse refs/remotes/origin/main)" = "$PMAPORTS_CHANNELS_COMMIT"
git -C "$PORTS" show refs/remotes/origin/main:channels.cfg > "$LOGS/channels.cfg"
mkdir -p "$PORTS/device/downstream"
cp -a "$ROOT/pmaports/device/downstream/"* "$PORTS/device/downstream/"
python3 "$ROOT/ci/j4primelte/patch-pmbootstrap.py" "$PMB"
git -C "$PMB" diff > "$LOGS/pmbootstrap.patch"

cat > "$CFG" <<EOF
[pmbootstrap]
aports = $PORTS
work = $WORK
device = samsung-j4primelte
ui = console
user = user
hostname = j4-debug
service_manager = openrc
timezone = Europe/Istanbul
locale = C.UTF-8
extra_space = 128
build_pkgs_on_install = False
ssh_keys = False
[providers]
[mirrors]
alpine = https://dl-cdn.alpinelinux.org/alpine/
pmaports = https://mirror.postmarketos.org/postmarketos/
EOF
# Initialize a new CI work directory without interactive init or ignored errors.
mkdir -p "$WORK/cache_git"
python3 - "$PMB" "$WORK" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import pmb.config
(Path(sys.argv[2]) / 'version').write_text(f'{pmb.config.work_version}\n')
PY
PM=(python3 "$PMB/pmbootstrap.py" -c "$CFG" --details-to-stdout --assume-yes)
"${PM[@]}" work_migrate
"${PM[@]}" status > "$LOGS/pmbootstrap-status.txt"
# Exercise the real metadata parser and native APK/chroot setup before the
# long kernel compile. No fake kernel or flashable image is produced here.
"${PM[@]}" apkbuild_parse linux-samsung-j4primelte device-samsung-j4primelte \
    > "$LOGS/apkbuild-parse.txt"
"${PM[@]}" chroot -- sh -ec 'apk --version; echo "Native pmbootstrap preflight passed"'
printf '%s\n' "$PMBOOTSTRAP_COMMIT" "$PMAPORTS_COMMIT" "$PMAPORTS_CHANNELS_COMMIT" \
    > "$WORK/j4-preflight-pins"
echo 'PMBOOTSTRAP_PREFLIGHT_OK'
