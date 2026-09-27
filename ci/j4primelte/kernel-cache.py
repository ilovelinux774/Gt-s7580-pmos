#!/usr/bin/env python3
"""Cache compiler outputs independently of userspace packaging; verify on reuse."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path('pmaports/device/downstream/linux-samsung-j4primelte')
FILES = {
    **{f'package/{name}': PACKAGE / name for name in
       ('zImage-dtb', 'kernel.release', 'kernel.config', 'kernel-modules.tar.gz')},
    **{f'logs/{name}': Path('artifacts/logs') / name for name in
       ('kernel.config', 'kernel-source.patch', 'dtbs.txt')},
}


def source_pins(root):
    values = dict(line.split('=', 1) for line in
                  (root / 'ci/j4primelte/sources.env').read_text().splitlines()
                  if line and not line.startswith('#'))
    return {key: values[key] for key in
            ('KERNEL_URL', 'KERNEL_COMMIT', 'TOOLCHAIN_URL', 'TOOLCHAIN_COMMIT')}


def recipe_key(root):
    h = hashlib.sha256(b'j4-kernel-cache-v1\n')
    h.update(json.dumps(source_pins(root), sort_keys=True).encode())
    h.update((root / 'ci/j4primelte/build-kernel.sh').read_bytes())
    return h.hexdigest()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(root):
    cache = root / 'artifacts/kernel-cache'
    if cache.exists():
        shutil.rmtree(cache)
    checksums = {}
    for relative, source in FILES.items():
        source = root / source
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f'Kernel output missing or a symlink: {source}')
        dest = cache / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        checksums[relative] = sha256(dest)
    manifest = {
        'version': 1,
        'recipe_key': recipe_key(root),
        'source_pins': source_pins(root),
        'kernel_build_ci_commit': subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
        'files': checksums,
        'not_a_flashable_image': True,
    }
    (cache / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    shutil.copyfile(cache / 'manifest.json', root / 'artifacts/logs/kernel-build-provenance.json')
    print('Saved verified kernel inputs for reuse before userspace image creation')


def restore(root):
    cache = root / 'artifacts/kernel-cache'
    manifest = json.loads((cache / 'manifest.json').read_text())
    if manifest['version'] != 1 or manifest['recipe_key'] != recipe_key(root):
        raise RuntimeError('Cached kernel does not match this build recipe')
    if set(manifest['files']) != set(FILES):
        raise RuntimeError('Cached kernel has an unexpected file list')
    # Verify every file before copying any of them into package inputs.
    for relative, expected in manifest['files'].items():
        path = cache / relative
        if not path.is_file() or path.is_symlink() or sha256(path) != expected:
            raise RuntimeError(f'Cached kernel checksum mismatch: {relative}')
    for relative, dest in FILES.items():
        dest = root / dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cache / relative, dest)
    shutil.copyfile(cache / 'manifest.json', root / 'artifacts/logs/kernel-build-provenance.json')
    print('Restored checksum-verified kernel inputs; no kernel recompilation needed')


if __name__ == '__main__':
    if sys.argv[1:] == ['key']:
        print(recipe_key(ROOT))
    elif sys.argv[1:] == ['capture']:
        capture(ROOT)
    elif sys.argv[1:] == ['restore']:
        restore(ROOT)
    else:
        raise SystemExit('Usage: kernel-cache.py {key,capture,restore}')
