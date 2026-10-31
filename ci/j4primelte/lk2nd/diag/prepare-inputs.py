#!/usr/bin/env python3
"""Recover verified *inputs* from a device-test-FAILED lk2nd M1 image.

The old combined BOOT must never be offered as working or reflashed for this
procedure. Only its unchanged kernel, initramfs, and two DTBs are re-used to
make a newly patched HLOS diagnostic, with exact hashes verified here.
"""
import argparse
import gzip
import hashlib
import importlib.util
from pathlib import Path
import zlib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pack = load('pack', ROOT / 'lk2nd-image.py')
wrap = load('wrap', ROOT / 'mainline-wrap-arm64.py')
FAILED_SHA = '3d9edd580d500598c83fe4143a6efba83a7ce6f5d1116305e82e216fd1243513'
INNER_SHA = 'df2120f75b5c728a9dee50962aa6cdc98d482f00fb258f2958f1e22809912b15'
LK_SHA = 'f05900fddb0540d992e1a73e14b38f6f1f6a7e097081df6caa566385fbb370b3'
OUTER_DTB_SHA = '685e9c161ba0b2e88afb642e87676b081b0fcd20f1effed4758bc4740614d745'
INNER_DTB_SHA = '58b55cc4662867802401dd9f2a6eb569d2d0d03ef15cc30dee9ad4c88762531a'
IMAGE_SHA = 'e78f359e9a6e32d52e184eb0c671ae35451d68fb9f27943ab10cd32ea505508a'
CONFIG_SHA = '5fe238cbe76f3edea4abe4b4f1f1c7726f3e027e1171cd34308531a5ec0e4bf1'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require_hash(label, data, expected):
    if sha(data) != expected:
        raise ValueError(f'{label} SHA-256 does not match pinned input')


def prepare(failed, donor, target):
    require_hash('device-test-failed input', failed, FAILED_SHA)
    require_hash('#37 donor', donor, pack.DONOR_SHA256)
    outer = pack.parse_boot(failed, require_ramdisk=False)
    old_lk, outer_tree = pack.unpack_gzip_dtb(outer['kernel'])
    inner = failed[pack.OFFSET:]
    require_hash('embedded previous LK', old_lk, LK_SHA)
    require_hash('outer DTB', outer_tree, OUTER_DTB_SHA)
    require_hash('embedded previous debug image', inner, INNER_SHA)
    pack.verify_combined(failed, donor, old_lk, outer_tree, inner)
    parsed, wrapped, tree = pack.check_inner(inner, donor)
    require_hash('inner M1 DTB', tree, INNER_DTB_SHA)
    raw = wrapped[wrap.IMAGE_OFFSET:]
    require_hash('original mainline Image', raw, IMAGE_SHA)
    if wrapped != wrap.wrap(raw):
        raise ValueError('old wrapper does not match standard wrapper')
    a = raw.find(b'IKCFG_ST')
    b = raw.find(b'IKCFG_ED', a + 8)
    if a < 0 or b <= a:
        raise ValueError('kernel config is missing')
    config = gzip.decompress(raw[a + 8:b])
    require_hash('embedded kernel config', config, CONFIG_SHA)
    main = target / 'mainline'
    (main / 'boot').mkdir(parents=True, exist_ok=True)
    (main / 'lk2nd').mkdir(parents=True, exist_ok=True)
    (main / 'boot' / 'boot-m1-debug.img').write_bytes(inner)
    (main / 'm1-Image.gz-dtb').write_bytes(parsed['kernel'])
    (main / 'm1.dtb').write_bytes(tree)
    (main / 'lk2nd' / 'j4-outer.dtb').write_bytes(outer_tree)
    (main / 'm1-original-Image.gz').write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))
    (main / 'kernel-config-m1').write_bytes(config)
    print('PASS: previously device-failed image used solely as pinned input, not as an output')
    print('PASS: original kernel, donor initramfs and two DTBs extracted with exact SHA-256 checks')
    print(f'original_kernel_sha256={sha(raw)}')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--failed-image', type=Path, required=True)
    p.add_argument('--donor', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    prepare(a.failed_image.read_bytes(), a.donor.read_bytes(), a.out)


if __name__ == '__main__':
    main()
