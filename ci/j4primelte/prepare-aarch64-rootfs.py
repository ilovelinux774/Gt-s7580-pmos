#!/usr/bin/env python3
"""Convert a pmbootstrap AArch64 rootfs tree into a J4 USB-debug rootfs.

This only edits the supplied staging tree. It never mounts or writes a phone,
BOOT, /vendor, or any host root filesystem. The kernel arrives separately in
BOOT; this rootfs deliberately contains no unrelated QEMU kernel/modules.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import struct
import sys

HERE = Path(__file__).resolve().parent
OVERLAY = HERE / "aarch64-rootfs"
KERNEL_RELEASE = "3.18.120-j4-medusa64-gpufw-debug+"
ROOT_UUID = "86e7262b-b628-4b89-8a6e-e96ef9b1aab6"
REQUIRED_PACKAGES = {
    "postmarketos-base", "dropbear", "busybox-extras", "iproute2", "iptables-legacy"
}
FORBIDDEN_PACKAGES = {
    "device-qemu-aarch64", "device-qemu-aarch64-kernel-stable",
    "linux-stable", "postmarketos-qemu-common", "systemd-boot"
}


class RootfsError(Exception):
    pass


def installed_packages(root: Path) -> set[str]:
    db = root / "usr/lib/apk/db/installed"
    if not db.is_file():
        raise RootfsError("APK installed-package database is missing")
    result = set()
    for block in db.read_text(errors="replace").split("\n\n"):
        for line in block.splitlines():
            if line.startswith("P:"):
                result.add(line[2:])
                break
    return result


def verify_aarch64_busybox(root: Path) -> None:
    binary = root / "bin/busybox"
    if not binary.is_file():
        raise RootfsError("/bin/busybox is missing")
    data = binary.read_bytes()[:20]
    if len(data) < 20 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise RootfsError("/bin/busybox is not a little-endian ELF64 executable")
    machine = struct.unpack_from("<H", data, 18)[0]
    if machine != 183:
        raise RootfsError(f"/bin/busybox ELF machine is {machine}, expected AArch64 (183)")
    if not (root / "lib/ld-musl-aarch64.so.1").exists():
        raise RootfsError("AArch64 musl interpreter /lib/ld-musl-aarch64.so.1 is missing")


def copy_overlay(src: Path, dst: Path) -> None:
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            copy_overlay(child, dst / child.name)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)
        if src.read_bytes().startswith(b"#!"):
            dst.chmod(0o755)


def apply(root: Path) -> None:
    root = root.resolve(strict=True)
    if root == Path("/") or not root.is_dir():
        raise RootfsError("Refusing to operate on / or a non-directory")
    if not (root / "etc/alpine-release").is_file() or not (root / "sbin/init").exists():
        raise RootfsError("Input does not look like an Alpine/postmarketOS rootfs tree")
    verify_aarch64_busybox(root)

    packages = installed_packages(root)
    missing = sorted(REQUIRED_PACKAGES - packages)
    if missing:
        raise RootfsError("Required aarch64 packages are missing: " + ", ".join(missing))
    present = sorted(FORBIDDEN_PACKAGES & packages)
    if present:
        raise RootfsError("Remove generic QEMU/kernel packages before customization: " + ", ".join(present))

    deviceinfo_src = OVERLAY / "deviceinfo"
    if not deviceinfo_src.is_file():
        raise RootfsError("J4 AArch64 deviceinfo overlay is missing")
    copy_overlay(deviceinfo_src, root / "usr/share/deviceinfo/deviceinfo")
    deviceinfo = (root / "usr/share/deviceinfo/deviceinfo").read_text()
    for expected in ('deviceinfo_codename="samsung-j4primelte"', 'deviceinfo_arch="aarch64"'):
        if expected not in deviceinfo:
            raise RootfsError("J4 deviceinfo overlay failed validation")

    # Keep the USB-only debug service, not the QEMU console/SSH setup.
    for rel in (
        "etc/init.d/j4-usb-debug",
        "usr/local/sbin/j4-usb-debug",
        "usr/local/sbin/j4-collect-logs",
    ):
        src = HERE / "rootfs" / rel
        if not src.is_file():
            raise RootfsError(f"Missing tested J4 debug helper: {src}")
        copy_overlay(src, root / rel)

    for levels in (root / "etc/runlevels").glob("*"):
        if not levels.is_dir():
            continue
        for service in ("sshd", "dropbear", "telnetd", "j4-wifi-ssh"):
            link = levels / service
            if link.exists() or link.is_symlink():
                link.unlink()
    boot = root / "etc/runlevels/boot"
    boot.mkdir(parents=True, exist_ok=True)
    service_link = boot / "j4-usb-debug"
    if service_link.exists() or service_link.is_symlink():
        service_link.unlink()
    service_link.symlink_to("../../init.d/j4-usb-debug")

    hostname = root / "etc/hostname"
    hostname.write_text("j4-aarch64\n")
    fstab = root / "etc/fstab"
    rows = [line for line in (fstab.read_text().splitlines() if fstab.exists() else [])
            if not line.strip().startswith("UUID=") and not line.strip().startswith("/dev/")]
    rows.insert(0, f"UUID={ROOT_UUID} / ext4 defaults,noatime 0 0")
    fstab.write_text("\n".join(rows) + "\n")

    inittab = root / "etc/inittab"
    if inittab.exists():
        rows = [line for line in inittab.read_text().splitlines()
                if not line.startswith(("ttyAMA0:", "ttyS0:"))]
        if not any(line.startswith("ttyMSM0:") for line in rows):
            rows.append("ttyMSM0::respawn:/sbin/getty -L 115200 ttyMSM0 vt100")
        inittab.write_text("\n".join(rows) + "\n")

    # This is a USB-debug image: the listener explicitly binds only to the
    # RNDIS address and its helper drops non-USB ingress to ports 22/23.
    shadow = root / "etc/shadow"
    lines = shadow.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        fields = line.split(":")
        if fields[0] == "root":
            if len(fields) < 9:
                raise RootfsError("Unexpected /etc/shadow root record")
            fields[1] = ""
            fields[7] = ""
            lines[i] = ":".join(fields)
            found = True
            break
    if not found:
        raise RootfsError("root account is missing from /etc/shadow")
    shadow.write_text("\n".join(lines) + "\n")
    shadow.chmod(0o640)

    rcconf = root / "etc/rc.conf"
    text = rcconf.read_text() if rcconf.exists() else ""
    if 'rc_logger="YES"' not in text:
        rcconf.write_text(text.rstrip() + '\n\n# Keep first-boot diagnostics persistent.\nrc_logger="YES"\n')
    (root / "var/log/j4").mkdir(parents=True, exist_ok=True)

    # Remove foreign QEMU boot files. No kernel image is placed in this rootfs;
    # the user's already-tested Medusa64 BOOT is separate.
    boot_dir = root / "boot"
    if boot_dir.is_dir():
        for child in boot_dir.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    qemu_stub = root / "usr/bin/qemu-aarch64-static"
    if qemu_stub.exists() or qemu_stub.is_symlink():
        qemu_stub.unlink()
    marker = root / "in-pmbootstrap"
    if marker.exists():
        marker.unlink()

    # The generic kernel package has already been removed. Never leave its
    # modules in a rootfs for a different kernel release.
    modules = root / "lib/modules"
    if modules.is_dir():
        for entry in modules.iterdir():
            if entry.name != KERNEL_RELEASE:
                shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        if not any(modules.iterdir()):
            modules.rmdir()

    # Filesystem mountpoints are created empty for the device kernel/initramfs.
    for rel in ("dev", "proc", "sys", "run"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    tmp = root / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    tmp.chmod(0o1777)

    # Re-run critical validations after all edits.
    verify_aarch64_busybox(root)
    if not service_link.is_symlink() or service_link.resolve() != (root / "etc/init.d/j4-usb-debug").resolve():
        raise RootfsError("USB debug service is not enabled in the boot runlevel")
    print("AARCH64_ROOTFS_PREPARED", root)
    print("arch=aarch64 kernel=Medusa64 root_uuid=" + ROOT_UUID)
    print("usb_debug=172.16.42.1:22,23; no Wi-Fi/audio/Bluetooth desktop services enabled")
    print("rootfs modules are included only if they match " + KERNEL_RELEASE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("rootfs_tree", help="pmbootstrap rootfs staging directory")
    args = parser.parse_args()
    try:
        apply(Path(args.rootfs_tree))
    except (RootfsError, OSError) as exc:
        print("ERROR:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
