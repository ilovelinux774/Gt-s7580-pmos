#!/usr/bin/env python3
"""Unit tests for the userdata migration helper."""
import importlib.util
import subprocess
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
MOVE = HERE / 'rootfs' / 'usr' / 'local' / 'sbin' / 'j4-rootfs-move'
INSTALLER = HERE / 'rootfs-move.py'


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = MOVE.read_text()

    def test_it_is_valid_shell(self):
        subprocess.run(['sh', '-n', str(MOVE)], check=True)

    def test_the_installer_embeds_this_script(self):
        text = INSTALLER.read_text()
        self.assertIn('rootfs-move.py', text)
        self.assertIn('j4-rootfs-move', text)

    def test_move_requires_an_explicit_confirmation(self):
        # --move wipes the internal storage; it must not be a stray keystroke.
        self.assertIn('--yes', self.text)
        self.assertIn('ERASES userdata', self.text)

    def test_running_root_is_never_touched(self):
        self.assertIn('already running from', self.text)
        self.assertIn('the old system is untouched', self.text)

    def test_it_refuses_to_format_a_mounted_partition(self):
        self.assertIn('is mounted; unmount it first', self.text)

    def test_it_refuses_without_the_needed_tools(self):
        self.assertIn('mkfs.ext4', self.text)
        self.assertIn('apk add e2fsprogs', self.text)

    def test_pseudo_filesystems_are_created_empty(self):
        for name in ('proc', 'sys', 'dev', 'run', 'tmp', 'mnt', 'media'):
            self.assertIn(name, self.text)
        self.assertIn('SKIP=', self.text)
        self.assertIn('mkdir -p "$dest/$name"', self.text)

    def test_backup_is_possible_before_the_format(self):
        self.assertIn('--backup', self.text)
        self.assertIn('backup destination is not writable', self.text)

    def test_it_checks_the_right_partitions(self):
        self.assertIn('/dev/mmcblk0p53', self.text)
        self.assertIn('pmOS_root', self.text)

    def test_it_reports_the_new_uuid_and_the_next_step(self):
        self.assertIn('NEW_UUID', self.text)
        self.assertIn('patch-boot-uuid.py', self.text)
        self.assertIn('/dev/mmcblk0p23', self.text)


class InstallerTests(unittest.TestCase):
    def test_installer_is_valid_python(self):
        spec = importlib.util.spec_from_file_location('rootfs_move', INSTALLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.HERE.is_dir())


if __name__ == '__main__':
    unittest.main()
