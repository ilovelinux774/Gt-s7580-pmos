#!/usr/bin/env python3
"""Defer fbcon's bind on j4primelte until the display driver has finished probing.

Read this before changing anything: the shape of the veto below is the residue of
three wrong theories, and the pstore log of run #39 is what killed the last one.

The vendor veto. ``fbcon_startup()`` asks the driver for permission::

    if (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {
            module_put(owner);
            return NULL;
    }

``mdss_fb_open()`` (mdss_fb.c:2864) runs ``pm_runtime_get_sync()`` and, while
``mfd->ref_cnt`` is 0 -- exactly the case while ``mdss_fb_probe()`` is still
inside ``mdss_fb_register()`` -- ``mdss_fb_blank_sub(FB_BLANK_UNBLANK)``. The
refusal is fatal rather than merely annoying: ``do_register_con_driver()``
(vendor vt.c:3604) calls ``con_startup()`` once and sets CON_DRIVER_FLAG_INIT
there, so after one refusal *nothing* ever registers ``fb_con`` again, and
/sys/class/vtconsole keeps only the dummy device. That is why runs up to #33 had
no way to show boot text.

What each experiment actually did:

  * #35, #36: call ``fb_open()``, ignore its return value. Boot-loop.
  * #37, #38: same, plus ``fbcon=late`` to move the *takeover* to
    late_initcall(). Boot-looped identically, because the takeover that
    ``register_framebuffer()`` triggers from the FB_EVENT_FB_REGISTERED notifier
    was never suppressed in those images -- the gating hunk only appeared in #39.
  * #39: intended "skip the call and suppress the registration takeover". What it
    really shipped was (a) the call still made, because ``fb_open()`` sits in the
    ``if`` *condition* and only the refusal was made conditional, and (b) the
    registration-time takeover left enabled for the default command line, because
    renaming the flag inverted the sense of ``!flag``. So #39 was the first image
    that bound the console *inside* probe with the panel half-initialised -- and
    it died exactly where the others did: console-ramoops of the #39 boot ends at
    ``[ 1.073138 ] mdss_fb_probe: fb0: split_mode:0 left:0 right:0`` with cmdline
    ``fbcon=map:0,scrollback:256k`` (no ``,late``, so the flash had taken), no
    ``Console: switching``, no oops. Silent, i.e. a watchdog reset, which fits a
    deadlock rather than a fault.

Two things that log does *not* say, and I previously claimed it did: it does not
prove ``mdss_fb_open()`` never returned (its success path prints nothing at all,
so a missing marker cannot tell "returned 0" from "hung"), and the final line is
a coarse marker only -- ``mdss_fb_probe()`` prints it a dozen statements before
``mdss_fb_register()`` with no output in between. What it does establish is that
binding the console while the framebuffer is being registered reboots this board,
whatever the veto returns, in every variant tried so far.

The untested shape, and the point of this patch: do not call ``fb_open()`` at all
(its return value is not the problem -- evaluating it is), do not take the console
over during registration, and bind once from ``late_initcall()``, i.e. after
``mdss_fb_probe()`` has run ``mdp.init_fnc()``. That is the closest kernel-side
approximation of the only bind ever proven on this phone, which was done from
userspace while X held fb0 (``FBIOPUT_CON2FBMAP`` -> 0, ``vtcon1 (M)``, ``bind 1``).

Deferred by default, so boot.img carries the fix with no boot argument to
remember. ``fbcon=veto`` restores the vendor behaviour and is what boot-veto.img
ships as the control group; ``fbcon=late`` is still accepted and means "defer",
for the cmdlines #37/#38 used. fbcon_exit()'s close stays harmless:
mdss_fb_release_all() returns -EINVAL while ref_cnt is 0, so no panel power
transition is issued from the console side either way.

Each hunk carries a `probe` -- a string unique in the file whose exact count
proves the edit landed. Do not replace that with "the original text must be gone":
``module_put(owner); return NULL;`` also ends the kzalloc failure path four lines
below the veto, and there is a second ``fbops->fb_open`` call site in
fbcon_pick_variant() (line 704) that must not be touched.

Idempotent: re-running the helper on an already patched tree changes nothing.
"""
import argparse
import re
import sys
from pathlib import Path

MARKER = 'pmos-j4primelte:'
FLAG = 'fbcon_defer_bind'
RELATIVE = Path('drivers') / 'video' / 'console' / 'fbcon.c'

# (old, new, probe) triples, applied in order. Every `old` must appear exactly
# once; after the whole set, every probe must count exactly what it declares.
VETO_ORIGINAL = (
    "\tif (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
    "\t\tmodule_put(owner);\n"
    "\t\treturn NULL;\n"
    "\t}\n"
)

