#!/usr/bin/env bash
# Build Artix's pinned OpenRC fork natively for the Arch Linux ARM rootfs.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <openrc-source-dir> <staging-dir>" >&2
  exit 2
fi

src=$(realpath "$1")
stage=$(realpath -m "$2")
[[ -f "$src/Makefile" ]] || { echo "not an OpenRC source tree: $src" >&2; exit 1; }
[[ $(uname -m) == aarch64 ]] || {
  echo "build OpenRC on an AArch64 runner; host is $(uname -m)" >&2
  exit 1
}
rm -rf "$stage"
mkdir -p "$stage"

# Build Artix's OpenRC fork (which includes openrc-init) for PID 1. Keep the
# Artix package layout and avoid optional PAM/SELinux/networking dependencies
# in this first bootable image.
make_args=(
  "BRANDING=Artix Linux"
  MKPAM=
  MKSELINUX=no
  MKTERMCAP=ncurses
  MKNET=no
  MKBASHCOMP=no
  MKZSHCOMP=no
  PREFIX=/usr
  BINDIR=/usr/bin
  SBINDIR=/usr/bin
  SYSCONFDIR=/etc
  LIBDIR=/usr/lib
  SHLIBDIR=/usr/lib
  LIBEXECDIR=/usr/lib/openrc
)
make -C "$src" "${make_args[@]}"
make -C "$src" "${make_args[@]}" DESTDIR="$stage" install
# Artix's package installs this SysV-compatible init table for openrc-init.
install -D -m 0644 "$src/support/sysvinit/inittab" "$stage/etc/inittab"

init_bin=$(find "$stage" -type f -name openrc-init -print -quit)
openrc_bin=$(find "$stage" -type f -name openrc -print -quit)
[[ -n $init_bin && -n $openrc_bin ]] || {
  echo "OpenRC install omitted openrc-init or openrc" >&2
  find "$stage" -maxdepth 6 -type f -print >&2
  exit 1
}
readelf -h "$init_bin" | grep -q 'Machine:.*AArch64' || {
  echo "openrc-init is not AArch64: $init_bin" >&2
  exit 1
}
readelf -h "$openrc_bin" | grep -q 'Machine:.*AArch64' || {
  echo "openrc is not AArch64: $openrc_bin" >&2
  exit 1
}
printf 'Artix OpenRC overlay staged for AArch64: %s\n' "$stage"
sha256sum "$init_bin" "$openrc_bin"
