#!/usr/bin/env python3
"""Wrap a zero-offset ARM64 Image for the J415F Samsung loader.

The J415FXXU6BVJ1 aboot ignores Android boot header addresses: for an ARM64
Image, it uses RAM base + 0x80000, while current mainline emits text_offset=0
and requires 2 MiB alignment. This wrapper is itself entered at base+0x80000
and branches, without touching registers, to the original unmodified Image at
base+0x200000. It is a positioning shim, not a new kernel or bootloader.

The input gzip must contain exactly one ARM64 Image with text_offset=0. Output is
one plain deterministic gzip stream; append the DTB with mainline-payload.py.
This addresses the documented alignment contract, not all possible boot issues.
"""
import argparse
import gzip
import struct
import zlib
from pathlib import Path

WRAPPER_TEXT_OFFSET = 0x80000
IMAGE_OFFSET = 0x180000
BRANCH_OFFSET = 0x40
MARKER_OFFSET = 0x44
MARKER = b'J4A64-SHIM-v1\0'
ARM64_NOP = 0xd503201f
ARM64_B = 0x14000000


def branch(source: int, target: int) -> int:
    delta = target - source
    if delta % 4 or not -(1 << 27) <= delta < (1 << 27):
        raise ValueError('ARM64 direct branch is out of range or misaligned')
    return ARM64_B | ((delta // 4) & 0x03ffffff)


def wrap(raw: bytes) -> bytes:
    if len(raw) < 64 or raw[56:60] != b'ARM\x64':
        raise ValueError('input is not an ARM64 Linux Image')
    offset, size, flags = struct.unpack_from('<QQQ', raw, 8)
    if offset != 0:
        raise ValueError('expected the pinned mainline Image with text_offset=0')
    if size < len(raw) or size == 0 or size > 0xffffffff - IMAGE_OFFSET:
        raise ValueError('invalid ARM64 effective image size')
    header = bytearray(IMAGE_OFFSET)
    struct.pack_into('<IIQQQ', header, 0, ARM64_NOP, branch(4, BRANCH_OFFSET),
                     WRAPPER_TEXT_OFFSET, IMAGE_OFFSET + size, flags)
    header[56:60] = b'ARM\x64'
    struct.pack_into('<I', header, BRANCH_OFFSET, branch(BRANCH_OFFSET, IMAGE_OFFSET))
    header[MARKER_OFFSET:MARKER_OFFSET + len(MARKER)] = MARKER
    return bytes(header) + raw


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True, help='original Image.gz')
    parser.add_argument('--out', type=Path, required=True, help='wrapped Image.gz')
    args = parser.parse_args(argv)
    try:
        data = args.image.read_bytes()
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = dec.decompress(data) + dec.flush()
        if not dec.eof or dec.unused_data:
            raise ValueError('input must be one complete gzip stream with no trailing data')
        wrapped = wrap(raw)
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise SystemExit(f'cannot make J4 ARM64 wrapper: {exc}') from exc
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(gzip.compress(wrapped, compresslevel=9, mtime=0))
    print(f'J4 ARM64 shim: entry +0x{WRAPPER_TEXT_OFFSET:x}, original Image '
          f'+0x{IMAGE_OFFSET:x}, {len(raw)} original bytes unchanged, '
          f'effective wrapped size 0x{struct.unpack_from("<Q", wrapped, 16)[0]:x}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
