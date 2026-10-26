#!/usr/bin/env python3
"""Tests for ci/j4primelte/kernel-fbcon.py.

The helper rewrites four places in the pinned fbcon.c, so these tests pin both
directions: it must apply to the text the vendor tree really carries, and it
must refuse anything else instead of silently shipping a kernel that either
leaves the panel dark (run #32 and earlier) or power-cycles it mid-probe and
boot-loops the phone (run #35/#36, which is what the deferred-bind hunk exists
to prevent).

Set J4_FBCON_REFERENCE=/path/to/fbcon.c to also check the helper against a
real copy of the pinned source (CI has no kernel tree, so it skips there).
"""
import importlib.util
import os
import re
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
DECL = "static int fbcon_has_console_bind;\n"
MAP = '\t\tif (!strncmp(options, "map:", 4)) {\n'
MODINIT = "module_init(fb_console_init);\n"


def sample_tree_text():
    """A tree that carries every anchor the patch needs, and nothing else."""
    return (
        "before\n" + HUNK + "after\n" + CONSW
        + DECL + "\n" + MAP + "\n" + MODINIT
    )


def make_tree(root, text):
    path = Path(root) / kernel_fbcon.RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class HunkContractTest(unittest.TestCase):
    """The patch data itself: a build that silently lost a hunk is the failure
    mode that put a bootlooping phone back in the user's hands."""

    def test_hunks_are_well_formed(self):
        self.assertEqual(len(kernel_fbcon.HUNKS), 4)
        for index, (old, new, strict) in enumerate(kernel_fbcon.HUNKS, 1):
            self.assertTrue(old, index)
            self.assertNotEqual(old, new, index)
            if strict:
                self.assertNotIn(old, new,
                                 'hunk %d is strict, so its replacement must '
                                 'not still contain the original text' % index)
            else:
                self.assertIn(old, new,
                              'hunk %d is additive: it keeps its anchor line, '
                              'which is what makes it non-strict' % index)
            self.assertEqual(strict, index == 1,
                             'only the veto hunk may demand its original text '
                             'be gone; the other three keep their anchor line')

    def test_the_generated_printk_uses_a_single_backslash_escape(self):
        # Runs #35/#36 doubled it, which made the marker merge into the next
        # kernel message -- and the marker is the only on-device proof.
        _, patched_veto, _ = kernel_fbcon.HUNKS[0]
        literal = re.search(r'printk\(KERN_INFO "([^"]*)"\);', patched_veto)
        self.assertIsNotNone(literal, 'no printk in the veto hunk')
        text = literal.group(1)
        self.assertTrue(text.endswith('\\n'), repr(text))
        self.assertFalse(text.endswith('\\\\n'), 'doubled escape: %r' % text)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_applies_and_reports_patched(self):
        make_tree(self.tmp, sample_tree_text())
        self.assertEqual(kernel_fbcon.apply(self.tmp), 'patched')
        self.assertEqual(kernel_fbcon.is_patched(self.tmp), (True, True))

    def test_the_veto_is_the_only_removed_code(self):
        original = sample_tree_text()
        path = make_tree(self.tmp, original)
        kernel_fbcon.apply(self.tmp)
        text = path.read_text()
        self.assertIn('before\n', text)
        self.assertIn('after\n', text)
        self.assertIn(CONSW, text)
        self.assertIn(DECL, text)
        # module_put(owner) must survive *somewhere* (the !ops path) but must
        # be gone from the veto, and the NULL return with it.
        self.assertNotIn('module_put(owner);\n\t\treturn NULL;\n\t}\n\n\tops = kzalloc',
                         text)
        self.assertEqual(text.count(kernel_fbcon.MARKER), 2,
                         'both narrative hunks must be stamped')

    def test_the_deferred_bind_is_wired(self):
        path = make_tree(self.tmp, sample_tree_text())
        kernel_fbcon.apply(self.tmp)
        text = path.read_text()
        self.assertIn('static int fbcon_late_bind;', text)
        self.assertIn('if (!strcmp(options, "late")) {', text)
        self.assertIn('late_initcall(fbcon_late_takeover);', text)
        # The bind is a no-op unless the boot arg set the flag: a kernel that
        # binds unconditionally is exactly the #35 bootloop.
        body = re.search(r'static int __init fbcon_late_takeover\(void\)\n'
                         r'\{\n\tif \(!fbcon_late_bind\)\n\t\treturn 0;', text)
        self.assertIsNotNone(body, 'late takeover is not gated on the flag')

    def test_idempotent(self):
        path = make_tree(self.tmp, sample_tree_text())
        kernel_fbcon.apply(self.tmp)
        once = path.read_text()
        self.assertEqual(kernel_fbcon.apply(self.tmp), 'patched')
        self.assertEqual(path.read_text(), once)

    def test_check_mode(self):
        make_tree(self.tmp, sample_tree_text())
        self.assertEqual(kernel_fbcon.is_patched(self.tmp), (False, False))
        kernel_fbcon.apply(self.tmp)
        self.assertEqual(kernel_fbcon.is_patched(self.tmp), (True, True))

    def test_partially_patched_tree_is_refused(self):
        # The #35 build carried the veto hunk *without* the deferred-bind
        # hunk, which is precisely the shape that boot-looped a phone. A tree
        # in that state must abort the build, and it must stay untouched so
        # the failure is debuggable from the log.
        text = sample_tree_text().replace(
            HUNK, '/* pmos-j4primelte: veto dropped, no late bind */\n', 1)
        path = make_tree(self.tmp, text)
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('refusing to guess', str(caught.exception))
        self.assertEqual(path.read_text(), text, 'a refused apply must not write')

    def test_the_late_bind_cannot_be_dropped_silently(self):
        # is_patched() demands all four hunks, so a build that lost the
        # boot-loop guard fails -- it does not quietly produce an #35 image.
        path = make_tree(self.tmp, sample_tree_text())
        kernel_fbcon.apply(self.tmp)
        text = path.read_text()
        stripped = text.replace('late_initcall(fbcon_late_takeover);\n', '')
        self.assertNotEqual(stripped, text)
        path.write_text(stripped)
        self.assertEqual(kernel_fbcon.is_patched(self.tmp), (False, True))

    def test_refuses_tree_without_the_hunk(self):
        make_tree(self.tmp, '/* some other fbcon */\n' + CONSW + DECL + MAP + MODINIT)
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('refusing to guess', str(caught.exception))

    def test_refuses_a_second_copy_of_the_veto(self):
        make_tree(self.tmp, sample_tree_text().replace(HUNK, HUNK + HUNK, 1))
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('expected 1', str(caught.exception))

    def test_refuses_when_the_consw_no_longer_uses_fbcon_startup(self):
        make_tree(self.tmp, sample_tree_text().replace(
            CONSW, '\t.con_startup = vgacon_startup,\n', 1))
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('con_startup', str(caught.exception))

    def test_missing_file_is_an_error_not_a_skip(self):
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(Path(self.tmp) / 'nope')
        self.assertIn('missing', str(caught.exception))

    def test_parentheses_stay_balanced(self):
        # This tree's fbcon.c genuinely has unbalanced parentheses inside
        # comments, so only the delta is meaningful.
        path = make_tree(self.tmp, sample_tree_text())
        before = path.read_text()
        kernel_fbcon.apply(self.tmp)
        after = path.read_text()
        self.assertEqual(after.count('{') - after.count('}'),
                         before.count('{') - before.count('}'))
        self.assertEqual(after.count('(') - after.count(')'),
                         before.count('(') - before.count(')'))


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
        for anchor in (DECL, MAP, MODINIT, CONSW):
            self.assertEqual(text.count(anchor), 1,
                             'anchor no longer unique in the pinned source: %r' % anchor)
        make_tree(tmp, text)
        kernel_fbcon.apply(tmp)
        patched = (Path(tmp) / kernel_fbcon.RELATIVE).read_text()
        self.assertNotIn(HUNK, patched)
        self.assertEqual(kernel_fbcon.is_patched(tmp), (True, True))
        # Only the four hunks moved; nothing else was rewritten.
        self.assertEqual(len(patched.splitlines()) - len(text.splitlines()), 37)
        self.assertEqual(
            patched.count('{') - patched.count('}'),
            text.count('{') - text.count('}'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
