#!/usr/bin/env python3
"""Tests for ci/j4primelte/kernel-fbcon.py.

The helper patches exactly one hunk of the pinned fbcon.c, so these tests pin
both directions: it must apply to the hunk the vendor tree really carries, and
it must refuse anything else instead of silently building a kernel that keeps
the panel dark until the display server starts.

Set J4_FBCON_REFERENCE=/path/to/fbcon.c to also check the helper against a
real copy of the pinned source (CI has no kernel tree, so it skips there).
"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('kernel_fbcon', HERE / 'kernel-fbcon.py')
kernel_fbcon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kernel_fbcon)

# Verbatim from drivers/video/console/fbcon.c at the pinned commit: the veto
# that keeps /sys/class/vtconsole/vtcon1 from ever appearing.
HUNK = (
    "\tif (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
    "\t\tmodule_put(owner);\n"
    "\t\treturn NULL;\n"
    "\t}\n"
)
CONSW = "\t.con_startup \t\t= fbcon_startup,\n"


def make_tree(root, text):
    path = Path(root) / kernel_fbcon.RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_applies_and_reports_patched(self):
        make_tree(self.tmp, 'before\n' + HUNK + 'after\n' + CONSW)
        self.assertEqual(kernel_fbcon.apply(self.tmp), 'patched')
        patched, gone = kernel_fbcon.is_patched(self.tmp)
        self.assertTrue(patched)
        self.assertTrue(gone)

    def test_only_the_veto_is_removed(self):
        original = 'before\n' + HUNK + 'after\n' + CONSW
        path = make_tree(self.tmp, original)
        kernel_fbcon.apply(self.tmp)
        text = path.read_text()
        # Everything that is not the patched hunk must survive untouched.
        self.assertIn('before\n', text)
        self.assertIn('after\n', text)
        self.assertIn(CONSW, text)
        self.assertNotIn('return NULL;\n\t}\n\n\tops = kzalloc', text)
        self.assertEqual(text.count(kernel_fbcon.MARKER), 1)
        # Balanced C: the helper replaces a block, it does not grow one.
        self.assertEqual(text.count('{') - text.count('}'),
                         original.count('{') - original.count('}'))

    def test_idempotent(self):
        path = make_tree(self.tmp, HUNK + CONSW)
        kernel_fbcon.apply(self.tmp)
        once = path.read_text()
        self.assertEqual(kernel_fbcon.apply(self.tmp), 'patched')
        self.assertEqual(path.read_text(), once)

    def test_check_mode(self):
        make_tree(self.tmp, HUNK + CONSW)
        self.assertEqual(kernel_fbcon.is_patched(self.tmp), (False, False))
        kernel_fbcon.apply(self.tmp)
        self.assertEqual(kernel_fbcon.is_patched(self.tmp), (True, True))

    def test_refuses_tree_without_the_hunk(self):
        make_tree(self.tmp, '/* some other fbcon */\n' + CONSW)
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('refusing to guess', str(caught.exception))

    def test_refuses_a_second_copy_of_the_hunk(self):
        make_tree(self.tmp, HUNK + HUNK + CONSW)
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('expected 1', str(caught.exception))

    def test_refuses_when_the_consw_no_longer_uses_fbcon_startup(self):
        make_tree(self.tmp, HUNK + '\t.con_startup = vgacon_startup,\n')
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('con_startup', str(caught.exception))

    def test_missing_file_is_an_error_not_a_skip(self):
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(Path(self.tmp) / 'nope')
        self.assertIn('missing', str(caught.exception))


@unittest.skipUnless(os.environ.get('J4_FBCON_REFERENCE'),
                     'no reference fbcon.c (set J4_FBCON_REFERENCE)')
class AgainstPinnedSourceTest(unittest.TestCase):
    """Run the real helper against a real copy of the pinned fbcon.c."""

    def test_reference_tree(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        reference = Path(os.environ['J4_FBCON_REFERENCE'])
        text = reference.read_text()
        self.assertIn(HUNK, text, 'the pinned fbcon.c no longer has the veto hunk')
        make_tree(tmp, text)
        kernel_fbcon.apply(tmp)
        patched = (Path(tmp) / kernel_fbcon.RELATIVE).read_text()
        self.assertNotIn(HUNK, patched)
        # One hunk in, nothing else moved: same line count apart from the patch.
        self.assertLess(len(patched.splitlines()) - len(text.splitlines()), 15)


if __name__ == '__main__':
    unittest.main(verbosity=2)