HUNKS = [
    # 1. The gate, on by default: the fix must not depend on a boot argument,
    #    and it lives in the same place as the other parser-written globals.
    (
        "static int fbcon_has_console_bind;\n",
        "static int fbcon_has_console_bind;\n"
        "/* pmos-j4primelte: bind at late_initcall() instead of during\n"
        " * register_framebuffer(); \"fbcon=veto\" puts the vendor behaviour back. */\n"
        "static int " + FLAG + " = 1;\n",
        ('static int ' + FLAG + ' = 1;', 1),
    ),
    # 2. Options, inside the existing fbcon= parser loop. The loop returns 1 for
    #    unknown tokens, so a dropped hunk here means the option is inert -- which
    #    is why build-kernel.sh greps for the token too.
    (
        "\t\tif (!strncmp(options, \"map:\", 4)) {\n",
        "\t\tif (!strcmp(options, \"veto\")) {\n"
        "\t\t\t" + FLAG + " = 0;\n"
        "\t\t\tcontinue;\n"
        "\t\t}\n"
        "\t\tif (!strcmp(options, \"late\")) {\n"
        "\t\t\t" + FLAG + " = 1;\n"
        "\t\t\tcontinue;\n"
        "\t\t}\n"
        "\n"
        "\t\tif (!strncmp(options, \"map:\", 4)) {\n",
        ('!strcmp(options, "veto")', 1),
    ),
    # 3. The veto. The flag branch comes first so that "defer" cannot evaluate
    #    the call at all; the vendor condition is reused verbatim as the else-if
    #    so that "veto" is byte-for-byte the vendor code. #39 instead left the
    #    condition line alone and guarded only the body, so it still called it.
    (
        VETO_ORIGINAL,
        "\t/* pmos-j4primelte: mdss_fb_open() power-cycles the panel while no\n"
        "\t * owner holds the framebuffer, which is the case inside\n"
        "\t * mdss_fb_probe()'s register_framebuffer(), and the bind that the\n"
        "\t * FB_EVENT_FB_REGISTERED notifier runs then reaches mdss_fb_set_par()\n"
        "\t * before mdp.init_fnc() has. Every image that bound from here rebooted\n"
        "\t * silently (console-ramoops of #35-#39 all end at \"mdss_fb_probe:\n"
        "\t * fb0: split_mode\"). So: do not call fb_open() here, and let the\n"
        "\t * deferred takeover below do the bind once probing is over. */\n"
        "\tif (" + FLAG + ") {\n"
        "\t\tprintk(KERN_INFO \"fbcon: fb%i not opened here, bind deferred to late init\\n\", info->node);\n"
        "\t} else if (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
        "\t\tmodule_put(owner);\n"
        "\t\treturn NULL;\n"
        "\t}\n",
        ('fbcon: fb%i not opened here', 1),
    ),
    # 4. Suppress the takeover that fbcon_fb_registered() performs from the
    #    notifier. This is the hunk #37 and #38 were missing.
    (
        "\t\tif (info_idx != -1)\n"
        "\t\t\tret = do_fbcon_takeover(1);\n",
        "\t\tif (info_idx != -1 && !" + FLAG + ")\n"
        "\t\t\tret = do_fbcon_takeover(1);\n",
        ('info_idx != -1 && !' + FLAG, 1),
    ),
    # 5. The deferred bind. fb_con is registered (hunk 3 let startup succeed) and
    #    con2fb_map is already set by the map: override, and do_take_over_console()
    #    tolerates the -EBUSY of that earlier registration, so all that is missing
    #    is the bind. Before init, so the boot messages still land on the panel.
    (
        "module_init(fb_console_init);\n",
        "#ifndef MODULE\n"
        "/* pmos-j4primelte: bind the console once the display driver has\n"
        " * finished probing. No-op with \"fbcon=veto\". */\n"
        "static int __init fbcon_deferred_takeover(void)\n"
        "{\n"
        "\tif (!" + FLAG + ")\n"
        "\t\treturn 0;\n"
        "\tconsole_lock();\n"
        "\tif (!con_is_bound(&fb_con))\n"
        "\t\tdo_fbcon_takeover(0);\n"
        "\tconsole_unlock();\n"
        "\treturn 0;\n"
        "}\n"
        "late_initcall(fbcon_deferred_takeover);\n"
        "#endif /* !MODULE */\n"
        "\n"
        "module_init(fb_console_init);\n",
        ('late_initcall(fbcon_deferred_takeover);', 1),
    ),
]

