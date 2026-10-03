#!/usr/bin/env python3
"""Synthetic cache unit tests. These fixtures are never used in an image build."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('kernel_cache', Path(__file__).with_name('kernel-cache.py'))
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'ci/j4primelte/sources.env'
        source.parent.mkdir(parents=True)
        source.write_text('KERNEL_URL=https://example.invalid/kernel\nKERNEL_COMMIT=abc\n'
                          'TOOLCHAIN_URL=https://example.invalid/compiler\nTOOLCHAIN_COMMIT=def\n'
                          'PMAPORTS_COMMIT=old\n')
        (source.parent / 'build-kernel.sh').write_text('#!/bin/sh\necho test fixture\n')
        for name, dest in cache.FILES.items():
            path = self.root / dest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('unit test fixture: ' + name).encode())

    def save(self):
        with patch.object(cache.subprocess, 'check_output', return_value='test-ci-revision\n'):
            cache.capture(self.root)

    def test_roundtrip(self):
        self.save()
        for path in cache.FILES.values():
            (self.root / path).unlink(missing_ok=True)
        cache.restore(self.root)
        self.assertTrue((self.root / cache.PACKAGE / 'zImage-dtb').is_file())

    def test_corruption_rejected(self):
        self.save()
        (self.root / 'artifacts/kernel-cache/package/zImage-dtb').write_bytes(b'corruption')
        with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
            cache.restore(self.root)

    def test_changed_kernel_recipe_rejected(self):
        self.save()
        (self.root / 'ci/j4primelte/build-kernel.sh').write_text('different kernel configuration')
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            cache.restore(self.root)

    def test_userspace_change_keeps_kernel_cache(self):
        self.save()
        path = self.root / 'ci/j4primelte/sources.env'
        path.write_text(path.read_text().replace('PMAPORTS_COMMIT=old', 'PMAPORTS_COMMIT=new'))
        cache.restore(self.root)


if __name__ == '__main__':
    unittest.main()
