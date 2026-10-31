#!/usr/bin/env python3
"""Independently check a finished J415F HLOS diagnostic BOOT image.

This validates exact payload relationships, not hardware or the TrustZone
call. Inputs must have passed the normal mainline checks before this verifier.
"""
import argparse
import gzip
import hashlib
import importlib.util
from pathlib import Path
import struct
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pack = load('pack', ROOT / 'lk2nd-image.py')
wrap = load('diag_wrap', HERE / 'diag-wrap.py')
dtb = load('diag_dtb', HERE / 'diag-dtb.py')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check(a):
    donor, normal_inner, image = (p.read_bytes() for p in (a.donor, a.normal_inner, a.image))
    lk, outer = a.lk.read_bytes(), a.outer_dtb.read_bytes()
    base_tree = a.base_dtb.read_bytes()
    original = gzip.decompress(a.original.read_bytes())
    pack.require(digest(donor) == pack.DONOR_SHA256, 'donor SHA-256 mismatch')
    pack.check_outer_dtb(a.outer_dtb)
    pack.require(len(image) <= pack.PARTITION_SIZE and len(image) > pack.OFFSET, 'image size/split')
    inner = image[pack.OFFSET:]
    report = pack.verify_combined(image, donor, lk, outer, inner)
    normal, normal_kernel, normal_tree = pack.check_inner(normal_inner, donor)
    candidate, kernel, tree = pack.check_inner(inner, donor)
    pack.require(normal_tree == base_tree, 'normal debug DTB does not match verified base')
    pack.require(normal_kernel == wrap.load_original_wrap().wrap(original), 'normal kernel differs from original plus standard wrapper')
    dtb.validate(base_tree, tree)
    with tempfile.TemporaryDirectory(prefix='j4-diag-verify-') as tmp:
        stub = wrap.make_stub(HERE / 'entry-pmsg.S', Path(tmp))
    pack.require(kernel == wrap.wrap(original, stub), 'candidate differs from checked diagnostic wrapper')
    pack.require(kernel[wrap.INNER_OFFSET:] == original, 'original kernel not byte-identical')
    pack.require(candidate['ramdisk'] == normal['ramdisk'] == pack.parse_boot(donor, require_ramdisk=True)['ramdisk'], 'initramfs changed')
    pack.require(candidate['cmdline'] == normal['cmdline'], 'debug command line changed')
    # Apart from the kernel length and Android image-ID, the whole header page
    # must be identical to the already checked M1 debug image.
    aheader, bheader = bytearray(inner[:2048]), bytearray(normal_inner[:2048])
    for start, end in ((8, 12), (576, 608)):
        aheader[start:end] = bheader[start:end] = bytes(end - start)
    pack.require(aheader == bheader, 'inner Android header changed unexpectedly')
    pack.require(0x80008000 + len(lk) < 0x80080000, 'initial lk2nd overlaps kernel load')
    pack.require(0x80055000 + 80 <= 0x80056000 and 0x80056000 + 72 < 0x80080000,
                 'HLOS scratch conflicts with kernel or itself')
    print('PASS: combined Android headers, 1 MiB split, donor SHA and size')
    print('PASS: unchanged donor initramfs, M1 debug bootargs and original mainline Image')
    print('PASS: exact assembled diagnostic wrapper and constrained DTB semantic diff')
    print('PASS: constructed HLOS scratch layout has no modeled kernel overlap')
    for k, v in report.items():
        print(f'{k}={v}')
    print(f'lk2nd_sha256={digest(lk)}')
    print(f'original_kernel_sha256={digest(original)}')
    print('LIMIT: real firmware behavior, pmsg survival, Linux execution and p47 effects are unproven')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ('donor', 'normal-inner', 'image', 'lk', 'outer-dtb', 'base-dtb', 'original'):
        p.add_argument('--' + flag, type=Path, required=True)
    check(p.parse_args())


if __name__ == '__main__':
    main()
