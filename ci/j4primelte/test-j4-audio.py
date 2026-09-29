#!/usr/bin/env python3
"""Synthetic tests only: never boot an ADSP, probe drivers or touch /lib/firmware."""
import importlib.util
import io
import contextlib
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('j4_audio', Path(__file__).with_name('j4-audio.py'))
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)


class J4AudioFirmwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def good_firmware(self):
        (self.base / 'adsp.mdt').write_bytes(b'test fixture only, not firmware')
        (self.base / 'adsp.b00').write_bytes(b'test fixture only, not firmware')
        (self.base / 'adsp.b01').write_bytes(b'test fixture only, not firmware')

    def test_complete_firmware(self):
        self.good_firmware()
        inst.check_firmware(self.base)

    def test_missing_mdt_rejected(self):
        (self.base / 'adsp.b00').write_bytes(b'test fixture only, not firmware')
        with self.assertRaises(inst.SetupError) as ctx:
            inst.check_firmware(self.base)
        self.assertIn('adsp.mdt', str(ctx.exception))
        self.assertIn('never stages firmware', str(ctx.exception))

    def test_missing_segments_rejected(self):
        (self.base / 'adsp.mdt').write_bytes(b'test fixture only, not firmware')
        with self.assertRaises(inst.SetupError) as ctx:
            inst.check_firmware(self.base)
        self.assertIn('adsp.b*', str(ctx.exception))

    def test_empty_files_rejected(self):
        (self.base / 'adsp.mdt').write_bytes(b'')
        (self.base / 'adsp.b00').write_bytes(b'test fixture only, not firmware')
        with self.assertRaises(inst.SetupError):
            inst.check_firmware(self.base)

    def test_symlinked_firmware_rejected(self):
        target = self.base / 'real.mdt'
        target.write_bytes(b'test fixture only, not firmware')
        (self.base / 'adsp.mdt').symlink_to(target)
        (self.base / 'adsp.b00').symlink_to(target)
        with self.assertRaises(inst.SetupError):
            inst.check_firmware(self.base)


