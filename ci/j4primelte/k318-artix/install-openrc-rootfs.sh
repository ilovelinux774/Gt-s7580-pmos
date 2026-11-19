#!/usr/bin/env bash
# Install the Artix OpenRC init layer into the Arch Linux ARM ext4 rootfs.
# The ALARM package set is retained initially; OpenRC, not systemd, is PID 1.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <rootfs.img> <openrc-staging-dir>" >&2
  exit 2
fi
[[ $EUID -eq 0 ]] || { echo "run as root to mount and update rootfs.img" >&2; exit 1; }
image=$(realpath "$1")
stage=$(realpath "$2")
[[ -s $image ]] || { echo "rootfs image is missing/empty: $image" >&2; exit 1; }
[[ -x "$stage/usr/bin/openrc-init" && -x "$stage/usr/bin/openrc" ]] || {
  echo "OpenRC overlay is incomplete: $stage" >&2; exit 1;
}

mnt=$(mktemp -d "${TMPDIR:-/tmp}/j4-artix-openrc-mnt.XXXXXX")
loopdev=''
mounted=0
cleanup() {
  if ((mounted)); then umount "$mnt" || true; fi
  if [[ -n $loopdev ]]; then losetup -d "$loopdev" || true; fi
  rmdir "$mnt" 2>/dev/null || true
}
trap cleanup EXIT
loopdev=$(losetup --find --show "$image")
mount "$loopdev" "$mnt"
mounted=1

# Copy the Artix-built OpenRC tree. Keep unused systemd libraries/binaries for
# compatibility with Arch ARM packages, but replace both init entry points.
cp -a "$stage"/. "$mnt"/
[[ -x "$mnt/usr/bin/openrc-init" && -x "$mnt/usr/bin/openrc" ]] || {
  echo "OpenRC binaries were not installed into the rootfs" >&2; exit 1;
}
rm -f "$mnt/usr/bin/init" "$mnt/sbin/init"
ln -s /usr/bin/openrc-init "$mnt/usr/bin/init"
if [[ -L "$mnt/sbin" ]]; then
  ln -sfn /usr/bin/openrc-init "$mnt/sbin/init"
else
  mkdir -p "$mnt/sbin"
  ln -sfn /usr/bin/openrc-init "$mnt/sbin/init"
fi

# Install the legacy Samsung Android composite RNDIS setup and its SSH
# listener. Configfs is unavailable in this 3.18 kernel; userspace activates
# the already-built-in android_usb function via its class sysfs attributes.
install -D -m 0755 "$(dirname "$0")/j4-rndis.sh" "$mnt/usr/libexec/j4-rndis"
install -D -m 0755 "$(dirname "$0")/j4-rndis.initd" "$mnt/etc/init.d/j4-rndis"
install -D -m 0600 "$(dirname "$0")/sshd-rndis.conf" "$mnt/etc/ssh/sshd_config.j4-rndis"
mkdir -p "$mnt/etc/runlevels/default"
ln -sfn /etc/init.d/j4-rndis "$mnt/etc/runlevels/default/j4-rndis"

# Fail the image build rather than silently shipping a service that cannot
# configure the link or provide SSH access.
for required in usr/bin/ip usr/bin/sshd usr/bin/ssh-keygen usr/bin/start-stop-daemon; do
  [[ -x "$mnt/$required" ]] || {
    echo "RNDIS access requires missing rootfs executable: /$required" >&2
    exit 1
  }
done
grep -q '^alarm:' "$mnt/etc/passwd" || {
  echo "RNDIS SSH setup requires the Arch Linux ARM alarm account" >&2
  exit 1
}

# OpenRC's upstream SysV-compatible inittab is supplied by the source tree in
# the overlay. No UART is assumed; the RNDIS/SSH link is the access path.
if [[ ! -s "$mnt/etc/inittab" ]]; then
  echo "OpenRC install did not provide /etc/inittab" >&2
  exit 1
fi

mkdir -p "$mnt/etc/runlevels"/{sysinit,boot,default,shutdown}
# Keep the first boot deliberately small: the initramfs already mounts /,
# devtmpfs, procfs and sysfs. Start the stock Arch ARM udev daemon if present.
if [[ -x "$mnt/etc/init.d/udev" ]]; then
  ln -sfn /etc/init.d/udev "$mnt/etc/runlevels/sysinit/udev"
fi
if [[ -x "$mnt/etc/init.d/hostname" ]]; then
  ln -sfn /etc/init.d/hostname "$mnt/etc/runlevels/boot/hostname"
fi

rc_conf="$mnt/etc/rc.conf"
[[ -f $rc_conf ]] || { echo "OpenRC did not install /etc/rc.conf" >&2; exit 1; }
python3 - "$rc_conf" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
text = p.read_text()
values = {'rc_logger': 'YES', 'rc_parallel': 'NO'}
for key, value in values.items():
    lines = text.splitlines()
    replacement = f'{key}="{value}"'
    found = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(f'{key}=') or line.lstrip().startswith(f'#{key}='):
            lines[i] = replacement
            found = True
            break
    if not found:
        lines.append(replacement)
    text = '\n'.join(lines) + '\n'
p.write_text(text)
PY

# Assert the selected init resolves to OpenRC, not systemd. The rootfs is an
# Arch Linux ARM seed with the Artix OpenRC PID 1 layer, not an official Artix
# aarch64 release; leave its os-release truthful.
target=$(readlink "$mnt/usr/bin/init")
[[ $target == /usr/bin/openrc-init ]] || { echo "unexpected /usr/bin/init: $target" >&2; exit 1; }
[[ -x "$mnt/usr/bin/openrc-init" ]] || exit 1
chroot "$mnt" /usr/bin/openrc --version >/dev/null
[[ $(chroot "$mnt" readlink -f /sbin/init) == /usr/bin/openrc-init ]] || {
  echo "chroot resolves /sbin/init to the wrong executable" >&2; exit 1;
}
if [[ -e "$mnt/sbin/init" ]]; then
  [[ $(readlink "$mnt/sbin/init") == /usr/bin/openrc-init ]] || {
    echo "unexpected /sbin/init target: $(readlink "$mnt/sbin/init")" >&2; exit 1;
  }
fi
sync
umount "$mnt"
mounted=0
losetup -d "$loopdev"
loopdev=''
e2fsck -fn "$image" >/dev/null
sha256sum "$image" > "$image.sha256"
printf 'OpenRC rootfs ready: %s\n' "$image"
cat "$image.sha256"
