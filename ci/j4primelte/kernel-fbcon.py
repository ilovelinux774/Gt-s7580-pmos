#!/usr/bin/env python3
"""Make fbcon bindable on j4primelte, and bind it only after the panel probed.

Everything here comes from reading the pinned tree, plus one boot-looped phone
and one pstore log that finally said what actually happened.

The vendor veto. ``fbcon_startup()`` asks the driver for permission::

    if (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {
            module_put(owner);
            return NULL;
    }

``mdss_fb_open()`` (mdss_fb.c:2864) runs ``pm_runtime_get_sync()`` and then
``mdss_fb_blank_sub(FB_BLANK_UNBLANK)`` whenever ``mfd->ref_cnt`` is 0
(mdss_fb.c:2894) -- which is precisely the case while ``mdss_fb_probe()`` is
still inside ``register_framebuffer()``. So the veto protects the kernel from a
panel power-up in the middle of its own probe, and the console pays for it:
``do_register_con_driver()`` (vendor vt.c:3604) consumes ``con_startup()`` once
and sets CON_DRIVER_FLAG_INIT there, so after one refusal *nothing* ever
registers ``fb_con`` again -- no userspace action can bind the console, and
/sys/class/vtconsole keeps only the dummy device.

The failed fix (runs #35-#38). ``console-ramoops`` of a boot-looped LATEBIND
boot ends mid-probe::

    [    1.042290] mdss_register_panel: adding framebuffer device soc:qcom,mdss_wb_panel
    [    1.043259] mdss_fb_probe: fb0: split_mode:0 left:0 right:0
    <no further record, no oops, no panic>

with ``fbcon=map:0,scrollback:256k,late`` on its own command line, and with no
"fbcon: fb_open refused" marker and no "Console: switching" line anywhere in the
buffer. That pins the death to the takeover ``register_framebuffer()`` triggers
(fbmem.c:1695 takes console_lock() and notifies fbcon) and, inside it, to the
``fb_open()`` call: the marker sat *after* that call, so its absence means
``mdss_fb_open()`` never returned. "fbcon=late" itself cannot be what killed it
-- the boot arg only moves *when* the bind is taken over -- so the earlier
conclusion "binding from an initcall is fatal by itself" is not supported by
this log; the unconditional open is, and #35 through #38 all died the same way.

What this patch therefore does:

  * the failing ``fb_open()`` is *skipped*, not merely ignored, so mdss_fb_open()
    is never entered from the registration path; fbcon_startup() then returns
    normally and fb_con stays registered;
  * the takeover that ``fbcon_fb_registered()`` performs at registration time is
    suppressed by default, and done instead at late_initcall(), after
    mdss_fb_probe() has returned. That is the persistent fix and it needs no
    boot argument; "fbcon=late" selects the *old* behaviour (open called, bind
    refused) and is what boot-latebind.img now carries, as a control group;
  * the late bind is still not a proven-safe takeover: at late_initcall nobody
    owns fb0, so an fb_open() there would run the same blank path. The only bind
    ever proven on this board ran while X held fb0 (ref_cnt != 0, blank path
    skipped). Calling fbcon_init()/mdss_fb_set_par() on a panel the boot loader
    handed over is the open question this run has to answer;
  * fbcon_exit()'s close is harmless in the skip case: mdss_fb_release_all()
    returns -EINVAL while ref_cnt is 0 ("try to close unopened fb %d"), so no
    panel power transition is issued from the console side either way.

Careful with "vendor text is gone" checks: ``module_put(owner); return NULL;``
also appears in the kzalloc failure path four lines below, and there is a second
``fbops->fb_open`` call site in fbcon_pick_variant() at line 704 that must NOT be
touched. So each hunk carries a `probe` instead -- a string unique in the whole
file whose exact occurrence count proves the edit landed.

The printk next to the skip is the on-device proof that a bind really went
through this path -- it can only be reached after fb_open() returned an error --
which is why it must end its line properly (runs #35/#36 emitted a doubled
escape and merged the message into the next one).

Idempotent: re-running the helper on an already patched tree changes nothing.
"""
import argparse
import re
import sys
from pathlib import Path

MARKER = 'pmos-j4primelte:'
RELATIVE = Path('drivers') / 'video' / 'console' / 'fbcon.c'

