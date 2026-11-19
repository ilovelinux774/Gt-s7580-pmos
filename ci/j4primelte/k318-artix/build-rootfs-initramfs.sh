#!/usr/bin/env bash
# Build this experiment's minimal initramfs with static BusyBox root discovery.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: $0 <arm64-deb-directory> <work-directory> <output-initramfs.cpio.xz> <root-filesystem-label>" >&2
  exit 2
fi

deb_dir=$(realpath "$1")
work_dir=$(realpath -m "$2")
out=$(realpath -m "$3")
root_label=$4
[[ $root_label =~ ^[A-Za-z0-9_-]+$ ]] || { echo "invalid ext4 label: $root_label" >&2; exit 1; }
root="$work_dir/root"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v dpkg-deb >/dev/null || { echo "dpkg-deb is required" >&2; exit 1; }
command -v gcc >/dev/null || { echo "gcc is required to build the static root-label helper" >&2; exit 1; }
[[ $(uname -m) == aarch64 ]] || {
  echo "build the J4+ initramfs on AArch64; host is $(uname -m)" >&2
  exit 1
}
shopt -s nullglob
packages=("$deb_dir"/*.deb)
((${#packages[@]} > 0)) || { echo "no .deb packages in $deb_dir" >&2; exit 1; }

rm -rf "$root"
mkdir -p "$root"
arm64_count=0
for package in "${packages[@]}"; do
  arch=$(dpkg-deb -f "$package" Architecture)
  [[ $arch == arm64 || $arch == all ]] || continue
  [[ $arch != arm64 ]] || ((arm64_count += 1))
  dpkg-deb -x "$package" "$root"
done
((arm64_count > 0)) || { echo "no arm64 packages found in $deb_dir" >&2; exit 1; }

# Keep only runtime files; the full Arch Linux ARM system lives on the ext4
# partition and is not packed into the J4's 32 MiB BOOT image.
rm -rf "$root/usr/share" "$root/usr/local/share" "$root/usr/include" \
  "$root/usr/lib/pkgconfig" "$root/usr/lib/cmake" \
  "$root/var/cache" "$root/var/lib"

busybox="$root/bin/busybox"
[[ -x $busybox ]] || busybox="$root/usr/bin/busybox"
[[ -x $busybox ]] || { echo "arm64 busybox-static is missing" >&2; exit 1; }
if [[ $(uname -m) == aarch64 ]]; then
  applets=$("$busybox" --list)
  for applet in sh cat echo head ls mkdir mount readlink sleep switch_root umount; do
    grep -Fxq "$applet" <<<"$applets" || {
      echo "BusyBox static is missing required applet: $applet" >&2
      exit 1
    }
  done
fi
mkdir -p "$root"/{dev,proc,sys,run,newroot,tmp,etc}
mkdir -p "$root/bin" "$root/sbin" "$root/usr/libexec"
ln -sfn "${busybox#"$root"}" "$root/bin/sh"
gcc -std=c11 -O2 -Wall -Wextra -Werror -static \
  -o "$root/sbin/find-ext4-root" "$script_dir/find-ext4-root.c"
readelf -h "$root/sbin/find-ext4-root" | grep -q 'Machine:.*AArch64' || {
  echo "find-ext4-root is not AArch64" >&2; exit 1;
}
if readelf -l "$root/sbin/find-ext4-root" | grep -q INTERP; then
  echo "find-ext4-root is not statically linked" >&2; exit 1
fi
for applet in cat echo head ls mkdir mount readlink sleep switch_root umount; do
  ln -sfn "${busybox#"$root"}" "$root/bin/$applet"
done
# Do not execute this arm64 BusyBox on x86_64 build hosts. The static BusyBox
# package supplies these applets; the target image links them by name.

cat > "$root/init" <<'EOF'
#!/bin/sh
# PID 1 in the initramfs: locate the labeled Arch root partition and switch to it.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

say() {
  echo "j4-arch-initramfs: $*" >/dev/kmsg 2>/dev/null || true
  echo "J4 Arch Linux ARM: $*" >/dev/console 2>/dev/null || true
}

mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run 2>/dev/null || true

root_spec=''
for token in $(cat /proc/cmdline); do
  case "$token" in
    root=*) root_spec=${token#root=} ;;
  esac
done
[ -n "$root_spec" ] || root_spec='LABEL=@ROOT_LABEL@'

root_device=''
root_label=''
case "$root_spec" in
  LABEL=*) root_label=${root_spec#LABEL=} ;;
  /dev/*) [ -b "$root_spec" ] && root_device=$root_spec ;;
  *) say "unsupported root= value: $root_spec" ;;
esac
tries=0
while [ -z "$root_device" ] && [ "$tries" -lt 60 ]; do
  if [ -n "$root_label" ]; then
    root_device=$(find-ext4-root "$root_label" 2>/dev/null || true)
  fi
  [ -b "$root_device" ] && break
  root_device=''
  tries=$((tries + 1))
  sleep 1
done

if [ -z "$root_device" ]; then
  say "root filesystem not found ($root_spec); entering initramfs shell"
  echo 'Available block devices:' >/dev/console
  ls /dev/mmcblk* /dev/sd* >/dev/console 2>&1 || true
  exec /bin/sh
fi

say "mounting $root_device as /"
mkdir -p /newroot
if ! mount -t ext4 -o rw "$root_device" /newroot; then
  say "failed to mount $root_device as ext4; entering initramfs shell"
  exec /bin/sh
fi

# Do not test /newroot/sbin/init directly: an absolute /usr/bin/init
# symlink is resolved against the initramfs root until switch_root changes it.
init_target=$(readlink /newroot/usr/bin/init 2>/dev/null || true)
if [ ! -x /newroot/usr/bin/openrc-init ] || [ "$init_target" != /usr/bin/openrc-init ]; then
  say "OpenRC init is missing or mislinked (target=$init_target); entering initramfs shell"
  umount /newroot 2>/dev/null || true
  exec /bin/sh
fi

mkdir -p /newroot/dev /newroot/proc /newroot/sys /newroot/run
mount -o move /dev /newroot/dev
mount -o move /proc /newroot/proc
mount -o move /sys /newroot/sys
mount -o move /run /newroot/run
say 'switching to Arch Linux ARM'
exec switch_root /newroot /sbin/init
say 'switch_root failed; entering initramfs shell'
exec /bin/sh
EOF
sed -i "s/@ROOT_LABEL@/$root_label/g" "$root/init"
chmod 0755 "$root/init"

mkdir -p "$(dirname "$out")"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python3 "$script_dir/../k419/mknewc.py" "$root" "$out"
test -s "$out"
printf 'contents: static BusyBox PID 1, static ext4-label scanner, ext4 root handoff (LABEL=%s), switch_root\n' "$root_label"
