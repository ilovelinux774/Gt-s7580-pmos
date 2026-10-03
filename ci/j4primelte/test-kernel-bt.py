#!/usr/bin/env python3
"""Unit tests for the WCNSS Bluetooth HCI driver staging helper."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(name.replace('_', '-') + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bt = load('kernel_bt')
cache = load('kernel_cache')

FAKE_MAKEFILE = """#
# Makefile for the Linux Bluetooth HCI device drivers.
#

obj-$(CONFIG_BT_HCIVHCI)\t+= hci_vhci.o
obj-$(CONFIG_BT_HCIUART)\t+= hci_uart.o

obj-$(CONFIG_MSM_BT_POWER)\t+= bluetooth-power.o
ccflags-y += -D__CHECK_ENDIAN__
"""

FAKE_KCONFIG = """menu "Bluetooth device drivers"

config BT_HCIUART
\ttristate "HCI UART driver"
\tdepends on BT

config MSM_BT_POWER
\tbool "MSM Bluetooth power control driver"

config BTFM_SLIM
\ttristate "BTFM SLIM driver"

endmenu
"""


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        bluetooth = self.tree / 'drivers' / 'bluetooth'
        bluetooth.mkdir(parents=True)
        (bluetooth / 'Makefile').write_text(FAKE_MAKEFILE)
        (bluetooth / 'Kconfig').write_text(FAKE_KCONFIG)
        self.makefile = bluetooth / 'Makefile'
        self.kconfig = bluetooth / 'Kconfig'

    def test_driver_source_exists(self):
        self.assertTrue(bt.DRIVER.is_file())
        text = bt.DRIVER.read_text()
        for needle in ('APPS_RIVA_BT_CMD', 'APPS_RIVA_BT_ACL', 'smd_named_open_on_edge',
                       'hci_register_dev', 'SMD_APPS_WCNSS'):
            self.assertIn(needle, text)

    def test_apply_wires_driver(self):
        dest = bt.apply(self.tree)
        self.assertTrue(dest.is_file())
        self.assertIn('hci_smd.o', self.makefile.read_text())
        kconfig = self.kconfig.read_text()
        self.assertIn('config BT_HCI_SMD', kconfig)
        self.assertTrue(kconfig.index('config BT_HCI_SMD') <
                        kconfig.index('config MSM_BT_POWER'))
        self.assertIn('depends on BT && MSM_SMD', kconfig)

    def test_apply_is_idempotent(self):
        bt.apply(self.tree)
        first_makefile = self.makefile.read_text()
        first_kconfig = self.kconfig.read_text()
        bt.apply(self.tree)
        self.assertEqual(first_makefile, self.makefile.read_text())
        self.assertEqual(first_kconfig, self.kconfig.read_text())
        self.assertEqual(first_kconfig.count('config BT_HCI_SMD'), 1)

    def test_check_reports_state(self):
        with patch('sys.argv', ['kernel-bt.py', str(self.tree), '--check']):
            self.assertEqual(bt.main(), 1)
        bt.apply(self.tree)
        with patch('sys.argv', ['kernel-bt.py', str(self.tree), '--check']):
            self.assertEqual(bt.main(), 0)

    def test_apply_rejects_non_kernel_tree(self):
        with self.assertRaises(SystemExit):
            bt.apply(self.tree / 'drivers')


class CacheKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = self.root / 'ci/j4primelte/sources.env'
        env.parent.mkdir(parents=True)
        env.write_text('KERNEL_URL=https://example.invalid/kernel\nKERNEL_COMMIT=abc\n'
                       'TOOLCHAIN_URL=https://example.invalid/compiler\nTOOLCHAIN_COMMIT=def\n')
        (env.parent / 'build-kernel.sh').write_text('#!/bin/sh\necho test fixture\n')

    def test_staged_kernel_changes_key(self):
        base = cache.recipe_key(self.root)
        staged = self.root / 'ci/j4primelte/kernel'
        staged.mkdir()
        (staged / 'hci_smd.c').write_text('/* driver v1 */\n')
        with_driver = cache.recipe_key(self.root)
        self.assertNotEqual(base, with_driver)
        (staged / 'hci_smd.c').write_text('/* driver v2 */\n')
        self.assertNotEqual(with_driver, cache.recipe_key(self.root))

    def test_missing_staged_directory_is_fine(self):
        self.assertEqual(cache.staged_kernel_files(self.root), [])


if __name__ == '__main__':
    unittest.main()
