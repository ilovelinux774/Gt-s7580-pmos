#!/usr/bin/env python3
"""Give the j4primelte console a bind that cannot wedge the panel.

Two separate facts about the pinned 3.18 tree drive this patch, and the first
build that ignored the second one boot-looped a phone:

1. fbcon can only ever be bound through fbcon_startup(). The vendor vt.c calls
   csw->con_startup() from do_register_con_driver() *and* sets
   CON_DRIVER_FLAG_INIT there, so do_bind_con_driver() never runs startup a
   second time; the only other caller is do_fbcon_takeover(), reached from
   fbcon_fb_registered(). When fbcon_startup() returns NULL the consw is
   unregistered again and nothing in userspace can bring the console back --
   that is why /sys/class/vtconsole shows only the dummy device on this board.

2. fbcon_startup() asks the driver for permission with fb_open(), and on this
   board that call is not a permission check at all: mdss_fb_open() runs
   mdss_fb_blank_sub(FB_BLANK_UNBLANK) whenever the framebuffer has no owner
   yet (mfd->ref_cnt == 0, mdss_fb.c:2894) -- which is exactly the situation
   while mdss_fb_probe() is still inside register_framebuffer(). Run #35 made
   fbcon ignore that failure and keep the bind; the console then power-cycled
   a panel that the boot loader had already programmed, and the phone rebooted
   ~25 s into every boot with nothing on the panel but the boot logo.

So the veto must go (otherwise there is no console, fact 1) but the open must
not be replaced by an unguarded bind (that is what bricked the boot, fact 2).
The middle path is what this patch implements:

  * fbcon_startup() keeps its decision logic, but a failing fb_open() is
    downgraded to a log message instead of a NULL return, so fb_con stays
    registered and vtcon1 survives;
  * the actual takeover can be deferred out of the registration path with
    "fbcon=late", which binds at late_initcall() instead -- after
    mdss_fb_probe() is completely finished. That is the same context the bind
    was proven in by hand on the device (panel up, splash still scanning,
    probe long done);
  * the printk is the on-device proof that the patch is live: with
    CONFIG_PRINTK the line lands in console-ramoops even when nothing is
    visible on the panel.

The printk's newline must end up as a two-character escape in the C source
(backslash then n). Runs #35 and #36 doubled it: cat -A on those builds'
kernel-source.patch shows

    +\t\tprintk(KERN_INFO "fbcon: fb_open failed, binding anyway\\\\n");$

so the marker line terminated with a literal backslash-n and merged into the
next kernel message. That line is the on-device proof that the patch is live,
so is_patched() pins each hunk's full replacement text instead of grepping a
loose marker, and test-kernel-fbcon.py asserts the exact escape.
"""
import argparse
import re
import sys
from pathlib import Path

MARKER = 'pmos-j4primelte:'
RELATIVE = Path('drivers') / 'video' / 'console' / 'fbcon.c'

