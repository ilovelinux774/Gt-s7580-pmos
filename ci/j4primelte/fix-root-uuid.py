#!/usr/bin/env python3
"""Give every J4+ image the same root filesystem UUID.

pmbootstrap creates a fresh ext4 filesystem for every build, so the
pmos_root_uuid baked into boot.img changes on every run. Flashing a boot.img
and a rootfs.img from different runs then stops in the initramfs debug shell
with "failed to mount subpartitions", which looks like a bricked phone.

This helper rewrites the rootfs UUID to one fixed value and patches the boot
header cmdline to match. Only cmdline bytes in the header change: the kernel
and ramdisk payloads stay byte-identical, so this is safe for a v0 header.
"""
import argparse
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

# Stable across every build, so any boot.img matches any rootfs.img. This is
# the UUID already carried by the SYSTEM partition of the reference phone, so a
# boot image from any run also boots the rootfs that is on that phone today.
FIXED_UUID = '86e7262b-b628-4b89-8a6e-e96ef9b1aab6'

UUID_RE = re.compile(rb'pmos_root_uuid=([0-9a-fA-F-]{36})')
CMDLINE_SLOTS = ((64, 576), (608, 1632))


class Error(Exception):
    pass


def extract_root_uuid(data):
    """Read pmos_root_uuid from the v0 boot header cmdline."""
    if bytes(data[:8]) != b'ANDROID!':
        raise Error('Not an Android boot image')
    version, = struct.unpack_from('<I', data, 8 + 9 * 4)
    if version != 0:
        raise Error('Unsupported boot header version: %s' % version)
    command = b''.join(bytes(data[start:end]).split(b'\0', 1)[0]
                       for start, end in CMDLINE_SLOTS)
    match = UUID_RE.search(command)
    if not match:
        raise Error('No pmos_root_uuid in the boot cmdline')
    return match.group(1).decode()


def patch_cmdline(data, old, new):
    """Replace the UUID inside the cmdline slots, in place and same length."""
    if len(old) != len(new):
        raise Error('UUID lengths differ; the header must not shift')
    replacements = 0
    for start, end in CMDLINE_SLOTS:
        chunk = bytes(data[start:end])
        count = chunk.count(old.encode())
        if not count:
            continue
        chunk = chunk.replace(old.encode(), new.encode())
        if len(chunk) != end - start:
            raise Error('Patched cmdline changed length')
        data[start:end] = chunk
        replacements += count
    return replacements


def set_filesystem_uuid(image, uuid):
    if not shutil.which('tune2fs'):
        raise Error('tune2fs is not available; install e2fsprogs')
    proc = subprocess.run(['tune2fs', '-U', uuid, str(image)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise Error('tune2fs failed: %s' % proc.stderr.strip())
    proc = subprocess.run(['tune2fs', '-l', str(image)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise Error('tune2fs -l failed: %s' % proc.stderr.strip())
    for line in proc.stdout.splitlines():
        if line.startswith('Filesystem UUID:'):
            current = line.split(':', 1)[1].strip().lower()
            if current != uuid.lower():
                raise Error('Rootfs UUID is %s, expected %s' % (current, uuid))
            return
    raise Error('Could not confirm the rootfs UUID')


def apply(folder):
    folder = Path(folder)
    boot = folder / 'boot.img'
    rootfs = folder / 'rootfs.img'
    for path in (boot, rootfs):
        if not path.is_file():
            raise Error('Missing image: %s' % path)
    data = bytearray(boot.read_bytes())
    payload_before = bytes(data[2048:])
    old = extract_root_uuid(data)
    if old.lower() == FIXED_UUID:
        set_filesystem_uuid(rootfs, FIXED_UUID)
        print('ROOT_UUID_OK: already %s' % FIXED_UUID)
        return
    changed = patch_cmdline(data, old, FIXED_UUID)
    if not changed:
        raise Error('The root UUID was not found in the boot cmdline')
    set_filesystem_uuid(rootfs, FIXED_UUID)
    if bytes(data[2048:]) != payload_before:
        raise Error('Kernel/ramdisk payload changed; refusing to write')
    boot.write_bytes(data)
    if extract_root_uuid(bytearray(boot.read_bytes())) != FIXED_UUID:
        raise Error('Boot cmdline does not carry the fixed UUID after writing')
    print('ROOT_UUID_OK: %s -> %s (%d cmdline slot(s))'
          % (old, FIXED_UUID, changed))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('folder', help='folder with boot.img and rootfs.img')
    args = parser.parse_args()
    try:
        apply(args.folder)
    except Error as exc:
        print('ERROR: %s' % exc)
        sys.exit(1)


if __name__ == '__main__':
    main()
