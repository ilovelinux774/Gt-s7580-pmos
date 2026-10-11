#!/usr/bin/env python3
"""Tests for the mainline M1 pipeline: fragment, staging, packing, verifying.

Run them from the repository root:

    TMPDIR=/home/user/tmp J4_MAINLINE_KERNEL_TREE=/home/user/j4-mainline-kernel \
        python3 ci/j4primelte/test-mainline.py

Everything that can be checked without a kernel build is checked against the real
files in this repository (the fragment, the device trees, the panel driver), and
the boot-image tests run the same code paths the CI job runs. Tests that need the
pinned tree are skipped when it is not there, which is how the workflow can run
this file before it clones anything.
"""
from pathlib import Path
import gzip
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SRC = HERE / 'mainline'
FRAGMENT = SRC / 'j4mainline.fragment'
DTS = SRC / 'msm8917-samsung-j4primelte.dts'
PANEL_DTS = SRC / 'msm8917-samsung-j4primelte-panel.dts'
PANEL_C = SRC / 'panel-samsung-s6d7at0b01.c'
KERNEL_TREE = Path(os.environ.get('J4_MAINLINE_KERNEL_TREE',
                                 Path.home() / 'j4-mainline-kernel'))
# Generated once from the pinned tree (`make help`/kconfig dump); the only list
# that can tell a real symbol from a plausible-looking one.
SYMBOL_KINDS = Path('/home/user/build-kconfig-defs.txt')


