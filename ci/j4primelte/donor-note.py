#!/usr/bin/env python3
"""Validate and record the legacy pmOS boot image that donates the initramfs."""
from pathlib import Path
import argparse
import hashlib
import re
import struct
import sys

UUID = re.compile(rb'pmos_root_uuid=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-'
                  rb'[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})')


def inspect(data: bytes) -> dict:
    if len(data) < 1632 or data[:8] != b'ANDROID!':
        raise ValueError('donor has no complete Android boot image header')
    ksize, kaddr, rsize, raddr, ssize, saddr, tags, page, version, osver = \
        struct.unpack_from('<10I', data, 8)
    if version != 0 or page not in (2048, 4096):
        raise ValueError(f'donor uses header v{version} with page {page}, not v0')
    if not ksize or not rsize:
        raise ValueError('donor must contain a nonempty kernel and initramfs')
    align = lambda n: (n + page - 1) // page * page
    total = page + align(ksize) + align(rsize) + align(ssize)
    if len(data) < total:
        raise ValueError('donor boot image or its payload padding is truncated')
    start = page
    kernel = data[start:start + ksize]
    start += align(ksize)
    ramdisk = data[start:start + rsize]
    start += align(rsize)
    second = data[start:start + ssize]
    digest = hashlib.sha1()
    for blob in (kernel, ramdisk, second):
        digest.update(blob)
        digest.update(struct.pack('<I', len(blob)))
    if data[576:608] != digest.digest().ljust(32, b'\0'):
        raise ValueError('donor Android v0 payload/size checksum mismatch')
    cmdline = data[64:576].split(b'\0', 1)[0] + data[608:1632].split(b'\0', 1)[0]
    roots = [part for part in cmdline.split() if part.startswith(b'pmos_root_uuid=')]
    if len(roots) != 1 or not UUID.fullmatch(roots[0]):
        raise ValueError('donor needs exactly one valid pmos_root_uuid')
    return {
        'kernel_bytes': ksize, 'kernel_addr': kaddr,
        'ramdisk_bytes': rsize, 'ramdisk_addr': raddr,
        'second_bytes': ssize, 'page_size': page, 'tags_addr': tags,
        'name': data[48:64].split(b'\0', 1)[0].decode('ascii', 'replace'),
        'root_uuid': UUID.fullmatch(roots[0]).group(1).decode('ascii').lower(),
        'sha256': hashlib.sha256(data).hexdigest(),
        'ramdisk_sha256': hashlib.sha256(ramdisk).hexdigest(),
    }


def note(data: bytes) -> str:
    try:
        d = inspect(data)
    except (ValueError, struct.error) as exc:
        raise SystemExit(str(exc)) from exc
    return (f'donor boot.img: kernel {d["kernel_bytes"]} B @0x{d["kernel_addr"]:08x}, '
            f'ramdisk {d["ramdisk_bytes"]} B @0x{d["ramdisk_addr"]:08x}, '
            f'second {d["second_bytes"]} B, page {d["page_size"]}, '
            f'tags 0x{d["tags_addr"]:08x}, header v0, name {d["name"] or "(unset)"}\n'
            f'donor_sha256={d["sha256"]}\nramdisk_sha256={d["ramdisk_sha256"]}\n'
            f'pmos_root_uuid={d["root_uuid"]}\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('output', nargs='?', type=Path)
    parser.add_argument('--expect-sha256', default='')
    parser.add_argument('--expect-root-uuid', default='')
    args = parser.parse_args(argv)
    data = args.image.read_bytes()
    try:
        d = inspect(data)
    except (ValueError, struct.error) as exc:
        raise SystemExit(str(exc)) from exc
    if args.expect_sha256 and d['sha256'] != args.expect_sha256.lower():
        raise SystemExit('donor SHA-256 differs from the pinned known-good release')
    if args.expect_root_uuid and d['root_uuid'] != args.expect_root_uuid.lower():
        raise SystemExit('donor rootfs UUID differs from the installed rootfs')
    text = note(data)
    print(text, end='')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
