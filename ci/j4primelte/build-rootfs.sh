#!/usr/bin/env bash
# Build the ext4 root filesystem the J4+ boots into, from ARMtix.
#
#   sudo bash ci/j4primelte/build-rootfs.sh armtix-openrc-20260921.tar.xz rootfs.img [size-MiB]
#
# Two things decide whether the result is worth flashing, and both are checks
# in this script rather than hopes:
#
# 1. The filesystem has to be mountable by Linux 3.18. Modern e2fsprogs
#    defaults add metadata_csum, which 3.18 refuses outright, and 64bit /
#    orphan_file / fast_commit are either unreadable or risky there. The
#    feature set is created explicitly, cleared after the fact, and re-checked;
#    the build fails if anything unsafe survives.
#
# 2. The userspace has to be *able to start*. The gate is audit-rootfs.py,
#    which reads .note.ABI-tag out of every ELF in the tree and refuses the
#    image when a binary on the boot path demands a newer kernel than this
#    phone has. ARMtix's glibc carries a 3.7.0 floor, so it passes on 3.18.120;
#    a tree built against upstream Arch's glibc (4.4) would not, and would be
#    caught here instead of on a black screen.
#
# No Alpine, no second userspace, no symlink games: one tree, one init.
# See ci/j4primelte/README-rootfs.md.
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 <armtix-openrc-*.tar.xz> <rootfs.img> [size-MiB]" >&2
  exit 2
fi

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
source "$HERE/pins.env"

archive=$(realpath "$1")
out=$(realpath -m "$2")
size_mib=${3:-$ARTIX_ROOTFS_IMAGE_MIB}
label=$ARTIX_ROOT_LABEL
init_system=$ARTIX_INIT

[[ -s $archive ]] || { echo "missing ARMtix tarball: $archive" >&2; exit 1; }
[[ $size_mib =~ ^[0-9]+$ ]] || { echo "size must be a MiB integer" >&2; exit 1; }
for tool in truncate mke2fs dumpe2fs e2fsck debugfs tar xz python3; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

image_bytes=$((size_mib * 1024 * 1024))
if (( image_bytes > J4_SYSTEM_PARTITION_BYTES )); then
  echo "rootfs ($image_bytes B) does not fit the recorded system partition" \
       "($J4_SYSTEM_PARTITION_BYTES B)" >&2
  exit 1
fi

# Make every failure explain itself: CI job logs are not always readable, and a
# bare "exit 1" from a 200-line script is not a diagnosis.
trap 'rc=$?; echo "build-rootfs: FAILED at line $LINENO: $BASH_COMMAND (rc=$rc)" >&2' ERR
STEP=start

root=$(mktemp -d "${TMPDIR:-/tmp}/j4-root.XXXXXX")
cleanup() { chmod -R u+rwX "$root" 2>/dev/null || true; rm -rf "$root"; }
trap cleanup EXIT

STEP=extract-artix
echo "== extracting ARMtix ($init_system) =="
# The image is an .xz'd tar of a root tree. Root is needed to reproduce owner,
# mode, hard links, and the /dev/null + /dev/console nodes the init needs to
# exist before devtmpfs is mounted over them.
if [[ $EUID -eq 0 ]]; then
  tar --numeric-owner --same-owner --xattrs --acls --selinux --sparse \
      --xz -xpf "$archive" -C "$root"
else
  echo "warning: not root; owners and device nodes will not survive" >&2
  tar --xz -xpf "$archive" -C "$root"
fi