def load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, str(HERE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repack = load('j4_repack', 'mainline-repack-boot.py')
config = load('j4_config', 'mainline-config.py')
staging = load('j4_staging', 'mainline-kernel.py')
verify = load('j4_verify', 'verify-mainline.py')
donor = load('j4_donor_note', 'donor-note.py')

DTB_MAGIC = bytes.fromhex('d00dfeed')


def append_dtb(image: bytes, tree: bytes, align: int = 8) -> bytes:
    """Mirror build-mainline.sh: pad the kernel, then the tree reaches the end."""
    return image + b'\0' * ((-len(image)) % align) + tree


def fake_dtb(size: int = 4096) -> bytes:
    """A trailing DTB the way dtc writes one: magic, then its own totalsize.

    The packer does not take "the magic appears somewhere near the end" as proof
    of an appended tree, so a fixture has to be arithmetically consistent.
    """
    assert size > 8
    return DTB_MAGIC + struct.pack('>I', size) + b'\0' * (size - 8)


def make_boot(kernel: bytes = b'original kernel', ramdisk: bytes = b'ramdisk blob',
              second: bytes = b'', page: int = 4096, name: bytes = b'j4primelte',
              cmdline: str = 'ro console=ttyMSM0,115200n8 rootwait '
                             'pmos_root_uuid=86e7262b-b628-4b89-8a6e-e96ef9b1aab6'):
    align = lambda n: (n + page - 1) // page * page
    out = bytearray(page)
    out[0:8] = b'ANDROID!'
    struct.pack_into('<10I', out, 8, len(kernel), 0x80008000, len(ramdisk),
                     0x82000000, len(second), 0x80f00000, 0x1e000000, page, 0,
                     (0 << 11) | 11)
    out[48:64] = name.ljust(16, b'\0')
    first, tail = repack.split_cmdline(cmdline.encode())
    out[64:64 + 512] = first.ljust(512, b'\0')
    out[608:608 + 1024] = tail.ljust(1024, b'\0')
    for blob in (kernel, ramdisk, second):
        out += blob
        out += b'\0' * (align(len(blob)) - len(blob))
    return bytes(out)


class Fragment(unittest.TestCase):
    """The policy file, parsed the way mainline-config.py parses it."""

    @classmethod
    def setUpClass(cls):
        cls.ops = config.parse_fragment(FRAGMENT, panel=True)
        cls.base = config.parse_fragment(FRAGMENT, panel=False)

    def test_parses_and_filters_panel_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            frag = Path(tmp) / 'f'
            frag.write_text('+ FOO\n- BAR\n@ STR hello world\n= CONFIG_NUM=7\n'
                            'PANEL + DRM_PANEL_SAMSUNG_S6D7AT0B01\n# comment\n')
            base = config.parse_fragment(frag)
            panel = config.parse_fragment(frag, panel=True)
            self.assertEqual(base, [('enable', 'FOO', None),
                                     ('disable', 'BAR', None),
                                     ('set-str', 'STR', 'hello world'),
                                     ('literal', 'NUM', '7')])
            driver = ('enable', 'DRM_PANEL_SAMSUNG_S6D7AT0B01', None)
            self.assertNotIn(driver, base)
            self.assertIn(driver, panel)

    def test_values_and_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            frag = Path(tmp) / 'f'
            frag.write_text('= CONFIG_FOO=y\n')
            self.assertEqual(config.parse_fragment(frag), [('literal', 'FOO', 'y')])
            for bad in ('% NOPE\n', '+ SYM extra\n', '@ SYM\n', 'lower_case\n',
                        '= FOO=y\n'):
                frag.write_text(bad)
                with self.assertRaises(ValueError, msg=bad):
                    config.parse_fragment(frag)

    def test_panel_build_must_enable_a_panel_driver(self):
        with tempfile.TemporaryDirectory() as tmp:
            frag = Path(tmp) / 'f'
            frag.write_text('+ DRM\nPANEL + DRM_MSM\n')
            with self.assertRaises(ValueError):
                config.parse_fragment(frag, panel=True)

    def test_render_defconfig(self):
        with tempfile.TemporaryDirectory() as tmp:
            frag = Path(tmp) / 'f'
            frag.write_text('+ A\n- B\n@ C one two\n= CONFIG_D=5\n')
            self.assertEqual(config.render_defconfig(frag),
                             'CONFIG_A=y\n# CONFIG_B is not set\n'
                             'CONFIG_C="one two"\nCONFIG_D=5\n')

    def test_shipped_fragment_is_sane(self):
        enabled = {s for kind, s, _ in self.ops if kind == 'enable'}
        disabled = {s for kind, s, _ in self.ops if kind == 'disable'}
        for symbol in config.REQUIRED:
            self.assertIn(symbol, enabled,
                          f'{symbol} is REQUIRED, so the fragment must enable it')
        # A FORBIDDEN symbol may be one that does not exist in this tree (a
        # downstream patch that must never come back); those are documentation.
        # Anything the tree does have must be turned off explicitly.
        for symbol in ('EFI', 'CMDLINE_FORCE', 'MODULES'):
            self.assertIn(symbol, disabled, f'{symbol} should be disabled by name')
        self.assertFalse(enabled & disabled, 'a symbol is both enabled and disabled')
        self.assertIn('DEBUG_INFO_NONE', enabled,
                      'a DWARF-enabled Image.gz does not fit the BOOT partition')
        self.assertIn('DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT', disabled,
                      'a choice member has to be cleared explicitly or '
                      'olddefconfig puts it back')
        # The M1 kernel must not be able to claim the display controller.
        for symbol in ('DRM_MSM', 'TOUCHSCREEN_S6SY761'):
            self.assertNotIn(symbol, {s for k, s, _ in self.base if k == 'enable'},
                             f'{symbol} belongs behind the PANEL gate')

    @unittest.skipUnless(SYMBOL_KINDS.is_file(), 'no symbol oracle here')
    def test_no_invented_symbols(self):
        kinds = {}
        for line in SYMBOL_KINDS.read_text().splitlines():
            if '\t' in line:
                name, kind = line.split('\t', 1)
                kinds[name] = kind
        unknown = sorted(s for _k, s, _v in self.ops
                         if s not in kinds and s != 'DRM_PANEL_SAMSUNG_S6D7AT0B01')
        self.assertEqual(unknown, [],
                         'fragment names symbols the pinned tree does not define')
        wrong = [s for kind, s, _v in self.ops
                 if kind == 'disable' and s in kinds
                 and kinds[s] not in ('bool', 'tristate')]
        self.assertEqual(wrong, [],
                         'only bool/tristate symbols can be turned off')


class BootPacking(unittest.TestCase):
    """The header layout, field by field, on a synthetic image."""

    def test_parses_the_legacy_v0_header(self):
        image = repack.BootImage(make_boot())
        self.assertEqual(image.page_size, 4096)
        self.assertEqual(image.kernel_addr, 0x80008000)
        self.assertEqual(image.tags_addr, 0x1e000000)
        self.assertEqual(image.name.rstrip(b'\0'), b'j4primelte')
        self.assertIn('pmos_root_uuid', image.cmdline)
        self.assertEqual(image.ramdisk, b'ramdisk blob')

    @unittest.skipUnless(Path('/home/user/Gt-s7580-pmos/boot.img').is_file(),
                         'no real boot image to parse here')
    def test_parses_a_real_samsung_image(self):
        data = Path('/home/user/Gt-s7580-pmos/boot.img').read_bytes()
        image = repack.BootImage(data)
        self.assertIn(image.page_size, (2048, 4096))
        self.assertGreater(image.kernel_size, 100000)
        # Repacking with its own kernel must be a no-op for every other byte.
        same = image.repack(image.kernel, image.ramdisk, image.second,
                            cmdline=image.cmdline)
        self.assertEqual(same.ramdisk, image.ramdisk)
        self.assertEqual(same.kernel, image.kernel)

    def test_kernel_swaps_and_ramdisk_survives(self):
        base = repack.BootImage(make_boot())
        payload = append_dtb(gzip.compress(b'arm64 Image body' * 100), fake_dtb(8192))
        out = repack.build(base, payload)
        self.assertEqual(out.kernel, payload)
        self.assertEqual(out.ramdisk, base.ramdisk)
        self.assertEqual(out.second, base.second)
        self.assertEqual(out.page_size, base.page_size)
        self.assertEqual(bytes(out.data[:8]), b'ANDROID!')
        # The board name is part of what fastboot and a recovery show, so it
        # has to be carried over untouched; the cmdline fields have to hold
        # exactly the split of the new command line, and the id has to be the
        # recomputed checksum, not the donor's.
        self.assertEqual(bytes(out.data[48:64]), bytes(base.data[48:64]))
        first, tail = repack.split_cmdline(out.cmdline.encode())
        self.assertEqual(bytes(out.data[64:64 + 512]).rstrip(b'\0'), first)
        self.assertEqual(bytes(out.data[608:608 + 1024]).rstrip(b'\0'), tail)

    def test_id_is_recomputed_over_the_payload(self):
        import hashlib
        base = repack.BootImage(make_boot())
        payload = append_dtb(gzip.compress(b'k' * 4000), fake_dtb())
        out = repack.build(base, payload)
        digest = hashlib.sha1(payload + out.ramdisk + out.second).digest()
        self.assertEqual(bytes(out.data[576:596]), digest)
        self.assertEqual(bytes(out.data[596:608]), b'\0' * 12)
        # Writing the digest must not have shifted anything: the ramdisk still
        # begins where the (new) kernel size says it does.
        again = repack.BootImage(bytes(out.data))
        self.assertEqual(again.ramdisk, base.ramdisk)

    def test_bootargs_are_appended_once(self):
        base = repack.BootImage(make_boot())
        payload = append_dtb(gzip.compress(b'k' * 4000), fake_dtb())
        out = repack.build(base, payload, debug_shell=True)
        tokens = out.cmdline.split()
        for flag in ('clk_ignore_unused', 'pd_ignore_unused', 'regulator_ignore_unused',
                     'rootwait', 'pmos.debug-shell'):
            self.assertEqual(tokens.count(flag), 1, f'{flag} in {out.cmdline}')
        self.assertIn('pmos_root_uuid=86e7262b-b628-4b89-8a6e-e96ef9b1aab6', tokens,
                      'the donor root marker has to survive or init never exits')

    def test_split_lands_on_a_space(self):
        short = b'a' * 500 + b' tok tok'
        first, tail = repack.split_cmdline(short)
        self.assertEqual((first, tail), (short, b''),
                         'a cmdline that fits the first field must not be split')
        long_cmd = b'z' * 505 + b' ' + b'w' * 90
        first, tail = repack.split_cmdline(long_cmd)
        self.assertLessEqual(len(first), 512)
        self.assertEqual(first + b' ' + tail, long_cmd,
                         'the cut has to be on whitespace so the join is exact')
        # An unbreakable token has no good answer; a hard cut is the honest one,
        # and it must not eat a byte of the tail.
        first, tail = repack.split_cmdline(b'x' * 600)
        self.assertEqual((len(first), len(tail)), (512, 88))

    def test_cmdline_too_long_is_refused(self):
        base = repack.BootImage(make_boot())
        with self.assertRaises(ValueError):
            base.set_cmdline('y' * (repack.CMDLINE_FIRST + repack.CMDLINE_REST + 1))

    def test_appended_dtb_is_located_by_its_own_size(self):
        image = gzip.compress(b'k' * 20000)
        tree = fake_dtb(43 * 1024 + 7)
        payload = append_dtb(image, tree)
        self.assertEqual(repack.find_appended_dtb(payload), len(image) + (-len(image)) % 8)
        with self.assertRaises(ValueError):
            repack.find_appended_dtb(payload + b'trailer')
        with self.assertRaises(ValueError):
            repack.find_appended_dtb(image + b'\0' * ((-len(image)) % 8 + 1) + tree)
        with self.assertRaises(ValueError):
            repack.find_appended_dtb(gzip.compress(b'no tree at all'))

    def test_garbage_is_refused(self):
        bad = bytearray(make_boot())
        bad[0:8] = b'NOTANANDROID'
        with self.assertRaises(ValueError):
            repack.BootImage(bytes(bad))
        versioned = bytearray(make_boot())
        struct.pack_into('<I', versioned, 8 + 8 * 4, 4)   # header version -> v4
        with self.assertRaises(ValueError):
            repack.BootImage(bytes(versioned))
        with self.assertRaises(ValueError):
            repack.BootImage(make_boot()[:1000])          # truncated header
        oversized = bytearray(make_boot())
        struct.pack_into('<I', oversized, 8, 10 ** 9)     # kernel_size beyond file
        with self.assertRaises(ValueError):
            repack.BootImage(bytes(oversized))
        paged = bytearray(make_boot())
        struct.pack_into('<I', paged, 8 + 7 * 4, 3000)    # not 2048/4096
        with self.assertRaises(ValueError):
            repack.BootImage(bytes(paged))

    def test_oversized_image_is_refused(self):
        # The BOOT partition is 32 MiB; rather than allocate that in a unit test,
        # shrink the limit and prove the packer honours it.
        base = repack.BootImage(make_boot(page=2048))
        payload = append_dtb(gzip.compress(b'arm64 kernel' * 512), fake_dtb())
        self.assertLess(len(payload), repack.MAX_IMAGE)
        original = repack.MAX_IMAGE
        try:
            repack.MAX_IMAGE = len(payload) // 2
            with self.assertRaises(ValueError):
                repack.build(base, payload)
        finally:
            repack.MAX_IMAGE = original

    def test_donor_note(self):
        note = donor.note(make_boot())
        self.assertIn('header v0', note)
        self.assertIn('ramdisk 12 B', note)
        with self.assertRaises(SystemExit):
            donor.note(b'NOTANANDROID' + b'\0' * 200)
        truncated = bytearray(make_boot())
        struct.pack_into('<I', truncated, 8 + 2 * 4, 0)   # ramdisk_size = 0
        with self.assertRaises(SystemExit):
            donor.note(bytes(truncated))


class RepackCli(unittest.TestCase):
    """The command line the CI build script actually runs."""

    def run_cli(self, payload, extra=(), debug=False):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base = root / 'base.img'
            base.write_bytes(make_boot())
            kern = root / 'payload.bin'
            kern.write_bytes(payload)
            out = root / 'boot.img'
            argv = ['--base', str(base), '--kernel', str(kern), '--out', str(out),
                    '--report', str(root / 'report.txt')]
            if debug:
                argv.append('--debug-shell')
            argv.extend(extra)
            self.assertEqual(repack.main(argv), 0)
            data = out.read_bytes()
            return data, (root / 'report.txt').read_text(), repack.BootImage(data)

    def test_real_payload_packs_and_reports(self):
        payload = append_dtb(gzip.compress(b'arm64 Image body' * 40), fake_dtb())
        data, report, img = self.run_cli(payload, debug=True)
        self.assertEqual(img.kernel, payload)
        self.assertEqual(img.ramdisk, repack.BootImage(make_boot()).ramdisk,
                         'the donor initramfs must be copied byte for byte')
        self.assertIn('pmos.debug-shell', img.cmdline.split())
        for token in repack.MAINLINE_BOOTARGS.split():
            self.assertIn(token, img.cmdline.split())
        self.assertIn('copied byte for byte', report)
        self.assertIn('header v0', report)
        self.assertIn('0x80008000', report, 'load addresses must be carried over')
        self.assertTrue(data[:8] == b'ANDROID!')

    def test_tree_bigger_than_a_page_is_still_found(self):
        # The first version of this guard looked only at the last 8 KiB, which is
        # where a DTB's *end* is, not its header: the real j4primelte tree is 43
        # KiB and was therefore refused by the tool that built it.
        tree = fake_dtb(43 * 1024 + 7)
        payload = append_dtb(gzip.compress(b'arm64 kernel' * 512), tree)
        data, _report, img = self.run_cli(payload)
        self.assertEqual(img.kernel, payload)
        self.assertEqual(img.kernel[len(img.kernel) - len(tree):], tree)

    def test_tree_not_reaching_the_last_byte_is_refused(self):
        payload = append_dtb(gzip.compress(b'arm64 kernel' * 512),
                             fake_dtb(4096)) + b'trailer'
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(payload)
        self.assertIn('DTB', str(ctx.exception))

    def test_misaligned_tree_is_refused(self):
        # LK scans for the magic on an aligned boundary; an unaligned tree would
        # boot with no device tree and no error message anywhere. One byte of
        # extra padding is enough to hide it from that scan.
        image = b'\x1f\x8b\x08' + b'k' * 101      # len 104, a multiple of 8
        payload = image + b'\0' * (8 + 1) + fake_dtb(4096)
        with self.assertRaises(SystemExit):
            self.run_cli(payload)

    def test_gzip_only_payload_is_refused(self):
        payload = gzip.compress(b'no device tree here')
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(payload)
        self.assertIn('DTB', str(ctx.exception))

    def test_non_gzip_payload_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(b'\x00' * 4096 + DTB_MAGIC)
        self.assertIn('gzip', str(ctx.exception))

    def test_repeated_bootargs_are_not_duplicated(self):
        payload = append_dtb(gzip.compress(b'k' * 500), fake_dtb())
        _, _, img = self.run_cli(payload, extra=['--cmdline-extra', 'rootwait'])
        tokens = img.cmdline.split()
        self.assertEqual(tokens.count('rootwait'), 1,
                         f'base already had rootwait: {img.cmdline}')
        # Packing an already-packed image must not stack the bootargs up again.
        again = repack.build(img, payload)
        self.assertEqual(again.cmdline.split().count('rootwait'), 1)
        self.assertEqual(again.cmdline, img.cmdline)


class Staging(unittest.TestCase):
    """What mainline-kernel.py does to a kernel tree."""

    def fake_tree(self, root: Path):
        (root / 'Kconfig').write_text('mainmenu "Fake"\n')
        qcom = root / 'arch' / 'arm64' / 'boot' / 'dts' / 'qcom'
        qcom.mkdir(parents=True)
        (qcom / 'msm8917.dtsi').write_text('/* nothing */\n')
        (qcom / 'Makefile').write_text(
            'dtb-$(CONFIG_ARCH_QCOM)\t+= msm8916-mtp.dtb\ndtb-$(CONFIG_ARCH_QCOM)\t'
            '+= msm8917-mtp.dtb\nclean-files += *.dtb.S\n')
        panel = root / 'drivers' / 'gpu' / 'drm' / 'panel'
        panel.mkdir(parents=True)
        (panel / 'Kconfig').write_text('menu "Panels"\n'
                                       'config DRM_PANEL_SIMPLE\n\tbool "x"\n'
                                       'endmenu\n')
        (panel / 'Makefile').write_text(
            'obj-$(CONFIG_DRM_PANEL) += panel.o\n'
            'obj-$(CONFIG_DRM_PANEL_SIMPLE) += panel-simple.o\n')
        (root / 'arch' / 'arm64' / 'configs').mkdir(parents=True)
        return root

    def test_dtb_lines_are_registered_once_with_a_tab(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fake_tree(Path(tmp))
            staging.stage_dts(root)
            staging.stage_dts(root)                      # idempotent
            text = (root / 'arch' / 'arm64' / 'boot' / 'dts' / 'qcom' /
                    'Makefile').read_text()
            self.assertEqual(text.count(staging.DTB), 1, 'the dtb line was doubled')
            self.assertEqual(text.count(staging.PANEL_DTB), 1)
            for line in text.splitlines():
                if line.startswith('dtb-$(CONFIG_ARCH_QCOM)'):
                    self.assertIn('\t', line,
                                  'make reads the variable name up to the first '
                                  'whitespace: a literal backslash-t defines a '
                                  'different variable')
            self.assertTrue((root / 'arch' / 'arm64' / 'boot' / 'dts' / 'qcom' /
                             staging.DTS).is_file())

    def test_anchor_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'Makefile').write_text('nothing relevant here\n')
            with self.assertRaises(SystemExit):
                staging.insert_after_last(root / 'Makefile',
                                          r'^dtb-\$\(CONFIG_ARCH_QCOM\).*$', 'x')

    def test_panel_stanza_is_refreshed_not_skipped(self):
        """A tree staged by an older version of this file must not stay stale."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fake_tree(Path(tmp))
            staging.stage_panel(root)
            staging.stage_panel(root)                    # idempotent
            kconfig = (root / 'drivers' / 'gpu' / 'drm' / 'panel' / 'Kconfig')
            text = kconfig.read_text()
            self.assertEqual(text.count('config DRM_PANEL_SAMSUNG_S6D7AT0B01'), 1)
            self.assertIn('panel-samsung-s6d7at0b01.o',
                          (root / 'drivers' / 'gpu' / 'drm' / 'panel' /
                           'Makefile').read_text())
            # Now pretend the stanza was written by an older stumper: replace the
            # depends line and re-stage; the tree has to catch up.
            stale = text.replace('depends on OF && DRM_MIPI_DSI', 'depends on OF')
            self.assertNotEqual(stale, text)
            kconfig.write_text(stale)
            staging.stage_panel(root)
            self.assertIn('depends on OF && DRM_MIPI_DSI', kconfig.read_text(),
                          'a stale stanza would build with the old dependencies')

    def test_restore_undoes_the_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.fake_tree(Path(tmp))
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t',
                            'commit', '-qm', 'base'], cwd=root, check=True)
            staging.stage_dts(root)
            staging.stage_panel(root)
            staging.stage_fragment(root)
            self.assertEqual(len(staging.staged_paths()), 7)
            staging.restore(root)
            self.assertEqual(subprocess.run(['git', 'status', '--porcelain'],
                                            cwd=root, capture_output=True,
                                            text=True).stdout.strip(), '',
                             'restore left the tree dirty')

    def test_real_files_are_present(self):
        for path in (DTS, PANEL_DTS, PANEL_C, FRAGMENT):
            self.assertTrue(path.is_file(), f'{path} is missing from the repo')
        wrapper = PANEL_DTS.read_text()
        self.assertIn('#define J4_PANEL 1', wrapper)
        self.assertIn('msm8917-samsung-j4primelte.dts', wrapper)
        base = DTS.read_text()
        self.assertIn('#ifdef J4_PANEL', base)
        self.assertIn('#ifndef J4_PANEL', base)


class StagedKconfig(unittest.TestCase):
    """"select X" plus "depends on X" is a kconfig recursive dependency.

    kconfig then refuses to read the whole tree, so a bad stanza breaks every
    build rather than only the panel one. That is exactly how this stanza failed
    once, which is why the checks are here.
    """

    def test_selects_and_depends_do_not_overlap(self):
        stanza = staging.PANEL_KCONFIG
        selected = set(re.findall(r'\bselect\s+([A-Z0-9_]+)', stanza))
        depended = set(re.findall(r'\bdepends on\s+([A-Z0-9_]+)', stanza))
        self.assertFalse(selected & depended,
                         f'kconfig recursive dependency: {sorted(selected & depended)}')
        self.assertIn('DRM_PANEL_SIMPLE', selected)
        self.assertTrue(stanza.startswith('config DRM_PANEL_SAMSUNG_S6D7AT0B01'))

    def test_stanza_symbols_are_defined_in_the_tree(self):
        """A `depends on` naming nothing is not an error - it is a driver that can
        never be built, because kconfig invents a promptless always-n symbol with
        that name. The staged panel driver depended on MIPI_DSI for a while, and
        this tree calls it DRM_MIPI_DSI; the .config simply had no driver.
        """
        if not (KERNEL_TREE / 'Kconfig').is_file():
            self.skipTest(f'{KERNEL_TREE}: no pinned kernel tree to audit against')
        known = config.known_symbols(KERNEL_TREE)
        self.assertTrue(known, f'{KERNEL_TREE}: no Kconfig files found')
        named = {token
                 for clause in re.findall(r'\b(?:select|depends on|imply)\s+(.*)',
                                          staging.PANEL_KCONFIG)
                 for token in re.findall(r'\b[A-Z][A-Z0-9_]*\b', clause)}
        self.assertTrue(named, 'the stanza has no dependencies to audit')
        self.assertFalse(sorted(named - known),
                         f'undefined symbols in the staged stanza: '
                         f'{sorted(named - known)}')

    def test_audit_runs_against_the_real_fragment(self):
        # Same guard for the fragment: every name it enables, disables or sets has
        # to exist in the pinned tree. A typo here costs a booting kernel that is
        # quietly missing a driver.
        if not (KERNEL_TREE / 'Kconfig').is_file():
            self.skipTest(f'{KERNEL_TREE}: no pinned kernel tree to audit against')
        ops = config.parse_fragment(FRAGMENT, panel=True)
        config.audit_symbols(KERNEL_TREE, ops)          # raises SystemExit if wrong
        with tempfile.TemporaryDirectory() as tmp:
            frag = Path(tmp) / 'bad.fragment'
            frag.write_text('+ THIS_DRIVER_DOES_NOT_EXIST\n')
            with self.assertRaises(SystemExit) as caught:
                config.audit_symbols(KERNEL_TREE, config.parse_fragment(frag))
            self.assertIn('THIS_DRIVER_DOES_NOT_EXIST', str(caught.exception))

    def test_audit_is_a_no_op_without_a_kernel_tree(self):
        # The CI pre-compile pass runs before the kernel is cloned, and a pmOS
        # checkout has no tree at all: the audit must not be the thing that fails.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(config.known_symbols(Path(tmp)))
            config.audit_symbols(Path(tmp), [('enable', 'ANYTHING', None)])

    def test_panel_driver_is_always_staged(self):
        script = (HERE / 'build-mainline.sh').read_text()
        self.assertIn('mainline-kernel.py" "$KERNEL" --panel', script,
                      'M1 must parse the panel Kconfig stanza too, so a broken '
                      'stanza fails every build instead of only M2')
        self.assertIn('mainline-config.py" check', script,
                      'the built .config must be audited, not just produced')
        self.assertIn('*.ko', script, 'a leaked module set has to fail the build')
        self.assertIn('J4_MAINLINE_DTBS_ONLY', script,
                      're-running only the device tree must not re-apply the '
                      'fragment behind the packed Image.gz')


class PanelDriver(unittest.TestCase):
    """The generated driver, against the device tree it has to match."""

    @classmethod
    def setUpClass(cls):
        cls.text = PANEL_C.read_text()
        cls.dts = DTS.read_text()

    def test_init_sequence_is_not_truncated(self):
        on = self.text[self.text.index('s6d7at0b01_on'):]
        on = on[:on.index('static int s6d7at0b01_off')]
        self.assertGreaterEqual(on.count('mipi_dsi_dcs_write_seq_multi'), 25,
                                'the generated init sequence looks cut short')
        self.assertIn('MIPI_DCS_SET_DISPLAY_ON', on,
                      'display-on must be sent separately (samsung,display_on_*)')
        self.assertIn('0x5a, 0x5a', on, 'the 0xF0/0xFC/0x9F page unlocks are gone')

    def test_mode_matches_the_device_tree(self):
        for value in ('720', '1480'):
            self.assertIn(f'.hdisplay = {value}' if value == '720'
                          else f'.vdisplay = {value}', self.text)

    def test_power_supplies_are_all_requested(self):
        # devm_regulator_get() names are the DT property minus "-supply"; a
        # missing one means the power order in the device tree is a lie.
        for name in ('vdd', 'vsp', 'vsn'):
            self.assertIn(f'devm_regulator_get(dev, "{name}")', self.text,
                          f'{name} regulator never requested')
        for name in ('"vci"', '"vddio"'):
            self.assertIn(name, self.text,
                          f'the bulk supply {name} disappeared from the driver')
        self.assertIn('devm_gpiod_get(dev, "reset"', self.text)

    def test_compatible_matches_the_device_tree(self):
        match = re.search(r'\.compatible = "([^"]+)"', self.text)
        self.assertTrue(match, 'no of_match_table compatible?')
        self.assertIn(match.group(1), self.dts,
                      'the driver and the device tree disagree on the compatible')


class DeviceTree(unittest.TestCase):
    """Both milestones, compiled the way kbuild compiles them."""

    @classmethod
    def setUpClass(cls):
        if not (KERNEL_TREE / 'arch' / 'arm64' / 'boot' / 'dts' / 'qcom' /
                'msm8917.dtsi').is_file():
            raise unittest.SkipTest(f'{KERNEL_TREE}: no device tree includes here')
        cls.dir = tempfile.TemporaryDirectory()
        cls.out = Path(cls.dir.name)
        cls.dtc = shutil.which('dtc')
        if not cls.dtc:
            raise unittest.SkipTest('dtc is not installed')

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'dir'):
            cls.dir.cleanup()

    def compile(self, source: str) -> bytes:
        dts_dir = KERNEL_TREE / 'arch' / 'arm64' / 'boot' / 'dts'
        board_dir = dts_dir / 'qcom'
        out = self.out / source.replace('/', '_')
        # cpp exactly like scripts/Makefile.lib, then dtc: the milestone symbol
        # can only be introduced by the preprocessor, which is why the panel
        # variant is a separate .dts instead of a -D flag.
        # Exactly scripts/Makefile.dtbs's dtc_cpp_flags, plus -I of the board
        # directory (kbuild gets that for free: it preprocesses the .dts in place).
        # -undef is not decoration: without it cpp predefines `linux` as 1 and
        # silently rewrites every `linux,code` property into `1,code`.
        proc = subprocess.run(
            ['cpp', '-E', '-nostdinc', '-undef', '-D__DTS__',
             '-I', str(KERNEL_TREE / 'scripts' / 'dtc' / 'include-prefixes'),
             '-I', str(board_dir), '-I', str(dts_dir),
             '-x', 'assembler-with-cpp', str(SRC / source)],
            capture_output=True, text=True)
        if proc.returncode:
            self.fail(f'cpp failed on {source}: {proc.stderr[:600]}')
        pre = out.with_suffix('.dts.tmp')
        pre.write_text(proc.stdout)
        proc = subprocess.run([self.dtc, '-O', 'dtb', '-b', '0', '-o', str(out),
                               '-i', str(board_dir), str(pre),
                               '-Wno-unit_address_vs_reg', '-Wno-simple_bus_reg',
                               '-Wno-node_name_chars'], capture_output=True, text=True)
        if proc.returncode:
            self.fail(f'dtc failed on {source}: {proc.stderr[:600]}')
        return out.read_bytes()

    def decompile(self, blob: bytes) -> str:
        path = self.out / 'flat.dtb'
        path.write_bytes(blob)
        proc = subprocess.run([self.dtc, '-I', 'dtb', '-O', 'dts', str(path)],
                              capture_output=True, text=True, check=True)
        return proc.stdout

    def test_both_variants_compile_and_differ(self):
        m1 = self.decompile(self.compile(staging.DTS))
        panel = self.decompile(self.compile(staging.PANEL_DTS))
        self.assertNotEqual(m1, panel)
        for text in (m1, panel):
            self.assertIn('Samsung Galaxy J4+ (SM-J415F)', text)
            self.assertIn('ramoops', text)
            self.assertIn('samsung,j4primelte', text)
            self.assertIsNone(verify.check_console(text))
        # The gates.
        self.assertNotIn('s6d7at0b01', m1)
        self.assertNotIn('ti,lm3632', m1)
        self.assertNotIn('lcd_extra_power', m1)
        self.assertIn('samsung,s6d7at0b01-pm6003xb1', panel)
        self.assertIn('ti,lm3632', panel)
        self.assertIn('simple-framebuffer', m1)
        self.assertNotIn('simple-framebuffer', panel)

    def test_console_uart_is_enabled_and_first(self):
        text = self.decompile(self.compile(staging.DTS))
        self.assertRegex(text, r'stdout-path\s*=\s*"serial0')
        # In the flattened tree the alias is a path, not a label - dtc drops
        # the label - so matching "&blsp1_uart1" here would only pass on source.
        aliases = re.search(r'aliases \{([^}]*)\}', text, re.S).group(1)
        self.assertIn('serial0 = "/soc@0/serial@', aliases)
        uart = re.search(r'serial@78af000 \{.*?\n\t\};', text, re.S)
        self.assertTrue(uart, 'no console node in the built tree')
        self.assertRegex(uart.group(0), r'status\s*=\s*"okay"')
        # Without -undef, cpp rewrites this property name to "1,code".
        self.assertIn('linux,code', text)
        self.assertNotIn('1,code', text)

    def test_check_console_catches_the_two_mistakes(self):
        self.assertIsNotNone(verify.check_console('chosen {\n\tstdout-path = '
                                                 '"framebuffer0";\n};\n'))
        self.assertIsNotNone(verify.check_console('chosen {\n\tstdout-path = '
                                                  '"serial0";\n};\n'))
        # A disabled uart with a correct stdout-path is still no console at all.
        bad = ('chosen {\n\tstdout-path = "serial0:115200n8";\n};\n\n'
               '/aliases {\n\tserial0 = &uart1;\n};\n\n'
               'uart1: serial@78b3000 {\n\tstatus = "disabled";\n};\n')
        self.assertIn('disabled', verify.check_console(bad))
        # Same tree as dtc prints it: resolved to a path, label gone.
        flat = ('chosen {\n\tstdout-path = "serial0:115200n8";\n};\n\n'
                '/aliases {\n\tserial0 = "/soc@0/serial@78b3000";\n};\n\n'
                'serial@78b3000 {\n\tstatus = "disabled";\n\t};\n')
        problem = verify.check_console(flat)
        self.assertIsNotNone(problem, 'the flattened spelling was accepted blindly')
        self.assertIn('disabled', problem)
        problem = verify.check_console(bad)
        self.assertIsNotNone(problem)
        self.assertIn('disabled', problem)


class VerifyHelpers(unittest.TestCase):
    """The checks that stand on their own next to the build script."""

    def test_gzip_truncation_is_detected(self):
        whole = gzip.compress(b'payload' * 2000)
        self.assertFalse(verify.gzip_is_truncated(whole))
        self.assertTrue(verify.gzip_is_truncated(whole[:-40]))
        self.assertTrue(verify.gzip_is_truncated(b'this is not gzip'))

    def test_appended_payload_must_be_the_built_dtb(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = fake_dtb(2048)
            image = gzip.compress(b'kernel' * 100)
            (root / 'm1-Image.gz-dtb').write_bytes(append_dtb(image, tree))
            (root / 'm1.dtb').write_bytes(tree)
            failures = []
            verify.check_appended(root, 'm1', failures)
            self.assertEqual(failures, [])
            (root / 'm1.dtb').write_bytes(tree + b'!')
            failures = []
            verify.check_appended(root, 'm1', failures)
            self.assertTrue(failures, 'a payload that is not the built DTB passed')

    def test_boot_checks_compare_against_the_donor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = make_boot(cmdline='ro pmos_root_uuid=abc rootwait')
            (root / 'donor.img').write_bytes(base)
            payload = append_dtb(gzip.compress(b'k' * 3000), fake_dtb())
            (root / 'boot').mkdir()
            good = repack.build(repack.BootImage(base), payload)
            (root / 'boot' / 'boot-m1-rootfs.img').write_bytes(bytes(good.data))
            failures = []
            verify.check_boot(root / 'boot', root / 'donor.img', failures)
            self.assertEqual(failures, [])
            # A swapped-out ramdisk is the mistake this exists to catch.
            tampered = bytearray(good.data)
            start = repack.align(len(payload), good.page_size) + good.page_size
            tampered[start:start + 4] = b'DEAD'
            (root / 'boot' / 'boot-m1-rootfs.img').write_bytes(bytes(tampered))
            failures = []
            verify.check_boot(root / 'boot', root / 'donor.img', failures)
            self.assertTrue(any('ramdisk' in f for f in failures), failures)

    def test_missing_donor_is_reported_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'boot').mkdir()
            failures = []
            verify.check_boot(root / 'boot', root / 'nope.img', failures)
            self.assertTrue(failures)


if __name__ == '__main__':
    unittest.main(verbosity=2)
