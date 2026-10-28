#!/usr/bin/env python3
"""Tests for ci/j4primelte/kernel-fbcon.py.

The helper rewrites five places in the pinned fbcon.c. These tests exist because
run #39 shipped a patch whose *comments* described the right design and whose code
did the opposite: fb_open() sat in the `if` condition so it was still called during
probe, and a flag rename left the registration-time takeover enabled for the
default command line. String-matching tests passed. So the assertions here are
about reachability and polarity, not just about the presence of text.

Set J4_FBCON_REFERENCE=/path/to/fbcon.c to also run the helper against a real copy
of the pinned source (CI has no kernel tree, so it skips there).
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

FLAG = kernel_fbcon.FLAG
# Verbatim from drivers/video/console/fbcon.c at the pinned commit.
VETO = (
    "\tif (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
    "\t\tmodule_put(owner);\n"
    "\t\treturn NULL;\n"
    "\t}\n"
)
KZALLOC_TAIL = ("\tops = kzalloc(sizeof(struct fbcon_ops), GFP_KERNEL);\n"
                "\tif (!ops) {\n\t\tmodule_put(owner);\n\t\treturn NULL;\n\t}\n")
CONSW = "\t.con_startup \t\t= fbcon_startup,\n"
DECL = "static int fbcon_has_console_bind;\n"
MAP = '\t\tif (!strncmp(options, "map:", 4)) {\n'
TAKEOVER = ("\t\tif (info_idx != -1)\n"
            "\t\t\tret = do_fbcon_takeover(1);\n")
MODINIT = "module_init(fb_console_init);\n"


def sample_tree_text():
    """A tree carrying every anchor the patch needs, and nothing else.

    The kzalloc failure path below the veto is part of the fixture on purpose: it
    is what makes "the original text must be gone afterwards" the wrong check.
    """
    return ("before\n" + VETO + KZALLOC_TAIL + "after\n" + CONSW + DECL + "\n"
            + MAP + "\n" + TAKEOVER + "\n" + MODINIT)


def make_tree(root, text):
    path = Path(root) / kernel_fbcon.RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def hunk(index):
    return kernel_fbcon.HUNKS[index][1]


class PolarityTest(unittest.TestCase):
    """The properties whose absence made #39 a bootloop."""

    def test_the_defer_branch_cannot_evaluate_fb_open(self):
        veto_new = hunk(2)
        # The flag branch has to come FIRST, and the call has to live in the
        # `else if`, so "defer" short-circuits past it. #39 kept the vendor
        # condition line intact and guarded only the body, i.e. still called it.
        # The flag branch must be evaluated before the vendor condition is.
        self.assertLess(veto_new.index("\tif (" + FLAG + ") {\n"),
                        veto_new.index('info->fbops->fb_open(info, 0)'),
                        'fb_open() is reachable before the defer branch')
        self.assertIn("\t} else if (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
                      "\t\tmodule_put(owner);\n\t\treturn NULL;\n\t}\n", veto_new)
        self.assertNotIn("\tif (info->fbops->fb_open", veto_new)
        self.assertEqual(veto_new.count('fb_open(info, 0)'), 1, veto_new)

    def test_defer_is_on_by_default(self):
        # Without this, boot.img ships a dark panel like #33 and the "fix" only
        # exists in an image nobody flashes.
        self.assertIn('static int ' + FLAG + ' = 1;', hunk(0))
        self.assertNotIn('static int ' + FLAG + ' = 0;', hunk(0))

    def test_registration_takeover_is_suppressed_when_deferred(self):
        # The hunk #37 and #38 were missing; without it the bind happens mid-probe
        # no matter what the veto does.
        self.assertIn('info_idx != -1 && !' + FLAG, hunk(3))

    def test_deferred_bind_runs_without_a_boot_argument(self):
        late_new = hunk(4)
        self.assertIn('late_initcall(fbcon_deferred_takeover);', late_new)
        self.assertIn('if (!' + FLAG + ')\n\t\treturn 0;', late_new)  # only "veto" skips
        self.assertNotIn('if (' + FLAG + ')\n\t\treturn 0;', late_new)

    def test_the_parser_understands_both_tokens(self):
        parser = hunk(1)
        self.assertIn('if (!strcmp(options, "veto")) {\n\t\t\t' + FLAG + ' = 0;\n', parser)
        self.assertIn('if (!strcmp(options, "late")) {\n\t\t\t' + FLAG + ' = 1;\n', parser)

    def test_the_generated_printk_uses_a_single_backslash_escape(self):
        # The doubled escape shipped in #35/#36 merged the marker into the next
        # message; the marker is the only on-device proof of the path taken.
        literal = re.search(r'printk\(KERN_INFO "([^"]*)"', hunk(2))
        self.assertIsNotNone(literal, 'no printk in the veto hunk')
        text = literal.group(1)
        self.assertTrue(text.endswith('\\n'), repr(text))
        self.assertFalse(text.endswith('\\\\n'), 'doubled escape: %r' % text)