# (old, new, strict) triples, applied in order. Every `old` must appear
# exactly once. `strict` hunks must also be *gone* afterwards: the additive
# hunks keep their anchor line inside the replacement, so asking for its
# absence there would be a contradiction.
HUNKS = [
    # 1. A failing fb_open() may not veto the bind, but binding must not make
    #    the panel power-up path run either. Skip the open; do not module_put,
    #    so the refcount still balances against fbcon_exit().
    (
        "\tif (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
        "\t\tmodule_put(owner);\n"
        "\t\treturn NULL;\n"
        "\t}\n",
        "\tif (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
        "\t\t/* pmos-j4primelte: mdss_fb_open() runs its panel power-up\n"
        "\t\t * (mdss_fb_blank_sub(FB_BLANK_UNBLANK)) whenever no owner\n"
        "\t\t * holds the framebuffer yet, which is exactly the case while\n"
        "\t\t * mdss_fb_probe() is still registering it. Letting that failure\n"
        "\t\t * veto the bind hid the console forever, because\n"
        "\t\t * do_register_con_driver() consumes con_startup() once and\n"
        "\t\t * nothing else ever registers fb_con again. Ignoring the\n"
        "\t\t * failure instead power-cycled a panel the boot loader had\n"
        "\t\t * already programmed and boot-looped the phone (run #35).\n"
        "\t\t * So keep the bind, skip the open: no new owner, and\n"
        "\t\t * mdss_fb_release_all() refuses the matching close while\n"
        "\t\t * ref_cnt is 0 (\"try to close unopened fb\"), so the panel\n"
        "\t\t * power state is left exactly as the boot loader set it. */\n"
        "\t\tprintk(KERN_INFO \"fbcon: fb_open refused, binding without it\\n\");\n"
        "\t}\n",
        True,
    ),
    # 2. Declaration for the new fbcon= option, next to the other globals the
    #    parser writes. Anchored on fbcon_has_console_bind because the line
    #    above it ends in a trailing space in this tree.
    (
        "static int fbcon_has_console_bind;\n",
        "static int fbcon_has_console_bind;\n"
        "static int fbcon_late_bind;\n",
        False,
    ),
    # 3. The option itself, inside the existing fbcon= parser loop.
    (
        "\t\tif (!strncmp(options, \"map:\", 4)) {\n",
        "\t\tif (!strcmp(options, \"late\")) {\n"
        "\t\t\tfbcon_late_bind = 1;\n"
        "\t\t\tcontinue;\n"
        "\t\t}\n"
        "\n"
        "\t\tif (!strncmp(options, \"map:\", 4)) {\n",
        False,
    ),
    # 4. The deferred takeover. fbcon_fb_registered() already left the driver
    #    registered (see hunk 1) and con2fb_map set to info_idx by
    #    do_fbcon_takeover(), so all that is missing is a bind, and
    #    do_take_over_console() tolerates the -EBUSY from the registration
    #    that already happened.
    (
        "module_init(fb_console_init);\n",
        "#ifndef MODULE\n"
        "/* pmos-j4primelte: bind the console once the display driver is\n"
        " * completely up instead of from inside register_framebuffer(), which\n"
        " * is where mdss_fb_open() would still have powered the panel up a\n"
        " * second time. Activated with \"fbcon=late\"; without it this stays\n"
        " * a no-op and the boot behaves like the vendor kernel. */\n"
        "static int __init fbcon_late_takeover(void)\n"
        "{\n"
        "\tif (!fbcon_late_bind)\n"
        "\t\treturn 0;\n"
        "\tconsole_lock();\n"
        "\tif (!con_is_bound(&fb_con))\n"
        "\t\tdo_fbcon_takeover(0);\n"
        "\tconsole_unlock();\n"
        "\treturn 0;\n"
        "}\n"
        "late_initcall(fbcon_late_takeover);\n"
        "#endif /* !MODULE */\n"
        "\n"
        "module_init(fb_console_init);\n",
        False,
    ),
]

# The hunk whose absence must abort the build: it is the one that makes the
# difference between "no console ever" and "console that survives".
PRIMARY = HUNKS[0][0]


def fbcon_path(tree):
    return Path(tree) / RELATIVE


def is_patched(tree):
    """(all hunks present, strict hunks' original text gone)."""
    text = fbcon_path(tree).read_text()
    stamped = all(new in text for _, new, _ in HUNKS)
    clean = not any(old in text for old, _, strict in HUNKS if strict)
    return stamped, clean


def apply(tree):
    """Return the state of the tree after patching. Raises if inapplicable."""
    path = fbcon_path(tree)
    if not path.is_file():
        raise SystemExit('kernel-fbcon: %s missing; is this the pinned tree?' % path)
    text = path.read_text()
    if all(new in text for _, new, _ in HUNKS):
        if any(old in text for old, _, strict in HUNKS if strict):
            raise SystemExit('kernel-fbcon: patched and unpatched hunk both present')
        print('kernel-fbcon: already patched, nothing to do')
        return 'patched'
    if PRIMARY not in text:
        raise SystemExit('kernel-fbcon: the fbcon_startup() hunk this patch '
                         'expects is not in %s; refusing to guess' % path)
    for index, (old, new, _strict) in enumerate(HUNKS):
        found = text.count(old)
        if found != 1:
            raise SystemExit('kernel-fbcon: hunk %d appears %d times, expected 1'
                             % (index + 1, found))
        text = text.replace(old, new, 1)
    # The point of the patch is that fb_con is bound through fbcon_startup();
    # if the consw no longer names it, this helper is patching the wrong thing.
    if not re.search(r'\.con_startup\s*=\s*fbcon_startup', text):
        raise SystemExit('kernel-fbcon: fb_con no longer wires con_startup '
                         'to fbcon_startup; refusing')
    path.write_text(text)
    patched, gone = is_patched(tree)
    if not patched or not gone:
        raise SystemExit('kernel-fbcon: patch did not stick')
    print('kernel-fbcon: fb_open can no longer veto the bind, and "fbcon=late" '
          'defers the takeover to late_initcall()')
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
        patched, gone = is_patched(args.tree)
        print('kernel-fbcon: %s' % ('patched' if patched and gone else 'NOT patched'))
        return 0 if patched and gone else 1
    apply(args.tree)
    return 0


if __name__ == '__main__':
    sys.exit(main())