# fbcon_startup() ends with exactly two refusals in the pinned tree: the veto's
# module_put()/return NULL pair and the identical pair in the kzalloc failure path
# right below it. Neither may move. Two also proves no hunk duplicated a refusal.
REFUSALS_EXPECTED = 2

# One per role: declaration, the two parser assignments, the veto branch, the
# takeover gate, the deferred bind's own guard.
FLAG_REFS_EXPECTED = 6

# Three hunks carry the stamp comment (the gate, the veto, the deferred bind).
MARK_STAMPS = 3

# The hunk whose absence must abort the build.
PRIMARY = VETO_ORIGINAL


def fbcon_path(tree):
    return Path(tree) / RELATIVE


def probes_present(text):
    return all(text.count(needle) == count for _, _, (needle, count) in HUNKS)


def is_patched(tree):
    """(all five replacement texts present and counted right, stamp lines right)."""
    text = fbcon_path(tree).read_text()
    stamped = all(new in text for _, new, _ in HUNKS) and probes_present(text)
    return stamped, text.count(MARKER) == MARK_STAMPS


def apply(tree):
    """Return the state of the tree after patching. Raises if inapplicable."""
    path = fbcon_path(tree)
    if not path.is_file():
        raise SystemExit('kernel-fbcon: %s missing; is this the pinned tree?' % path)
    text = path.read_text()
    opens_before = text.count('fbops->fb_open(info, 0)')
    if all(new in text for _, new, _ in HUNKS):
        if not probes_present(text):
            raise SystemExit('kernel-fbcon: patched text is in place but a probe '
                             'counts wrong; the tree is in no state I recognise')
        print('kernel-fbcon: already patched, nothing to do')
        return 'patched'
    if PRIMARY not in text:
        raise SystemExit('kernel-fbcon: the fbcon_startup() hunk this patch '
                         'expects is not in %s; refusing to guess' % path)
    for index, (old, new, (needle, count)) in enumerate(HUNKS):
        found = text.count(old)
        if found != 1:
            raise SystemExit('kernel-fbcon: hunk %d appears %d times, expected 1'
                             % (index + 1, found))
        text = text.replace(old, new, 1)
    if not re.search(r'\.con_startup\s*=\s*fbcon_startup', text):
        raise SystemExit('kernel-fbcon: fb_con no longer wires con_startup '
                         'to fbcon_startup; refusing')
    # The polarity checks. A build that shipped the opposite sense of any of
    # these is what #39 was, and no string-only test caught it, so the shapes
    # below are asserted as well as the counts.
    refs = text.count(FLAG)
    if refs != FLAG_REFS_EXPECTED:
        raise SystemExit('kernel-fbcon: the defer flag is referenced %d times, '
                         'expected %d; refusing to write' % (refs, FLAG_REFS_EXPECTED))
    if 'static int ' + FLAG + ' = 1;' not in text:
        raise SystemExit('kernel-fbcon: the defer flag is not on by default, which '
                         'would leave boot.img with a dark panel like #33; refusing')
    if '\tif (' + FLAG + ') {\n' not in text:
        raise SystemExit('kernel-fbcon: fb_open() is not behind the defer branch, '
                         'so the call still happens during probe; refusing')
    if text.count('fbops->fb_open(info, 0)') != opens_before:
        raise SystemExit('kernel-fbcon: the number of fb_open() call sites moved '
                         'from %d to %d; this patch adds and removes none'
                         % (opens_before, text.count('fbops->fb_open(info, 0)')))
    if text.count('module_put(owner);') != REFUSALS_EXPECTED:
        raise SystemExit('kernel-fbcon: module_put(owner) now appears %d times, '
                         'expected %d; a hunk swallowed the wrong block'
                         % (text.count('module_put(owner);'), REFUSALS_EXPECTED))
    path.write_text(text)
    stamped, stamped_two = is_patched(tree)
    if not stamped or not stamped_two:
        raise SystemExit('kernel-fbcon: patch did not stick')
    print('kernel-fbcon: fb_open() not called at registration, bind deferred to '
          'late_initcall() ("fbcon=veto" restores the vendor behaviour)')
    return 'patched'


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('tree', help='path to the pinned kernel source tree')
    parser.add_argument('--check', action='store_true',
                        help='only report whether the tree carries the patch')
    args = parser.parse_args()
    if args.check:
        if not fbcon_path(args.tree).is_file():
            print('kernel-fbcon: %s missing' % RELATIVE)
            return 1
        stamped, stamped_two = is_patched(args.tree)
        print('kernel-fbcon: %s' % ('patched' if stamped and stamped_two else 'NOT patched'))
        return 0 if stamped and stamped_two else 1
    apply(args.tree)
    return 0


if __name__ == '__main__':
    sys.exit(main())