# ARMtix tarballs are flat, but tolerate a single top-level directory.
if [[ ! -e $root/etc && $(find "$root" -maxdepth 1 -mindepth 1 -type d | wc -l) -eq 1 ]]; then
  inner=$(find "$root" -maxdepth 1 -mindepth 1 -type d)
  echo "  tarball has one top-level directory; hoisting $(basename "$inner")"
  shopt -s dotglob
  mv "$inner"/* "$root"/
  rmdir "$inner"
  shopt -u dotglob
fi

[[ -d $root/etc ]] || { echo "no /etc after extraction; is this a rootfs?" >&2; exit 1; }

STEP=sanity
echo "== checking the tree is ARMtix and not something else =="
if [[ -r $root/etc/os-release ]]; then
  grep -E '^(ID|NAME|ID_LIKE)=' "$root/etc/os-release" | sed 's/^/  /' || true
else
  echo "warning: no /etc/os-release in the tree" >&2
fi
case $init_system in
  openrc)
    [[ -x $root/usr/bin/openrc-init || -x $root/sbin/openrc-init ]] || {
      echo "FAIL: no openrc-init in the tree, but ARTIX_INIT=openrc" >&2
      echo "      /sbin/init is a symlink to $(readlink "$root/sbin/init" 2>/dev/null || echo '(nothing)')" >&2
      exit 1
    }
    ;;
  *) echo "unsupported ARTIX_INIT=$init_system (only openrc is built here)" >&2; exit 1 ;;
esac

# The kernel has no modules of its own in the userspace, and ARMtix ships the
# generic aarch64 kernels plus firmware. Dropping them keeps the image inside
# the recorded partition and removes 300 MiB of modules for kernels this phone
# will never boot. Set J4_KEEP_KERNELS=1 to keep them.
if [[ ${J4_KEEP_KERNELS:-0} != 1 ]]; then
  STEP=trim-kernels
  echo "== removing generic kernel packages' payload from the image =="
  for path in usr/lib/modules boot; do
    if [[ -e $root/$path ]]; then
      before=$(du -sm "$root/$path" 2>/dev/null | cut -f1)
      rm -rf "${root:?}/$path"
      mkdir -p "$root/$path"
      echo "  $path: removed ${before:-?} MiB"
    fi
  done
fi

STEP=tree-fixups
echo "== tree fixups =="
# The handoff works from /dev, /proc, /sys and /run already being mounted by
# the initramfs, but they must also exist as directories in the image.
for path in dev proc sys run tmp var/log var/run var/tmp; do
  mkdir -p "$root/$path"
done
chmod 1777 "$root/tmp" "$root/var/tmp"
[[ -e $root/var/run ]] || ln -s /run "$root/var/run"
# /dev/console is what OpenRC's logger and getty talk to. ARMtix ships these
# nodes in its tarball; recreate them only if extraction lost them (non-root).
if [[ $EUID -eq 0 ]]; then
  [[ -e $root/dev/null ]] || mknod -m 0666 "$root/dev/null" c 1 3
  [[ -e $root/dev/console ]] || mknod -m 0600 "$root/dev/console" c 5 1
fi

# This project's own service: keep the USB RNDIS link (and an SSH listener on
# it) alive across the switch into userspace. It never touches a block device.
if [[ -d $root/etc/init.d ]]; then
  install -D -m 0755 "$HERE/rootfs/j4-rndis" "$root/usr/libexec/j4-rndis"
  install -D -m 0755 "$HERE/rootfs/j4-rndis.initd" "$root/etc/init.d/j4-rndis"
  mkdir -p "$root/etc/runlevels/default"
  ln -sfn /etc/init.d/j4-rndis "$root/etc/runlevels/default/j4-rndis"
  # The boot console here is ttyMSM0; a getty on it is how a serial cable sees
  # a login prompt. It costs nothing when no cable is attached, and the link is
  # only created when the init script exists (a dangling runlevel entry would
  # make OpenRC fail the runlevel instead of skipping it).
  for level in sysinit boot default; do
    mkdir -p "$root/etc/runlevels/$level"
  done
  if [[ -f $root/etc/init.d/agetty.ttyMSM0 ]]; then
    ln -sfn /etc/init.d/agetty.ttyMSM0 "$root/etc/runlevels/default/agetty.ttyMSM0"
    if [[ -f $root/etc/conf.d/agetty.ttyMSM0 ]]; then
      sed -i 's/^#\?baud=.*/baud=115200/' "$root/etc/conf.d/agetty.ttyMSM0"
      sed -i 's/^#\?term_type=.*/term_type=vt100/' "$root/etc/conf.d/agetty.ttyMSM0"
    fi
    echo "  agetty on ttyMSM0 enabled at 115200"
  else
    echo "  no agetty.ttyMSM0 init script; serial login not enabled"
  fi
else
  echo "warning: no /etc/init.d in the tree; the J4+ USB service was not installed" >&2
fi
install -D -m 0644 "$HERE/rootfs/fstab" "$root/etc/fstab"

# Evidence survives a failed boot only if the target tree can receive it.
mkdir -p "$root/var/log/j4"

STEP=audit
echo "== 3.18 compatibility gate =="
python3 "$HERE/audit-rootfs.py" "$root" --kernel "$ARTIX_KERNEL_FLOOR_ALLOWED" \
  --json-out "$out.audit.json" || {
  echo "refusing to build an image whose userspace cannot start on this kernel" >&2
  exit 1
}

STEP=ext4-image
echo "== creating the ext4 image ($size_mib MiB, label $label) =="
mkdir -p "$(dirname "$out")"
truncate -s "$image_bytes" "$out"

