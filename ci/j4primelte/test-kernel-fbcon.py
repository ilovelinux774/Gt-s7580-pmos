#!/usr/bin/env python3
"""Tests for ci/j4primelte/kernel-fbcon.py.

The helper rewrites five places in the pinned fbcon.c, so these tests pin both
directions: it must apply to the text the vendor tree really carries, and it
must refuse anything else instead of silently shipping a kernel that either
leaves the panel dark (runs up to #33) or power-cycles the display from inside
its own probe and boot-loops the phone (runs #35-#38).

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
    """A tree that carries every anchor the patch needs, and nothing else.

    The kzalloc failure path right after the veto is part of the fixture on
    purpose: it is what makes a "the original text must be gone afterwards"
    check wrong, and the reason the helper counts probes instead.
    """
    return ("before\n" + VETO + KZALLOC_TAIL + "after\n" + CONSW + DECL + "\n"
            + MAP + "\n" + TAKEOVER + "\n" + MODINIT)


def make_tree(root, text):
    path = Path(root) / kernel_fbcon.RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class HunkContractTest(unittest.TestCase):
    """The patch data itself. A build that silently lost one of the five hunks
    is what put a boot-looping phone back in the user's hands, so the shape of
    the table is asserted before any behaviour is."""

    def test_hunks_are_well_formed(self):
        self.assertEqual(len(kernel_fbcon.HUNKS), 5)
        for index, (old, new, probe) in enumerate(kernel_fbcon.HUNKS, 1):
            self.assertTrue(old, index)
            self.assertNotEqual(old, new, index)
            needle, count = probe
            self.assertTrue(needle, index)
            self.assertGreaterEqual(count, 1, index)
            # A probe is only a proof if it is not already in the tree the hunks
            # start from, and if the replacement really carries it.
            self.assertIn(needle, new,
                          'hunk %d replaces its own probe away' % index)
            self.assertNotIn(needle, sample_tree_text(),
                             'hunk %d probe is not unique to the patched tree'
                             % index)
            # An absence check would be a lie when the replacement keeps the
            # original text as a substring: that is how the veto's
            # module_put()/return NULL pair also matched the kzalloc path.
            if old in new:
                self.assertNotIn('module_put', old,
                                'hunk %d keeps its original text; only a probe '
                                'may verify it' % index)

    def test_the_veto_skips_the_open_instead_of_tolerating_it(self):
        # Runs #35-#38 still called fb_open() and threw away its return value;
        # the pstore log shows the call never returned. So fb_open() may appear
        # exactly once in the whole replacement: in the untouched condition line.
        veto_new = kernel_fbcon.HUNKS[0][1]
        self.assertEqual(veto_new.count('fb_open(info, 0)'), 1, veto_new)
        self.assertNotIn('binding anyway', veto_new)
        self.assertIn('skipping it and binding at late init', veto_new)
        # Polarity. The refusal must hang off `if (fbcon_late_refusal)`'s TRUE
        # side, i.e. it must be the opt-in, never the default.
        # by the flag; an `else` here would refuse the bind on every *successful*
        # open (the vendor block ends without a return), i.e. it would compile,
        # boot, and simply never show anything -- exactly the #32 bug again.
        self.assertNotIn('\t} else {', veto_new)
        self.assertIn('\t\tif (fbcon_late_refusal) {\n'
                      '\t\t\tmodule_put(owner);\n'
                      '\t\t\treturn NULL;\n'
                      '\t\t}\n\t}\n', veto_new)
        self.assertTrue(veto_new.endswith('\t}\n'), repr(veto_new[-40:]))

    def test_the_deferred_bind_is_not_gated_on_the_boot_arg(self):
        # The fix must be persistent: if the late takeover were behind "fbcon=late"
        # too, boot.img would still ship a dark panel and nothing would be fixed.
        late_new = kernel_fbcon.HUNKS[4][1]
        self.assertIn('late_initcall(fbcon_bind_late_takeover);', late_new)
        self.assertNotIn('fbcon_late_refusal', late_new)
        # ",late" only re-enables the *registration-time* takeover.
        self.assertIn('info_idx != -1 && !fbcon_late_refusal', kernel_fbcon.HUNKS[1][1])

    def test_the_generated_printk_uses_a_single_backslash_escape(self):
        # The doubled escape shipped in #35/#36 merged the marker into the next
        # kernel message, and the marker is the only on-device proof.
        _, patched_veto, _ = kernel_fbcon.HUNKS[0]
        literal = re.search(r'printk\(KERN_INFO "([^"]*)"', patched_veto)
        self.assertIsNotNone(literal, 'no printk in the veto hunk')
        text = literal.group(1)
        self.assertTrue(text.endswith('\\n'), repr(text))
        self.assertFalse(text.endswith('\\\\n'), 'doubled escape: %r' % text)

    def test_probes_count_right_on_the_patched_sample(self):
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

    def test_every_anchor_survives_besides_the_veto(self):
        path = make_tree(self.tmp, sample_tree_text())
        kernel_fbcon.apply(self.tmp)
        text = path.read_text()
        self.assertIn('before\n', text)
        self.assertIn('after\n', text)
        self.assertIn(CONSW, text)
        self.assertIn(DECL, text)
        self.assertIn(KZALLOC_TAIL, text, 'the kzalloc path must be untouched')
        self.assertEqual(text.count('module_put(owner);'),
                         kernel_fbcon.OPENS_EXPECTED + 1)
        self.assertEqual(text.count(kernel_fbcon.MARKER), 2,
                         'both narrative hunks must be stamped')

    def test_patched_tree_keeps_every_gate_reference(self):
        path = make_tree(self.tmp, sample_tree_text())
        kernel_fbcon.apply(self.tmp)
        text = path.read_text()
        # declaration + parser + takeover gate + veto refusal
        self.assertEqual(text.count('fbcon_late_refusal'), 4, text.count('fbcon_late_refusal'))
        self.assertEqual(text.count('late_initcall(fbcon_bind_late_takeover);'), 1)

    def test_refuses_a_tree_that_only_replaced_the_veto(self):
        # Half-landed edits (the #35-#38 shape: veto rewritten, nothing else)
        # must never be patched further; the PRIMARY anchor is gone, so refuse.
        stripped = sample_tree_text().replace(VETO, kernel_fbcon.HUNKS[0][1], 1)
        path = make_tree(self.tmp, stripped)
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('refusing to guess', str(caught.exception))
        self.assertEqual(path.read_text(), stripped, 'a refused apply must not write')

    def test_refuses_to_write_a_patch_without_the_deferred_bind(self):
        # The gate count catches a build that lost the late function: without it
        # the console is never bound at all, which is #32's dark panel again.
        saved = kernel_fbcon.HUNKS[4]
        self.addCleanup(kernel_fbcon.HUNKS.__setitem__, 4, saved)
        kernel_fbcon.HUNKS[4] = (saved[0], 'module_init(fb_console_init);\n', saved[2])
        make_tree(self.tmp, sample_tree_text())
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        # the final verification, not the reference count, is what catches this
        # particular drop; the count guard covers the parser/declaration ones
        self.assertIn('did not stick', str(caught.exception))

    def test_refuses_a_tree_missing_the_parser(self):
        saved = kernel_fbcon.HUNKS[3]
        self.addCleanup(kernel_fbcon.HUNKS.__setitem__, 3, saved)
        kernel_fbcon.HUNKS[3] = (saved[0], saved[0], saved[2])   # option never parsed
        make_tree(self.tmp, sample_tree_text())
        with self.assertRaises(SystemExit) as caught:
            kernel_fbcon.apply(self.tmp)
        self.assertIn('expected at least 4', str(caught.exception))

    def test_a_half_declared_tree_is_not_reported_patched(self):
        # The --check gate in build-kernel.sh is the only thing between a broken
        # hunk set and a phone that will not boot, so "is this tree patched" must
        # be exact: a stray declaration on its own is not the patch.
        mixed = sample_tree_text().replace(DECL, DECL + 'static int fbcon_late_refusal;\n', 1)
        make_tree(self.tmp, mixed)
        self.assertEqual(kernel_fbcon.is_patched(self.tmp), (False, False))

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
        # If a hunk ever swallows the block below the veto, module_put() count
        # drops and the veto branch silently changes meaning.
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
        reference = Path(os.environ['J4_FBCON_REFERENCE'])
        text = reference.read_text()
        self.assertIn(VETO, text, 'the pinned fbcon.c no longer has the veto hunk')
        for anchor in (DECL, MAP, TAKEOVER, MODINIT, CONSW):
            self.assertEqual(text.count(anchor), 1,
                             'anchor no longer unique in the pinned source: %r' % anchor)
        # The two refusals this patch reasons about, and nothing else.
        self.assertEqual(text.count('module_put(owner);'),
                         kernel_fbcon.OPENS_EXPECTED + 1,
                         'the pinned file no longer has the veto and the kzalloc '
                         'refusal in fbcon_startup()')
        make_tree(tmp, text)
        kernel_fbcon.apply(tmp)
        patched = (Path(tmp) / kernel_fbcon.RELATIVE).read_text()
        self.assertEqual(kernel_fbcon.is_patched(tmp), (True, True))
        # Five hunks in, nothing else moved. This number is deliberately exact:
        # it is the only cheap check that the helper did not also rewrite text
        # it does not own.
        self.assertEqual(len(patched.splitlines()) - len(text.splitlines()), 37)
        self.assertEqual(patched.count('{') - patched.count('}'),
                         text.count('{') - text.count('}'))
        self.assertEqual(patched.count('fbops->fb_open(info, 0)'),
                         text.count('fbops->fb_open(info, 0)'),
                         'fb_open() call sites must survive untouched')
        # And the escape in the marker is the good one, in the real file.
        line = [l for l in patched.splitlines() if 'skipping it and binding' in l][0]
        self.assertEqual(line.count('\\'), 1, line)


if __name__ == '__main__':
    unittest.main(verbosity=2)
