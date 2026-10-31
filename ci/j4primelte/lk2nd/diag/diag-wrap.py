#!/usr/bin/env python3
"""Insert the experimental AArch64 pmsg-stage marker into the J4 M1 shim.

The original Image and its payload remain byte-for-byte intact. Writes to the
reserved pmsg RAM quarter are diagnostic, hardware-untested and not a fix.
"""
import argparse
import gzip
import importlib.util
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[2]
ENTRY_OFFSET = 0x40
INNER_OFFSET = 0x180000
EXPECTED_MESSAGE = b'J4DIAG-HLOS-1 B: AArch64 wrapper reached; Linux unproven\n'


def load_original_wrap():
    spec = importlib.util.spec_from_file_location('normal_wrap', ROOT / 'mainline-wrap-arm64.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_stub(source, outdir):
    obj, elf, binary = (outdir / ('entry.' + suffix) for suffix in ('o', 'elf', 'bin'))
    subprocess.run(['aarch64-linux-gnu-as', '-o', str(obj), str(source)], check=True)
    subprocess.run(['aarch64-linux-gnu-ld', '-Ttext=0x40', '--defsym=j4_original_image=0x180000',
                    '-e', 'j4_diag_aarch64_entry', '-o', str(elf), str(obj)], check=True)
    subprocess.run(['aarch64-linux-gnu-objcopy', '-O', 'binary', '-j', '.text', str(elf), str(binary)], check=True)
    stub = binary.read_bytes()
    if not 0 < len(stub) <= 4096 - ENTRY_OFFSET or stub.count(EXPECTED_MESSAGE) != 1:
        raise ValueError('unexpected diagnostic marker code size or unique message')
    return stub


def wrap(raw, stub):
    original = load_original_wrap().wrap(raw)
    out = bytearray(original)
    out[ENTRY_OFFSET:ENTRY_OFFSET + len(stub)] = stub
    if out[:ENTRY_OFFSET] != original[:ENTRY_OFFSET] or out[ENTRY_OFFSET + len(stub):] != original[ENTRY_OFFSET + len(stub):]:
        raise ValueError('diagnostic changed bytes outside wrapper code')
    if out[INNER_OFFSET:] != raw or out[56:60] != b'ARM\x64':
        raise ValueError('original ARM64 kernel changed')
    if struct.unpack_from('<Q', out, 8)[0] != 0x80000:
        raise ValueError('wrapper load offset changed')
    return bytes(out)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--image', type=Path, required=True)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    d = zlib.decompressobj(31)
    raw = d.decompress(a.image.read_bytes()) + d.flush()
    if not d.eof or d.unused_data:
        raise ValueError('input must be one complete gzip stream')
    with tempfile.TemporaryDirectory(prefix='j4-entry-') as temp:
        stub = make_stub(a.source, Path(temp))
    data = wrap(raw, stub)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    print(f'EXPERIMENTAL: {len(stub)}-byte stage-B marker inserted; original {len(raw)}-byte Image unchanged')


if __name__ == '__main__':
    main()
