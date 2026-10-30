#!/usr/bin/env python3
"""Build an LK-compatible, 8-byte-aligned Image.gz + DTB payload.

Qualcomm LK variants commonly find an appended DTB at the exact position returned
by their gzip decompressor, not by searching past zero padding. Therefore alignment
bytes must be *inside* the gzip header rather than between gzip and DTB. The FNAME
field is understood by both standard gzip and the reference Qualcomm LK inflater.
Actual Samsung bootloader behavior remains unverified until a device test.
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
        raise ValueError(f'unsupported gzip flags 0x{flags:02x}; LK supports FNAME only')
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


def align_gzip(image: bytes) -> bytes:
    """Align the complete gzip stream by inserting an optional FNAME field."""
    original = complete_gzip(image)
    extra = (-len(image)) % 8
    if not extra:
        return image
    if image[3] != 0:
        raise ValueError('cannot add alignment to a gzip with existing optional fields')
    header = bytearray(image[:10])
    header[3] = 8  # FNAME, whose NUL terminator counts toward the alignment.
    filename = b'j' * (extra - 1) + b'\0'
    result = bytes(header) + filename + image[10:]
    if len(result) % 8 or complete_gzip(result) != original:
        raise ValueError('gzip alignment changed the decompressed kernel')
    return result


def combine(image: bytes, dtb: bytes) -> bytes:
    if len(dtb) < 40 or dtb[:4] != bytes.fromhex('d00dfeed'):
        raise ValueError('device tree has no valid FDT header')
    if struct.unpack_from('>I', dtb, 4)[0] != len(dtb):
        raise ValueError('device tree totalsize does not match its file length')
    image = align_gzip(image)
    return image + dtb


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
    aligned_size = len(payload) - len(dtb)
    print(f'{len(image)} B original gzip + {aligned_size - len(image)} B gzip '
          f'header alignment + {len(dtb)} B DTB; no gap after gzip')
    return 0


if __name__ == '__main__':
    sys.exit(main())
