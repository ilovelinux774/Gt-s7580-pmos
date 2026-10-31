#!/usr/bin/env python3
"""Build a plain Image.gz with the J4+ DTB appended directly at gzip EOF.

The public TeamWin j4primelte ARM64 prebuilt uses a plain gzip header (no FNAME)
and concatenates FDTs directly after the stream, even when the first FDT is not
8-byte-aligned. No zero padding is inserted after the gzip stream: reference
Qualcomm LK variants use the gzip end as the appended-DTB offset.

This format is grounded in a same-model reference, not a proof that every Samsung
bootloader accepts a new mainline DTB. Hardware testing is still necessary.
"""
import argparse
import struct
import sys
import zlib
from pathlib import Path


def complete_gzip(blob: bytes) -> bytes:
    if len(blob) < 18 or blob[:3] != b'\x1f\x8b\x08':
        raise ValueError('kernel is not a complete gzip stream')
    flags = blob[3]
    if flags not in (0, 8):
        raise ValueError(f'unsupported gzip flags 0x{flags:02x}')
    if flags == 8:
        end = blob.find(b'\0', 10, min(len(blob), 266))
        if end < 0:
            raise ValueError('gzip filename is missing or exceeds the LK limit')
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = dec.decompress(blob) + dec.flush()
    except zlib.error as exc:
        raise ValueError(f'invalid gzip stream: {exc}') from exc
    if not dec.eof or dec.unused_data:
        raise ValueError('gzip stream is truncated or has trailing bytes')
    return data


def plain_gzip(image: bytes) -> bytes:
    """Strip FNAME if present, preserving the compressed stream and payload.

    The failed M1 used FNAME solely to force alignment. A standard plain gzip
    matches the same-model ARM64 reference instead and needs no such field.
    """
    original = complete_gzip(image)
    if image[3] == 0:
        return image
    end = image.find(b'\0', 10, min(len(image), 266))
    # complete_gzip() has already verified that the terminator exists.
    header = bytearray(image[:10])
    header[3] = 0
    result = bytes(header) + image[end + 1:]
    if complete_gzip(result) != original:
        raise ValueError('removing gzip filename changed the kernel')
    return result


def combine(image: bytes, dtb: bytes) -> bytes:
    if len(dtb) < 40 or dtb[:4] != bytes.fromhex('d00dfeed'):
        raise ValueError('device tree has no valid FDT header')
    if struct.unpack_from('>I', dtb, 4)[0] != len(dtb):
        raise ValueError('device tree totalsize does not match its file length')
    return plain_gzip(image) + dtb


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, type=Path)
    parser.add_argument('--dtb', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    image, dtb = args.image.read_bytes(), args.dtb.read_bytes()
    try:
        payload = combine(image, dtb)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(payload)
    gzip_size = len(payload) - len(dtb)
    print(f'{gzip_size} B plain gzip + {len(dtb)} B DTB; '
          f'no gap after gzip, DTB offset mod 8 = {gzip_size % 8}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
