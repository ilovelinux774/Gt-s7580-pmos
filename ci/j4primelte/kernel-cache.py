#!/usr/bin/env python3
"""Cache compiler outputs independently of userspace packaging; verify on reuse."""
import hashlib
import json
import os
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


def staged_kernel_files(root):
    """Kernel sources this repository stages into the pinned tree.

    A driver-only change must invalidate the cached kernel, so these files are
    hashed into the recipe key just like build-kernel.sh.
    """
    directory = root / 'ci/j4primelte/kernel'
    if not directory.is_dir():
        return []
    return sorted(directory.glob('*.c')) + sorted(directory.glob('*.h'))


# Helpers that patch the checked-out tree. Their content changes the kernel, so
# it belongs in the key just like build-kernel.sh does.
STAGING_HELPERS = ('kernel-bt.py', 'kernel-gpu.py')


def recipe_key(root):
    h = hashlib.sha256(b'j4-kernel-cache-v1\n')
    # The GPU experiment rebuilds the kernel around DRM instead of the MDSS
    # framebuffer driver, so a cache entry from the other setting must never
    # be reused, even though no checked-in file changed.
    h.update(('gpu_experiment=%s\n'
              % os.environ.get('J4_GPU_EXPERIMENT', '0')).encode())
    h.update(json.dumps(source_pins(root), sort_keys=True).encode())
    h.update((root / 'ci/j4primelte/build-kernel.sh').read_bytes())
    for name in STAGING_HELPERS:
        helper = root / 'ci/j4primelte' / name
        if helper.is_file():
            h.update(name.encode())
            h.update(helper.read_bytes())
    for path in staged_kernel_files(root):
        h.update(path.name.encode())
        h.update(path.read_bytes())
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
    # Belt and braces: the key already carries the flag, but never let a kernel
    # built with the other GPU setting reach an image.
    config = (cache / 'package/kernel.config').read_text(errors='replace')
    if (os.environ.get('J4_GPU_EXPERIMENT', '0') == '1') != \
            ('CONFIG_DRM_MSM=m' in config):
        raise RuntimeError('Cached kernel was built with the other GPU setting')
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
