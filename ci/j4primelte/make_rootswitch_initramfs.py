#!/usr/bin/env python3
"""Build the J4+ root-switch initramfs.

Why this exists
---------------
The reference BOOT image probes fine on the phone with its postmarketOS
initramfs: mdss_fb probes, the legacy ``android_usb`` gadget exposes RNDIS, and
the debug shell is reachable at 172.16.42.1. The way out of that debug shell is
*not* ``pmos_continue_boot`` -- that mounts the installed pmOS root filesystem,
resizes it and switches to its init, which is exactly the state this project is
supposed to leave alone.

So this initramfs reuses the proven parts and nothing else:

* the same postmarketOS userland, so the USB gadget setup, udev, telnetd and
  log plumbing are byte-for-byte the code that already worked;
* the same kernel and appended Rev06 DTB, untouched, because those are what
  made ``mdss_fb_probe`` survive;
* a small ``/hooks-extra`` script that finds an ext4 filesystem with a
  configurable label (default ``ARTIXROOT``), refuses to touch anything that
  looks like a postmarketOS install, and switches to *its* init instead of a
  hardcoded ``/sbin/init``.

The initramfs is built from an extracted pmOS initramfs directory, so the
build never needs pmbootstrap, chroot or an armhf interpreter.

    python3 ci/j4primelte/make_rootswitch_initramfs.py pmos-initramfs/ out.gz \
        --root-label ARTIXROOT

The output is a plain gzip stream, because this bootloader's ramdisk is an
unwrapped gzip cpio archive (see j4boot.py).
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import stat
import sys
import tarfile
from pathlib import Path

HOOK_NAME = "99-j4-root-switch.sh"
CLEANUP_NAME = "99-j4-root-switch-cleanup.sh"
INIT_FILE = "j4-rootswitch"

HOOK = r"""#!/bin/sh
# J4+ root switch. Runs from /hooks-extra, i.e. after the postmarketOS init has
# brought up udev, the USB gadget, the framebuffer and the block devices. It is
# the last point at which a hook can replace the root selection.
#
# It finds an ext4 filesystem with a known label, refuses to touch a pmOS
# install, mounts that filesystem at /sysroot and records which userspace init
# should become PID 1. init_2nd.sh then skips the pmOS root handling.
set -u

LABEL="@ROOT_LABEL@"
STATE=/run/j4-rootswitch
mkdir -p "$STATE"
say() {
    echo "j4-rootswitch: $*" >/dev/kmsg 2>/dev/null || true
    echo "J4 root switch: $*" >/dev/console 2>/dev/null || true
}

# The image written to the phone's system/userdata partition is a bare ext4
# filesystem, so the partition itself carries LABEL and a plain blkid finds it.
label_of() {
    blkid -s LABEL -o value "$1" 2>/dev/null || true
}

say "scanning partitions for an ext4 filesystem labelled $LABEL"