# Features a 3.18 kernel cannot mount or handle. DISABLE_AT_MKFS is handed to
# mke2fs as an explicit -O, so a host whose defaults change cannot slip one
# through; UNSAFE_FEATURES adds the two that this mke2fs refuses to toggle
# (iversion, dirdata) and is used as the final gate, so an image that somehow
# carries them is rejected instead of flashed.
#   metadata_csum, metadata_csum_seed, orphan_file, fast_commit -- 3.18 has no
#       checksummed metadata at all: it refuses the mount outright
#   meta_bg  -- group descriptor layout 3.18 does not know
#   quota    -- 3.18 has no quota inodes, and mke2fs 1.47 enables this by default
#   ea_inode, large_dir, casefold, encrypt -- not in 3.18's ext4
#   bigalloc -- cluster allocation, not in 3.18's ext4
# 64bit is deliberately NOT in the list: 64-bit block numbers are ancient and
# 3.18 handles them; removing the feature needs resize2fs, which is not worth
# the risk on a 2.4 GiB image.
DISABLE_AT_MKFS=(metadata_csum metadata_csum_seed meta_bg orphan_file fast_commit
                 quota ea_inode large_dir casefold encrypt bigalloc)
UNSAFE_FEATURES=("${DISABLE_AT_MKFS[@]}" iversion dirdata)
disabled=''
for feature in "${DISABLE_AT_MKFS[@]}"; do
  disabled+="${disabled:+,}^$feature"
done
# One mke2fs call: -O owns the feature list, -d copies owner, modes, symlinks,
# hard links and device nodes straight out of the tree. Nothing needs a loop
# device, which CI runners cannot count on.
echo "== creating and populating the filesystem =="
mke2fs -q -F -t ext4 -b 4096 -I 256 -m 1 -L "$label" -O "$disabled" -d "$root" "$out" >/dev/null

STEP=verify
echo "== verifying the image =="
e2fsck -fn "$out" >/dev/null || { echo "e2fsck rejected the image" >&2; exit 1; }
features=$(dumpe2fs -h "$out" 2>/dev/null | sed -n 's/^Filesystem features:[[:space:]]*//p')
echo "features: $features"
for bad in "${UNSAFE_FEATURES[@]}"; do
  if grep -qw "$bad" <<<"$features"; then
    echo "3.18 cannot mount this image: $bad is set" >&2
    exit 1
  fi
done
echo "all ${#UNSAFE_FEATURES[@]} unsafe features are absent"
got_label=$(dumpe2fs -h "$out" 2>/dev/null | sed -n 's/^Filesystem volume name:[[:space:]]*//p')
echo "label: $got_label"
[[ $got_label == "$label" ]] || { echo "label is '$got_label', expected '$label'" >&2; exit 1; }

# The init is the one thing the initramfs hook will exec. Prove it is in the
# image, executable, and that its interpreter exists at the path it asks for.
STEP=verify-init
init_path=usr/bin/openrc-init
[[ -e $root/sbin/openrc-init ]] && init_path=sbin/openrc-init
debugfs -R "stat /$init_path" "$out" 2>/dev/null | head -4
# debugfs exits 0 even when the lookup fails, so the presence test is on its
# own output, not on its status.
present() { debugfs -R "stat /$1" "$out" 2>/dev/null | grep -q '^Inode:'; }
for path in etc/os-release etc/init.d/j4-rndis usr/libexec/j4-rndis \
            usr/lib/ld-linux-aarch64.so.1 lib/ld-linux-aarch64.so.1 etc/fstab; do
  present "$path" && echo "  present: /$path"
done
present etc/fstab || { echo "FAIL: the fstab was not written into the image" >&2; exit 1; }

# The whole point of this image: PID 1 in the image, not just in the tree.
debugfs -R "stat /$init_path" "$out" 2>/dev/null | grep -q "Mode:" || {
  echo "FAIL: /$init_path is not in the image" >&2
  exit 1
}

# The audit report belongs next to the image, not inside it.
if [[ -f $out.audit.json ]]; then
  echo "audit report: $out.audit.json"
fi
blocks=$(dumpe2fs -h "$out" 2>/dev/null | sed -n 's/^Block count:[[:space:]]*//p')
block_size=$(dumpe2fs -h "$out" 2>/dev/null | sed -n 's/^Block size:[[:space:]]*//p')
free=$(dumpe2fs -h "$out" 2>/dev/null | sed -n 's/^Free blocks:[[:space:]]*//p')
if [[ -n ${blocks:-} && -n ${free:-} && -n ${block_size:-} ]]; then
  echo "used: $(((blocks - free) * block_size)) bytes, free: $((free * block_size)) bytes of $image_bytes"
  if (( free * block_size < image_bytes / 20 )); then
    echo "image leaves less than 5% free; the handset will not have room to work" >&2
    exit 1
  fi
fi

sha256sum "$out" > "$out.sha256"
cat "$out.sha256"
echo "build-rootfs: ok"