# (old, new, probe) triples, applied in order. Every `old` must appear exactly
# once before and exactly `probe.count` times after; `new` must contain `old`
# whole or not at all -- it may not leave `old` sitting inside itself as a
# substring, which would make an absence check meaningless (that is how the
# module_put()/return NULL pair from the veto reached the kzalloc path).
VETO_ORIGINAL = (
    "\tif (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
    "\t\tmodule_put(owner);\n"
    "\t\treturn NULL;\n"
    "\t}\n"
)

HUNKS = [
    # 1. The veto, skipped by default. "fbcon=late" re-enables it via hunk 2,
    #    which is what the previous images did and what the log convicted.
    (
        VETO_ORIGINAL,
        "\tif (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
        "\t\t/* pmos-j4primelte: mdss_fb_open() power-cycles the panel while\n"
        "\t\t * no owner holds the framebuffer, which is exactly the case\n"
        "\t\t * inside mdss_fb_probe()'s register_framebuffer(); calling it\n"
        "\t\t * from here hung the board mid-probe (console-ramoops of the\n"
        "\t\t * boot-looped run ends at \"mdss_fb_probe: fb0: split_mode\", with\n"
        "\t\t * no oops and no marker line, i.e. the call never returned).\n"
        "\t\t * Skip it and take the console over at late_initcall() below.\n"
        "\t\t */\n"
        "\t\tprintk(KERN_INFO \"fbcon: fb%i open refused by driver, skipping it and binding at late init\\n\", info->node);\n"
        "\t\t/* \"fbcon=late\" keeps the vendor refusal, i.e. no bind at all;\n"
        "\t\t * see the gate in fbcon_fb_registered(), where the flag lives;\n"
        "\t\t * without \"late\" the block falls through and fb_con registers. */\n"
        "\t\tif (fbcon_late_refusal) {\n"
        "\t\t\tmodule_put(owner);\n"
        "\t\t\treturn NULL;\n"
        "\t\t}\n"
        "\t}\n",
        ('fbcon: fb%i open refused by driver', 1),
    ),
    # 2. "fbcon=late" is what keeps the old, fatal path reachable: with it,
    #    fbcon_fb_registered() still takes over from the FB_EVENT_FB_REGISTERED
    #    notifier, i.e. inside mdss_fb_probe(). Without it, that takeover is
    #    suppressed and only the deferred one runs.
    (
        "\t\tif (info_idx != -1)\n"
        "\t\t\tret = do_fbcon_takeover(1);\n",
        "\t\tif (info_idx != -1 && !fbcon_late_refusal)\n"
        "\t\t\tret = do_fbcon_takeover(1);\n",
        ('info_idx != -1 && !fbcon_late_refusal', 1),
    ),
    # 3. Declaration for the new fbcon= option, next to the other globals the
    #    parser writes. Anchored on fbcon_has_console_bind because the line
    #    above it ends in a trailing space in this tree.
    (
        "static int fbcon_has_console_bind;\n",
        "static int fbcon_has_console_bind;\n"
        "static int fbcon_late_refusal;\n",
        ('static int fbcon_late_refusal;', 1),
    ),
    # 4. The option, inside the existing fbcon= parser loop (the loop returns
    #    1 for unknown tokens, so the option is inert if this hunk is ever
    #    dropped -- see the build guard in build-kernel.sh).
    (
        "\t\tif (!strncmp(options, \"map:\", 4)) {\n",
        "\t\tif (!strcmp(options, \"late\")) {\n"
        "\t\t\tfbcon_late_refusal = 1;\n"
        "\t\t\tcontinue;\n"
        "\t\t}\n"
        "\n"
        "\t\tif (!strncmp(options, \"map:\", 4)) {\n",
        ('!strcmp(options, "late")', 1),
    ),
    # 5. The deferred takeover. Registration already left fb_con registered and
    #    con2fb_map set by the map: override, and do_take_over_console()
    #    tolerates the -EBUSY from that registration, so all that is missing is
    #    the bind -- done once mdss_fb_probe() returned and before init runs, so
    #    the boot messages still land on the panel.
    (
        "module_init(fb_console_init);\n",
        "#ifndef MODULE\n"
        "/* pmos-j4primelte: bind the console once the display driver has\n"
        " * finished probing instead of from inside register_framebuffer().\n"
        " * Runs in every image: a refused fb_open() at registration time\n"
        " * otherwise leaves fb_con forever unbound, and this call is late\n"
        " * enough that mdss_fb_probe() has returned. */\n"
        "static int __init fbcon_bind_late_takeover(void)\n"
        "{\n"
        "\tconsole_lock();\n"
        "\tif (!con_is_bound(&fb_con))\n"
        "\t\tdo_fbcon_takeover(0);\n"
        "\tconsole_unlock();\n"
        "\treturn 0;\n"
        "}\n"
        "late_initcall(fbcon_bind_late_takeover);\n"
        "#endif /* !MODULE */\n"
        "\n"
        "module_init(fb_console_init);\n",
        ('late_initcall(fbcon_bind_late_takeover);', 1),
    ),
]

