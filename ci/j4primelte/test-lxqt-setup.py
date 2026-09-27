#!/usr/bin/env python3
"""Synthetic tests only: no apk, no services, no host framebuffer access."""
import importlib.util
import io
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import contextlib

spec = importlib.util.spec_from_file_location('lxqt_setup', Path(__file__).with_name('lxqt-setup.py'))
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

SAMPLE_INPUT = '''
I: Bus=0000 Vendor=0000 Product=0000 Version=0000
N: Name="qpnp_pon"
H: Handlers=sec_key_notifier kbd event0

I: Bus=0018 Vendor=0000 Product=0000 Version=0000
N: Name="sec_touchscreen"
P: Phys=sec_touchscreen/input1
S: Sysfs=/devices/soc/78b6000.i2c/i2c-2/2-0048/input/input10
U: Uniq=
H: Handlers=sec_key_notifier kbd mdss_fb kgsl mouse0 event10
B: PROP=2
'''


class LxqtSetupTests(unittest.TestCase):
    def test_touch_event_parsing(self):
        self.assertEqual(setup.find_touch_event(SAMPLE_INPUT), '/dev/input/event10')

    def test_touch_event_absent(self):
        self.assertIsNone(setup.find_touch_event('N: Name="gpio_keys"\nH: Handlers=kbd event9\n'))

    def test_desktop_and_helper_files(self):
        self.assertIn('Type=Application', setup.AUTOSTART_TEXT)
        self.assertIn('Exec=/usr/local/sbin/j4-screen-on', setup.AUTOSTART_TEXT)
        self.assertIn('xset -dpms', setup.SCREEN_ON_TEXT)
        self.assertTrue(setup.SCREEN_ON_TEXT.startswith('#!/bin/sh'))

    def test_atomic_write_modes_and_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sub' / 'file'
            setup.atomic_write(path, b'one', 0o755)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
            setup.atomic_write(path, b'two', 0o644)
            self.assertEqual(path.read_bytes(), b'two')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
            self.assertEqual(len(list(path.parent.iterdir())), 1)

    def test_space_gate_rejects_small_root(self):
        with patch.object(setup.os, 'statvfs', return_value=Mock(f_bavail=10, f_frsize=4096)):
            self.assertLess(setup.free_bytes(), setup.MIN_FREE)

    def test_package_list_is_lxqt_only(self):
        self.assertIn('postmarketos-ui-lxqt', setup.PACKAGES)
        self.assertIn('onboard', setup.PACKAGES)
        joined = ' '.join(setup.PACKAGES)
        self.assertNotIn('dropbear', joined)
        self.assertNotIn('j4-usb-debug', joined)

    def test_install_stops_fb_splash_before_apk(self):
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(tuple(args))
            if args[0] == 'apk' and args[1] == 'update':
                return 'OK: 1 package(s) updated'
            if args[:3] == ('apk', 'add', '--simulate'):
                return 'Installing postmarketos-ui-lxqt (0.4-r2)'
            return ''

        def fake_subprocess(*args, **kwargs):
            calls.append(tuple(args[0]))
            return Mock(returncode=0, stdout='', stderr='')

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fb_init = root / 'j4-fb-splash.init'
            fb_init.write_text('#!/sbin/openrc-run\n')
            paths = dict(SCREEN_ON=root / 'usr/local/sbin/j4-screen-on',
                         AUTOSTART=root / 'etc/xdg/autostart/j4-screen-on.desktop',
                         BACKUP=root / 'var/lib/j4-lxqt',
                         FB_INIT=fb_init,
                         FB_STARTED=root / 'not-started')
            out = io.StringIO()
            with patch.multiple(setup, **paths), \
                    patch.object(setup, 'preflight', return_value=('/dev/input/event10', 'plan')), \
                    patch.object(setup, 'display_service', return_value='tinydm'), \
                    patch.object(setup, 'run', side_effect=fake_run), \
                    patch.object(setup.subprocess, 'run', side_effect=fake_subprocess), \
                    patch.object(setup.os, 'sync'), contextlib.redirect_stdout(out):
                setup.install()
            self.assertIn('INSTALL_OK', out.getvalue())
            self.assertIn('xset', paths['SCREEN_ON'].read_text())
            self.assertIn('Type=Application', paths['AUTOSTART'].read_text())
            self.assertEqual(stat.S_IMODE(paths['SCREEN_ON'].stat().st_mode), 0o755)
        stop_index = next(i for i, c in enumerate(calls)
                          if c[:2] == ('rc-service', 'j4-fb-splash') and c[2] == 'stop')
        del_index = next(i for i, c in enumerate(calls)
                         if c[:3] == ('rc-update', 'del', 'j4-fb-splash'))
        add_index = next(i for i, c in enumerate(calls)
                         if c[:2] == ('apk', 'add') and c[2] == '--no-progress')
        self.assertLess(stop_index, add_index)
        self.assertLess(del_index, add_index)

    def test_install_refuses_when_fb_splash_will_not_stop(self):
        def fake_run(*args, **kwargs):
            if args[:3] == ('apk', 'add', '--simulate'):
                return 'Installing postmarketos-ui-lxqt'
            return ''

        def fake_subprocess(*args, **kwargs):
            return Mock(returncode=0, stdout='', stderr='')

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fb_init = root / 'j4-fb-splash.init'
            fb_init.write_text('x')
            fb_started = root / 'started'
            fb_started.write_text('')
            paths = dict(SCREEN_ON=root / 'a', AUTOSTART=root / 'b', BACKUP=root / 'c',
                         FB_INIT=fb_init, FB_STARTED=fb_started)
            with patch.multiple(setup, **paths), \
                    patch.object(setup, 'preflight', return_value=('/dev/input/event10', 'plan')), \
                    patch.object(setup, 'run', side_effect=fake_run), \
                    patch.object(setup.subprocess, 'run', side_effect=fake_subprocess), \
                    patch.object(setup.os, 'sync'), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(setup.SetupError, 'did not stop'):
                    setup.install()

    def test_fallback_session_uses_spare_vt(self):
        self.assertIn('vt7', setup.FALLBACK_SESSION)
        self.assertIn('startlxqt', setup.FALLBACK_SESSION)
        self.assertIn('after j4-usb-debug', setup.FALLBACK_INIT)

    def test_host_guard(self):
        result = subprocess.run(['python3', str(Path(__file__).with_name('lxqt-setup.py')), '--check'],
                                capture_output=True, text=True, cwd=Path(__file__).parent)
        self.assertEqual(result.returncode, 1)
        self.assertIn('phone', result.stderr.lower())


if __name__ == '__main__':
    unittest.main()