class J4AudioInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = dict(
            HELPER=self.root / 'usr/local/sbin/j4-audio',
            INIT=self.root / 'etc/init.d/j4-audio',
            CONF=self.root / 'etc/conf.d/j4-audio',
            RUNLEVEL_LINK=self.root / 'etc/runlevels/boot/j4-audio',
        )

    def test_installer_writes_service_files_and_runs_bringup(self):
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(tuple(str(a) for a in args))
            if args[:3] == ('rc-update', 'add', 'j4-audio'):
                self.paths['RUNLEVEL_LINK'].parent.mkdir(parents=True, exist_ok=True)
                self.paths['RUNLEVEL_LINK'].symlink_to(self.paths['INIT'])
                return ''
            if args[0] == str(self.paths['HELPER']):
                return 'j4-audio: sound card is up'
            raise AssertionError(f'Unexpected operation: {args}')

        output = io.StringIO()
        with patch.multiple(inst, **self.paths), \
                patch.object(inst, 'check_device'), \
                patch.object(inst, 'check_adsp_loader'), \
                patch.object(inst, 'check_firmware'), \
                patch.object(inst, 'run', side_effect=fake_run), \
                patch.object(inst.os, 'sync'), \
                patch.object(inst, 'status_lines', return_value=['sound card device: bound']), \
                contextlib.redirect_stdout(output):
            inst.install()
        text = output.getvalue()
        self.assertIn('INSTALL_OK', text)
        self.assertIn('BRINGUP_OK', text)
        self.assertEqual(self.paths['HELPER'].read_text(), inst.SCRIPT_TEXT)
        self.assertEqual(self.paths['INIT'].read_text(), inst.INIT_TEXT)
        self.assertEqual(self.paths['CONF'].read_text(), inst.CONF_TEXT)
        self.assertEqual(stat.S_IMODE(self.paths['HELPER'].stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.paths['INIT'].stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(self.paths['CONF'].stat().st_mode), 0o644)
        self.assertTrue(self.paths['RUNLEVEL_LINK'].is_symlink())
        self.assertEqual(calls[0][:3], ('rc-update', 'add', 'j4-audio'))
        self.assertEqual(calls[0][3], 'boot')
        self.assertEqual(calls[1][0], str(self.paths['HELPER']))
        self.assertEqual(calls[1][1], '--boot')

    def test_reinstall_does_not_touch_runlevel_twice(self):
        self.paths['RUNLEVEL_LINK'].parent.mkdir(parents=True)
        self.paths['RUNLEVEL_LINK'].symlink_to(self.paths['INIT'])
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(tuple(str(a) for a in args))
            return ''

        with patch.multiple(inst, **self.paths), \
                patch.object(inst, 'check_device'), \
                patch.object(inst, 'check_adsp_loader'), \
                patch.object(inst, 'check_firmware'), \
                patch.object(inst, 'run', side_effect=fake_run), \
                patch.object(inst.os, 'sync'), \
                patch.object(inst, 'status_lines', return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()):
            inst.install()
        self.assertEqual([c[0] for c in calls], [str(self.paths['HELPER'])])

    def test_missing_firmware_aborts_before_writing(self):
        abort = inst.SetupError('Missing/empty/symlinked firmware')
        guards = [patch.object(inst, 'check_device'),
                  patch.object(inst, 'check_adsp_loader'),
                  patch.object(inst, 'check_firmware', side_effect=abort)]
        with patch.multiple(inst, **self.paths), \
                guards[0], guards[1], guards[2], \
                self.assertRaises(inst.SetupError):
            inst.install()
        self.assertFalse(self.paths['HELPER'].exists())
        self.assertFalse(self.paths['INIT'].exists())
        self.assertFalse(self.paths['CONF'].exists())

    def test_bringup_failure_still_installs(self):
        def fake_run(*args, **kwargs):
            if args[0] == 'rc-update':
                return ''
            raise inst.SetupError('no sound card after 45s')

        output = io.StringIO()
        with patch.multiple(inst, **self.paths), \
                patch.object(inst, 'check_device'), \
                patch.object(inst, 'check_adsp_loader'), \
                patch.object(inst, 'check_firmware'), \
                patch.object(inst, 'run', side_effect=fake_run), \
                patch.object(inst.os, 'sync'), \
                patch.object(inst, 'status_lines', return_value=[]), \
                contextlib.redirect_stdout(output):
            inst.install()
        self.assertIn('INSTALL_OK', output.getvalue())
        self.assertIn('BRINGUP_PENDING', output.getvalue())
        self.assertTrue(self.paths['HELPER'].is_file())

    def test_main_rejects_both_flags(self):
        with patch.object(inst.sys, 'argv', ['j4-audio.py', '--check', '--install']):
            with self.assertRaises(SystemExit):
                inst.main()


class J4AudioRepoConsistencyTests(unittest.TestCase):
    def test_embedded_payloads_match_rootfs_overlay(self):
        here = Path(__file__).parent
        self.assertEqual(inst.SCRIPT_TEXT,
                         (here / 'rootfs/usr/local/sbin/j4-audio').read_text())
        self.assertEqual(inst.INIT_TEXT,
                         (here / 'rootfs/etc/init.d/j4-audio').read_text())
        self.assertEqual(inst.CONF_TEXT,
                         (here / 'rootfs/etc/conf.d/j4-audio').read_text())

    def test_payloads_carry_version_marker(self):
        self.assertIn('J4_AUDIO_V1', inst.SCRIPT_TEXT)
        self.assertIn('J4_AUDIO_V1', inst.INIT_TEXT)
        self.assertIn('J4_AUDIO_V1', inst.CONF_TEXT)

    def test_payloads_never_stage_firmware(self):
        for text in (inst.SCRIPT_TEXT, inst.INIT_TEXT, inst.CONF_TEXT):
            self.assertNotIn('/mmcblk', text)
            self.assertNotIn('mount ', text)

    def test_customize_rootfs_enables_service(self):
        customize = (Path(__file__).parent / 'customize-rootfs.py').read_text()
        self.assertIn('j4-audio', customize)

    def test_runtime_script_is_posix_sh(self):
        self.assertTrue(inst.SCRIPT_TEXT.startswith('#!/bin/sh\n'))
        self.assertIn('drivers_probe', inst.SCRIPT_TEXT)
        self.assertIn('/sys/kernel/boot_adsp/boot', inst.SCRIPT_TEXT)


if __name__ == '__main__':
    unittest.main()
