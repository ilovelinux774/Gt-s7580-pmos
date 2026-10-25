#!/usr/bin/env python3
"""Synthetic tests only: never open a host framebuffer or touch brightness."""
import importlib.util
from pathlib import Path
import struct
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
spec = importlib.util.spec_from_file_location('fb_splash', Path(__file__).with_name('fb-splash.py'))
splash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(splash)


class SplashTests(unittest.TestCase):
    def setUp(self):
        # Exact geometry reported by the SM-J415F user's fbdebug output.
        self.fix = bytearray(68)
        self.fix[:12] = b'mdssfb_80000'
        struct.pack_into('<4I', self.fix, 20, 8716288, 0, 0, 2)
        struct.pack_into('<I', self.fix, 44, 2944)
        self.var = bytearray(160)
        struct.pack_into('<8I', self.var, 0, 720, 1480, 720, 2960, 0, 0, 32, 0)
        for off, bit in zip((32, 44, 56, 68), (0, 8, 16, 24)):
            struct.pack_into('<3I', self.var, off, bit, 8, 0)

    def g(self):
        return splash.geometry(self.fix, self.var)

    def buffer(self, g):
        return bytearray(b'\xAA' * g['memory_bytes'])

    def test_reported_geometry(self):
        g = self.g()
        self.assertEqual((g['width'], g['height'], g['stride']), (720, 1480, 2944))

    def test_every_glyph_is_five_by_seven(self):
        for char, glyph in splash.GLYPHS.items():
            self.assertEqual(len(glyph), 7, char)
            self.assertTrue(all(len(row) == 5 for row in glyph), char)

    def test_title_and_subtitle_fit(self):
        self.assertLessEqual(splash.text_width('POSTMARKETOS', 6), 720)
        self.assertLessEqual(splash.text_width('SAMSUNG SM-J415F - J4PLUS DEBUG', 3), 720)
        self.assertLessEqual(splash.text_width('BOOT OK - FB OWNED BY PMOS', 3), 720)

    def test_splash_draws_pixels_but_never_padding(self):
        g = self.g()
        data = self.buffer(g)
        splash.draw_splash(data, g, 'USB 172.16.42.1')
        self.assertEqual(bytes(data[2880:2944]), b'\xAA' * 64)  # first row padding
        self.assertNotEqual(bytes(data[:2880]), b'\xAA' * 2880)
        self.assertEqual(data[1480 * 2944:], b'\xAA' * (len(data) - 1480 * 2944))

    def test_status_redraw_is_local(self):
        g = self.g()
        data = self.buffer(g)
        splash.draw_splash(data, g, 'HOST J4-DEBUG')
        before_top = bytes(data[100 * 2944:140 * 2944])
        splash.draw_status(data, g, 'UP 00:00:05')
        splash.draw_status(data, g, 'UP 12:34:56')
        self.assertEqual(bytes(data[100 * 2944:140 * 2944]), before_top)
        region = bytes(data[splash.STATUS_Y * 2944:(splash.STATUS_Y + 32) * 2944])
        self.assertNotEqual(region, bytes(len(region)))
        self.assertEqual(bytes(data[2880:2944]), b'\xAA' * 64)

    def test_text_clips_without_crash(self):
        g = self.g()
        data = self.buffer(g)
        splash.draw_text(data, g, g['width'] - 10, g['height'] - 5, 'WIDE TEXT', 6, splash.FG)
        splash.fill_rect(data, g, -20, -20, 50, 50, splash.BAND)
        self.assertEqual(data[1480 * 2944:], b'\xAA' * (len(data) - 1480 * 2944))

    def test_second_buffer_and_padding_untouched_with_y_offset(self):
        struct.pack_into('<I', self.var, 20, 1480)
        g = self.g()
        data = self.buffer(g)
        splash.draw_splash(data, g, 'USB 172.16.42.1')
        self.assertEqual(bytes(data[:1480 * 2944]), b'\xAA' * (1480 * 2944))
        self.assertEqual(bytes(data[1480 * 2944 + 2880:1480 * 2944 + 2944]), b'\xAA' * 64)

    def test_bad_layouts_rejected(self):
        struct.pack_into('<I', self.var, 32, 16)
        with self.assertRaises(splash.SplashError):
            self.g()
        struct.pack_into('<I', self.var, 32, 0)
        struct.pack_into('<I', self.fix, 44, 2800)
        with self.assertRaises(splash.SplashError):
            self.g()

    def test_install_refuses_to_overwrite_the_boot_text(self):
        """fbcon drawing the boot text and this splash drawing a logo are mutually
        exclusive on one framebuffer, so the installer has to look before it
        writes. The check is sysfs, not the kernel config: an image built from an
        older kernel is on the phone right now, whatever this repository says."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            saved = splash.VTCONSOLE
            try:
                con = root / 'vtcon0'
                con.mkdir()
                (con / 'name').write_text('(fg) dummy device\n')
                (con / 'bind').write_text('1\n')
                splash.VTCONSOLE = root
                self.assertFalse(splash.fbcon_bound())
                fb = root / 'vtcon1'
                fb.mkdir()
                (fb / 'name').write_text('(fg) frame buffer device\n')
                (fb / 'bind').write_text('0\n')
                self.assertFalse(splash.fbcon_bound(), 'unbound console counted')
                (fb / 'bind').write_text('1\n')
                self.assertTrue(splash.fbcon_bound())
                # A missing sysfs tree (host, or a kernel with no VT layer) must
                # not raise: the daemon is still the only way to see anything.
                splash.VTCONSOLE = root / 'nope'
                self.assertFalse(splash.fbcon_bound())
            finally:
                splash.VTCONSOLE = saved

    def test_boot_reaches_the_panel_from_the_build_scripts(self):
        """Three files have to agree or the panel stays blank and everyone is
        surprised: the kernel must build fbcon in, and the boot image must ask for
        a tty0 console (CONFIG_CMDLINE is empty in this tree, so the header
        cmdline is the whole command line)."""
        kernel_sh = (HERE / 'build-kernel.sh').read_text()
        self.assertIn('FRAMEBUFFER_CONSOLE VT_HW_CONSOLE_BINDING', kernel_sh,
                      'fbcon is no longer enabled in the build')
        self.assertNotIn('TZDEV FRAMEBUFFER_CONSOLE', kernel_sh,
                      'fbcon is still on the disable list, which cancels the enable')
        self.assertIn('BT_HCI_SMD FRAMEBUFFER_CONSOLE', kernel_sh,
                      'nothing asserts that fbcon survived olddefconfig; a symbol '
                      'that only exists at =m silently shows no boot text')
        deviceinfo = (ROOT / 'pmaports/device/downstream/'
                      'device-samsung-j4primelte/deviceinfo').read_text()
        self.assertIn('console=tty0', deviceinfo, 'no VT console in the bootargs, '
                      'so /dev/console never reaches the panel')
        self.assertIn('console=ttyMSM0,115200n8', deviceinfo,
                      'losing the UART console would cost the debugging path')
        self.assertIn('consoleblank=0', deviceinfo,
                      'VT blanking on mdss_fb has stranded this backlight before')
        self.assertIn('fbcon=map:0', deviceinfo)
        self.assertNotIn(' quiet', deviceinfo,
                         'quiet would hide the very messages this is for')


        self.assertIn('supervisor="supervise-daemon"', splash.INIT_TEXT)
        self.assertIn('after devfs', splash.INIT_TEXT)
        self.assertNotIn('j4-usb-debug', splash.INIT_TEXT.replace('after devfs', ''))
        self.assertNotIn('dropbear', splash.INIT_TEXT)
        self.assertNotIn('restart network', splash.INIT_TEXT)


if __name__ == '__main__':
    unittest.main()