# fbcon_startup() ends with exactly two refusals in the pinned tree: the veto's
# module_put()/return NULL pair and the identical pair in the kzalloc failure
# path right below it. After patching the first one only moves inside an
# "if (!fbcon_late_refusal)" branch, so the total may not change -- and since it was
# 2 before the patch too, this doubles as proof that no hunk deleted or
# duplicated a refusal. The polarity matters more than the count: a refusal on
# the *else* side of that condition would fire on every successful fb_open()
# and leave the panel dark forever, which no structural check would notice, so
# test-kernel-fbcon.py pins the shape of the block instead.
OPENS_EXPECTED = 1

# The hunk whose absence must abort the build.
PRIMARY = VETO_ORIGINAL


def fbcon_path(tree):
    return Path(tree) / RELATIVE


def probes_present(text):
    return all(text.count(needle) == count for _, _, (needle, count) in HUNKS)


def is_patched(tree):
    """(all five replacement texts present, every probe counting exactly right)."""
    text = fbcon_path(tree).read_text()
    stamped = all(new in text for _, new, _ in HUNKS)
    return stamped and probes_present(text), text.count(MARKER) == 2


def apply(tree):
    """Return the state of the tree after patching. Raises if inapplicable."""
    path = fbcon_path(tree)
    if not path.is_file():
        raise SystemExit('kernel-fbcon: %s missing; is this the pinned tree?' % path)
    text = path.read_text()
    # Counted before any replacement: the file must end with exactly as many
    # real fb_open() call sites as it started with (we skip the call inside
    # fbcon_startup(), we do not add or delete one).
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
    # The consw wiring is what makes this patch the whole story; if fb_con no
    # longer names fbcon_startup, this helper is patching the wrong thing.
    if not re.search(r'\.con_startup\s*=\s*fbcon_startup', text):
        raise SystemExit('kernel-fbcon: fb_con no longer wires con_startup '
                         'to fbcon_startup; refusing')
    # A silent loss of any one gate reference restores the behaviour that
    # boot-looped #35-#38, so demand all four roles the flag appears in:
    # declaration, parser assignment, veto refusal, takeover gate. (The deferred
    # takeover carries its own name, not the flag: it must run in every image.)
    refs = text.count('fbcon_late_refusal')
    if refs < 4:
        raise SystemExit('kernel-fbcon: the late-bind flag is referenced %d '
                         'times, expected at least 4; refusing to write' % refs)
    if text.count('module_put(owner);') != OPENS_EXPECTED + 1:
        raise SystemExit('kernel-fbcon: module_put(owner) now appears %d times, '
                         'expected %d; a hunk swallowed the wrong block'
                         % (text.count('module_put(owner);'), OPENS_EXPECTED + 1))
    if text.count('fbops->fb_open(info, 0)') != opens_before:
        raise SystemExit('kernel-fbcon: the number of fb_open() call sites moved '
                         'from %d to %d; this patch adds and removes none'
                         % (opens_before, text.count('fbops->fb_open(info, 0)')))
    path.write_text(text)
    stamped, stamped_two = is_patched(tree)
    if not stamped or not stamped_two:
        raise SystemExit('kernel-fbcon: patch did not stick')
    print('kernel-fbcon: fb_open skipped, takeover deferred to late_initcall() '
          '("fbcon=late" restores the old behaviour as the control group)')
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
