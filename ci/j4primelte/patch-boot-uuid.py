#!/usr/bin/env python3
"""Point a J4+ boot image at a different root filesystem.

A boot.img finds its rootfs by pmos_root_uuid in the boot header cmdline, and
every build creates a fresh ext4 with a new UUID. When a phone already has a
working rootfs on SYSTEM and only the boot image was replaced, the result is
the initramfs debug shell ("failed to mount subpartitions"), even though the
rootfs is fine.

This tool rewrites that one UUID in place, so a boot image can be pointed at
the rootfs that is already on the phone:

    python3 patch-boot-uuid.py boot.img 86e7262b-b628-4b89-8a6e-e96ef9b1aab6
    python3 patch-boot-uuid.py --show boot.img

Only cmdline header bytes change. The kernel and ramdisk payloads stay
byte-identical, and that is asserted before anything is written.
"""
import argparse
import re
import struct
import sys
from pathlib import Path

UUID_RE = re.compile(rb'pmos_root_uuid=([0-9a-fA-F-]{36})')
UUID_SHAPE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
                        r'-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
CMDLINE_SLOTS = ((64, 576), (608, 1632))


class Error(Exception):
    pass


def header_fields(data):
    if bytes(data[:8]) != b'ANDROID!':
        raise Error('Not an Android boot image')
    fields = struct.unpack_from('<10I', data, 8)
    version, page = fields[9], fields[7]
    if version != 0:
        raise Error('Unsupported boot header version: %s' % version)
    return page


def cmdline(data):
    return b''.join(bytes(data[start:end]).split(b'\0', 1)[0]
                    for start, end in CMDLINE_SLOTS)


def current_uuid(data):
    match = UUID_RE.search(cmdline(data))
    if not match:
        raise Error('No pmos_root_uuid in the boot cmdline')
    return match.group(1).decode()


def patch(data, old, new):
    if len(old) != len(new):
        raise Error('UUID lengths differ; the header must not shift')
    replacements = 0
    for start, end in CMDLINE_SLOTS:
        chunk = bytes(data[start:end])
        if old.encode() not in chunk:
            continue
        chunk = chunk.replace(old.encode(), new.encode())
        if len(chunk) != end - start:
            raise Error('Patched cmdline changed length')
        data[start:end] = chunk
        replacements += chunk.count(new.encode())
    return replacements


def show(path):
    data = bytearray(Path(path).read_bytes())
    header_fields(data)
    print('cmdline: %s' % cmdline(data).decode())
    print('root uuid: %s' % current_uuid(data))


def apply(path, new):
    if not UUID_SHAPE.match(new):
        raise Error('%r is not a UUID' % new)
    path = Path(path)
    data = bytearray(path.read_bytes())
    header_fields(data)
    payload = bytes(data[2048:])
    old = current_uuid(data)
    if old.lower() == new.lower():
        print('ROOT_UUID_OK: already %s' % new)
        return
    changed = patch(data, old, new)
    if not changed:
        raise Error('The root UUID was not found in the boot cmdline')
    if bytes(data[2048:]) != payload:
        raise Error('Kernel/ramdisk payload changed; refusing to write')
    path.write_bytes(data)
    check = bytearray(path.read_bytes())
    if current_uuid(check) != new:
        raise Error('Verification failed after writing')
    if bytes(check[2048:]) != payload:
        raise Error('Payload changed after writing')
    print('ROOT_UUID_OK: %s -> %s (%d cmdline slot(s))' % (old, new, changed))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('image', help='boot.img to patch')
    parser.add_argument('uuid', nargs='?', help='new pmos_root_uuid')
    parser.add_argument('--show', action='store_true',
                        help='print the current cmdline and root UUID')
    args = parser.parse_args()
    try:
        if args.show or not args.uuid:
            show(args.image)
        else:
            apply(args.image, args.uuid)
    except Error as exc:
        print('ERROR: %s' % exc)
        sys.exit(1)


if __name__ == '__main__':
    main()