class HunkContractTest(unittest.TestCase):
    def test_hunks_are_well_formed(self):
        self.assertEqual(len(kernel_fbcon.HUNKS), 5)
        seen = set()
        for index, (old, new, (needle, count)) in enumerate(kernel_fbcon.HUNKS, 1):
            self.assertTrue(old, index)
            self.assertNotEqual(old, new, index)
            self.assertTrue(needle, index)
            self.assertGreaterEqual(count, 1, index)
            self.assertIn(needle, new, 'hunk %d replaces its own probe away' % index)
            self.assertNotIn(needle, sample_tree_text(),
                             'hunk %d probe is not unique to a patched tree' % index)
            self.assertNotIn(needle, seen, 'hunk %d probe is not distinct' % index)
            seen.add(needle)
        self.assertEqual(kernel_fbcon.MARK_STAMPS,
                         sum(new.count(kernel_fbcon.MARKER) for _, new, _ in kernel_fbcon.HUNKS))

    def test_probes_count_right_after_applying(self):
        text = sample_tree_text()
        for old, new, (needle, count) in kernel_fbcon.HUNKS:
            self.assertEqual(text.count(old), 1, needle)
            self.assertEqual(new.count(needle), count, needle)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_applies_and_reports_patched(self):
        make_tree(self.tmp, sample_tree_text())
        self.assertEqual(kernel_fbcon.apply(self.tmp), 'patched')
        self.assertEqual(kernel_fbcon.is_patched(self.tmp), (True, True))

    def test_every_other_anchor_survives(self):
        path = make_tree(self.tmp, sample_tree_text())
        kernel_fbcon.apply(self.tmp)
        text = path.read_text()
        self.assertIn('before\n', text)
        self.assertIn('after\n', text)
        self.assertIn(CONSW, text)
        self.assertIn(DECL, text)
        self.assertIn(KZALLOC_TAIL, text, 'the kzalloc path must be untouched')
        self.assertEqual(text.count('module_put(owner);'), kernel_fbcon.REFUSALS_EXPECTED)
        self.assertEqual(text.count(kernel_fbcon.MARKER), kernel_fbcon.MARK_STAMPS)
        self.assertEqual(text.count(FLAG), kernel_fbcon.FLAG_REFS_EXPECTED)

    def test_refuses_a_veto_that_still_calls_fb_open(self):
        # The exact #39 defect, injected: guard present, call still first.
        saved = kernel_fbcon.HUNKS[2]
        self.addCleanup(kernel_fbcon.HUNKS.__setitem__, 2, saved)
        kernel_fbcon.HUNKS[2] = (
            VETO,
            "\tif (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
            "\t\tif (!" + FLAG + ") {\n\t\t\tmodule_put(owner);\n\t\t\treturn NULL;\n\t\t}\n"
            "\t}\n",
            saved[2],
        )
        make_tree(self.tmp, sample_tree_text())
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('behind the defer branch', str(caught.exception))

    def test_refuses_a_flag_that_defaults_off(self):
        saved = kernel_fbcon.HUNKS[0]
        self.addCleanup(kernel_fbcon.HUNKS.__setitem__, 0, saved)
        kernel_fbcon.HUNKS[0] = (saved[0],
                                 DECL + "static int " + FLAG + ";\n", saved[2])
        make_tree(self.tmp, sample_tree_text())
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('not on by default', str(caught.exception))

    def test_refuses_a_dropped_hunk(self):
        saved = kernel_fbcon.HUNKS[4]
        self.addCleanup(kernel_fbcon.HUNKS.__setitem__, 4, saved)
        kernel_fbcon.HUNKS[4] = (saved[0], 'module_init(fb_console_init);\n', saved[2])
        path = make_tree(self.tmp, sample_tree_text())
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('refusing to write', str(caught.exception))
        # and nothing was written for the refused case
        self.assertEqual(path.read_text(), sample_tree_text())

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

    def test_refuses_tree_without_the_hunk(self):
        make_tree(self.tmp, '/* some other fbcon */\n' + CONSW + DECL + MAP
                  + TAKEOVER + MODINIT)
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('refusing to guess', str(caught.exception))

    def test_refuses_a_half_patched_tree(self):
        # Veto already rewritten and nothing else: PRIMARY is gone, so the rest
        # must not be guessed in.
        text = sample_tree_text().replace(VETO, hunk(2), 1)
        path = make_tree(self.tmp, text)
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('refusing to guess', str(caught.exception))
        self.assertEqual(path.read_text(), text, 'a refused apply must not write')

    def test_refuses_a_second_copy_of_the_veto(self):
        make_tree(self.tmp, sample_tree_text().replace(VETO, VETO + VETO, 1))
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('expected 1', str(caught.exception))

    def test_refuses_when_the_consw_no_longer_uses_fbcon_startup(self):
        make_tree(self.tmp, sample_tree_text().replace(
            CONSW, '\t.con_startup = vgacon_startup,\n', 1))
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('con_startup', str(caught.exception))

    def test_refuses_a_tree_that_lost_the_kzalloc_guard(self):
        # The fixture carries that path (the real file has two refusals and no
        # more), so deleting it is what trips this guard -- not the pinned tree.
        broken = sample_tree_text().replace(KZALLOC_TAIL, 'ops = kzalloc(1);\n', 1)
        make_tree(self.tmp, broken)
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('module_put', str(caught.exception))

    def test_missing_file_is_an_error_not_a_skip(self):
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(Path(self.tmp) / 'nope')
        self.assertIn('missing', str(caught.exception))

    def test_braces_and_parentheses_stay_balanced(self):
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
        text = Path(os.environ['J4_FBCON_REFERENCE']).read_text()
        self.assertIn(VETO, text, 'the pinned fbcon.c no longer has the veto hunk')
        for anchor in (DECL, MAP, TAKEOVER, MODINIT, CONSW):
            self.assertEqual(text.count(anchor), 1,
                             'anchor no longer unique in the pinned source: %r' % anchor)
        self.assertEqual(text.count('module_put(owner);'), kernel_fbcon.REFUSALS_EXPECTED)
        make_tree(tmp, text)
        kernel_fbcon.apply(tmp)
        patched = (Path(tmp) / kernel_fbcon.RELATIVE).read_text()
        self.assertEqual(kernel_fbcon.is_patched(tmp), (True, True))
        # Five hunks in, nothing else moved. Exact on purpose: it is the only
        # cheap check that the helper did not rewrite text it does not own.
        self.assertEqual(len(patched.splitlines()) - len(text.splitlines()), 38)
        self.assertEqual(patched.count('{') - patched.count('}'),
                         text.count('{') - text.count('}'))
        self.assertEqual(patched.count('fbops->fb_open(info, 0)'),
                         text.count('fbops->fb_open(info, 0)'),
                         'fb_open() call sites must survive untouched')
        line = [l for l in patched.splitlines() if 'not opened here' in l][0]
        self.assertEqual(line.count('\\'), 1, line)


if __name__ == '__main__':
    unittest.main(verbosity=2)