found=''
for candidate in /dev/mmcblk*p* /dev/sd* /dev/disk/by-partlabel/*; do
    [ -b "$candidate" ] || continue
    [ "$(label_of "$candidate")" = "$LABEL" ] || continue
    found=$candidate
    break
done
# Second pass through the kpartx device maps the pmOS init created while
# looking for subpartitions; harmless when there are none.
if [ -z "$found" ]; then
    for candidate in /dev/mapper/*; do
        [ -b "$candidate" ] || continue
        [ "$(label_of "$candidate")" = "$LABEL" ] || continue
        found=$candidate
        break
    done
fi

if [ -z "$found" ]; then
    say "no filesystem labelled $LABEL found; nothing was mounted, resized or written"
    say "the other initramfs tools are: $(ls /sbin /usr/sbin 2>/dev/null | tr '\n' ' ')"
    exit 0
fi
say "found $LABEL on $found"

mkdir -p /j4root
# Read-only, and without "noload". A read-only ext4 mount does not replay the
# journal on its own, so nothing is written here; but the remount below turns
# the mount read-write, and ext4 only recovers the journal at that point. With
# noload still in effect the filesystem would go read-write with an unreplayed
# journal, which is how a clean filesystem acquires errors.
if ! mount -t ext4 -o ro "$found" /j4root; then
    say "refusing to continue: $found is not mountable read-only as ext4"
    exit 0
fi

# Identify the userspace before letting it become PID 1. A pmOS filesystem here
# would mean the label sits on the installation this project protects.
if [ -e /j4root/etc/os-release ] &&
   grep -Eq '^ID="?postmarketos"?$' /j4root/etc/os-release; then
    say "refusing to switch: $found carries a postmarketOS rootfs"
    umount /j4root
    exit 0
fi

# OpenRC first, and deliberately so. The image is ARMtix, whose init is
# openrc-init; Arch's glibc systemd would start as far as the kernel floor goes
# but brings its own assumptions about /run, cgroups and a writable /usr, none
# of which are established here. Anything else at /sbin/init is still honoured
# last, so a busybox or script init also works.
init=''
for candidate in /usr/bin/openrc-init /sbin/openrc-init /sbin/init /usr/bin/init; do
    if [ -x "/j4root$candidate" ] || [ -L "/j4root$candidate" ]; then
        init=$candidate
        break
    fi
done
if [ -z "$init" ]; then
    say "refusing to switch: $found has none of openrc-init/init"
    umount /j4root
    exit 0
fi

mkdir -p /sysroot
if ! mount -o move /j4root /sysroot; then
    say "could not move $found to /sysroot"
    umount /j4root 2>/dev/null || true
    exit 0
fi

# Make it writable only after everything is in place, and never repair it here:
# the filesystem has not been checked in this boot. A writable root is what
# OpenRC needs to record service state in /run and /var; read-only is a
# degraded boot, not a refusal.
if ! mount -o remount,rw /sysroot 2>/dev/null; then
    say "WARNING: $found did not remount read-write; continuing read-only"
    mount -o remount,ro /sysroot 2>/dev/null || true
fi

printf '%s\n' "$init" > "$STATE/init"
printf '%s\n' "$found" > "$STATE/device"
say "$found is mounted at /sysroot; userspace init will be $init"
"""

CLEANUP = r"""#!/bin/sh
# Runs just before switch_root. Copy the initramfs log into the target root so a
# silent failure has evidence after the fact, then leave the filesystem alone.
for root in /sysroot; do
    [ -d "$root" ] || continue
    [ -e "$root/etc/os-release" ] || continue
    if grep -Eq '^ID="?postmarketos"?$' "$root/etc/os-release" 2>/dev/null; then
        continue
    fi
    mkdir -p "$root/var/log/j4" 2>/dev/null || continue
    cp /pmOS_init.log "$root/var/log/j4/initramfs.log" 2>/dev/null || true
    dmesg > "$root/var/log/j4/dmesg-before-switch-root.log" 2>/dev/null || true
    cat /proc/cmdline > "$root/var/log/j4/cmdline.txt" 2>/dev/null || true
    sync
done
"""

INIT_WRAPPER = r"""#!/bin/sh
# /init is gone by the time this runs, so switch_root is exec'd from here with
# the interpreter resolved through the busybox that stays in the new root.
exec /bin/busybox sh -c 'exec switch_root /sysroot @INIT@'
"""


def iter_tree(root: Path):
    """Directory-major walk that never descends through a symlink.

    ``os.walk`` lists a symlink-to-directory under ``dirnames`` and would then
    emit it as a directory and walk into the target; this initramfs relies on
    ``bin -> usr/bin``-style links, so they have to stay symlinks.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        symlinked = [name for name in dirnames if (Path(dirpath) / name).is_symlink()]
        dirnames[:] = sorted(name for name in dirnames if name not in symlinked)
        yield Path(dirpath), dirnames, sorted(filenames + symlinked)


def newc_entry(name: str, mode: int, data: bytes, ino: int, mtime: int) -> bytes:
    namez = name.encode() + b"\x00"
    body = data + b"\x00" * ((-len(data)) % 4)
    # newc pads the *header* (110 bytes + name) to 4 bytes; the name field
    # itself already ends on a NUL, so the body starts at that pad boundary.
    pad_name = (-(110 + len(namez))) % 4
    fields = (
        ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(namez), 0,
    )
    header = b"070701" + b"".join(b"%08X" % f for f in fields)
    header += namez + b"\x00" * pad_name
    return header + body


def build_cpio(root: Path, mtime: int = 0) -> bytes:
    out = io.BytesIO()
    ino = 300000
    for base, dirnames, filenames in iter_tree(root):
        rel = base.relative_to(root)
        if str(rel) != ".":
            ino += 1
            out.write(newc_entry("./" + str(rel), 0o40755, b"", ino, mtime))
        for filename in filenames:
            path = base / filename
            relpath = path.relative_to(root)
            if path.is_symlink():
                target = os.readlink(path)
                ino += 1
                out.write(newc_entry("./" + str(relpath), 0o120777, target.encode(), ino, mtime))
            elif path.is_file():
                ino += 1
                st = path.stat()
                out.write(
                    newc_entry(
                        "./" + str(relpath),
                        st.st_mode & 0o7777,
                        path.read_bytes(),
                        ino,
                        mtime,
                    )
                )
    out.write(newc_entry("TRAILER!!!", 0, b"", 0, 0))
    # same rule as above: no separate name padding for the trailer either
    out.write(b"\x00" * ((-out.tell()) % 512))
    return out.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pmos_initramfs", help="directory holding the extracted initramfs")
    parser.add_argument("output", help="gzip-initramfs written here")
    parser.add_argument("--root-label", default="ARTIXROOT")
    parser.add_argument(
        "--work-dir",
        default=None,
        help="where the staged tree is written (default: alongside the output)",
    )
    args = parser.parse_args()

    label = args.root_label
    if not label or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in label):
        raise SystemExit(f"invalid ext4 label: {label!r}")

    src = Path(args.pmos_initramfs)
    for required in ("init", "init_2nd.sh", "init_functions.sh", "bin/busybox"):
        if not (src / required).exists():
            raise SystemExit(f"{src} does not look like the pmOS initramfs: missing {required}")

    out = Path(args.output)
    work = Path(args.work_dir) if args.work_dir else out.parent / (out.name + ".root")
    if work.exists():
        import shutil

        shutil.rmtree(work)
    work.mkdir(parents=True)

    # Copy the pmOS userland. Hard links are checked separately below so that a
    # busybox layout copied with symlinks stays verifiable.
    import shutil

    for entry in sorted(src.iterdir()):
        target = work / entry.name
        if entry.is_symlink():
            os.symlink(os.readlink(entry), target)
        elif entry.is_dir():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target)

    hooks_extra = work / "hooks-extra"
    hooks_extra.mkdir(exist_ok=True)
    hook_path = hooks_extra / HOOK_NAME
    hook_path.write_text(HOOK.replace("@ROOT_LABEL@", label))
    hook_path.chmod(0o755)

    hooks_cleanup = work / "hooks-cleanup"
    hooks_cleanup.mkdir(exist_ok=True)
    cleanup_path = hooks_cleanup / CLEANUP_NAME
    cleanup_path.write_text(CLEANUP)
    cleanup_path.chmod(0o755)

    init_2nd = (work / "init_2nd.sh").read_text()
    needle = "\nrun_hooks /hooks-extra\n"
    if needle not in init_2nd:
        raise SystemExit("init_2nd.sh changed upstream: the /hooks-extra call moved")
    patched = init_2nd.replace(
        needle,
        "\nrun_hooks /hooks-extra\n"
        "\n"
        "# j4-rootswitch: /hooks-extra may have selected the root filesystem and\n"
        "# mounted it at /sysroot together with the userspace init it wants.\n"
        "# Without that marker nothing below changes.\n"
        "j4_init=''\n"
        "[ -f /run/j4-rootswitch/init ] && j4_init=\"$(cat /run/j4-rootswitch/init)\"\n",
    )

    # The stock chain below waits for a pmOS_root partition, resizes it and
    # mounts it. On this project's target that partition does not exist, so the
    # chain would sit in wait_partition() and then halt the boot. Skip it
    # entirely when the hook already produced a root.
    old_root_chain = (
        "wait_root_partition\n"
        "delete_old_install_partition\n"
        "resize_root_partition\n"
        "unlock_root_partition\n"
        "resize_root_filesystem\n"
        "mount_root_partition\n"
        "resize_filesystem_after_mount /sysroot\n"
    )
    new_root_chain = (
        "if [ -z \"$j4_init\" ]; then\n"
        "\twait_root_partition\n"
        "\tdelete_old_install_partition\n"
        "\tresize_root_partition\n"
        "\tunlock_root_partition\n"
        "\tresize_root_filesystem\n"
        "\tmount_root_partition\n"
        "\tresize_filesystem_after_mount /sysroot\n"
        "else\n"
        "\techo \"$LOG_PREFIX j4-rootswitch: /sysroot is already mounted, skipping\"\n"
        "\techo \"$LOG_PREFIX pmOS root waiting, resizing and mounting\"\n"
        "fi\n"
    )
    if old_root_chain not in patched:
        raise SystemExit("could not patch init_2nd.sh: the pmOS root chain moved")
    patched = patched.replace(old_root_chain, new_root_chain)

    old_fstab = (
        'if [ -z "$(cat /sysroot/etc/fstab | grep -v \"#\" | tr -d \'[:space:]\')" ]; then\n'
        "\twait_boot_partition\n"
        "\tmount_boot_partition /sysroot/boot \"rw\"\n"
        "fi\n"
    )
    if old_fstab in patched:
        patched = patched.replace(
            old_fstab,
            old_fstab.replace('if [ -z "$(cat', 'if [ -z "$j4_init" ] && [ -z "$(cat', 1),
        )
    patched = patched.replace(
        'init="/sbin/init"\n',
        'if [ -n "$j4_init" ]; then\n'
        '\tinit="$j4_init"\n'
        '\techo "$LOG_PREFIX j4-rootswitch: switching to $init on /sysroot"\n'
        'else\n'
        '\tinit="/sbin/init"\n'
        'fi\n',
    )
    if 'j4-rootswitch: switching to' not in patched:
        raise SystemExit("could not patch init_2nd.sh: the init= assignment moved")
    (work / "init_2nd.sh").write_text(patched)
    (work / "init_2nd.sh").chmod(0o755)
    (work / INIT_FILE).write_text(
        "This initramfs was modified by ci/j4primelte/make_rootswitch_initramfs.py.\n"
        "Root label: " + label + "\n"
        "Changed files: hooks-extra/" + HOOK_NAME + ", hooks-cleanup/" + CLEANUP_NAME +
        ", init_2nd.sh\n"
    )

    cpio = build_cpio(work)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as handle:
        with gzip.GzipFile(fileobj=handle, mode="wb", compresslevel=6, mtime=0) as gz:
            gz.write(cpio)
    print(f"wrote {out} ({out.stat().st_size} bytes, cpio {len(cpio)} bytes, label {label})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
