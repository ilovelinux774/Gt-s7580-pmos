#!/usr/bin/env python3
"""Stop the display driver from vetoing fbcon's console bind.

The j4primelte kernel carries CONFIG_FRAMEBUFFER_CONSOLE=y, registers mdss_fb's
two framebuffers (/proc/fb: mdssfb_80000 for the panel, mdssfb_a0000 for the
writeback pipe) and parses "fbcon=map:0", yet nothing ever appears on the panel
until the display server starts. The reason is one hunk inside
fbcon_startup(): it asks the framebuffer driver for permission to bind,

    if (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {
            module_put(owner);
            return NULL;
    }

and Samsung's mdss_fb_open() runs mdss_fb_blank_sub(FB_BLANK_UNBLANK) whenever
the framebuffer has no owner yet (mfd->ref_cnt == 0) -- which is precisely the
situation while mdss_fb_probe() is still registering it. That fails, so
fbcon_startup() returns NULL, do_bind_con_driver() refuses the bind, fbcon
unregisters its own consw, and /sys/class/vtconsole keeps only the dummy
device. Nothing in userspace can recover from that, because do_take_over_console()
-- reached only through do_fbcon_takeover() -- is the sole thing that ever
registers fb_con.

The bind itself is fine: FBIOPUT_CON2FBMAP from a shell, with the display server
already holding fb0 open (so ref_cnt != 0 and the blank path is skipped), binds
fbcon on the spot -- vtcon1 "(M) frame buffer device" with bind 1, and vtcon0
dropping to bind 0. So the veto, not the mode, the font, the backlight or X, is
what hides the boot messages. Dropping the veto lets the console write into the
splash memory the boot loader already programmed, which is what the panel scans
out regardless.

Idempotent: re-running the helper on an already patched tree changes nothing.
"""
import argparse
import re
import sys
from pathlib import Path

MARKER = 'pmos-j4primelte: mdss_fb_open()'
RELATIVE = Path('drivers') / 'video' / 'console' / 'fbcon.c'

ORIGINAL = (
    "\tif (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
    "\t\tmodule_put(owner);\n"
    "\t\treturn NULL;\n"
    "\t}\n"
)

PATCHED = (
    "\tif (info->fbops->fb_open && info->fbops->fb_open(info, 0)) {\n"
    "\t\t/* pmos-j4primelte: mdss_fb_open() calls mdss_fb_blank_sub()\n"
    "\t\t * whenever the framebuffer has no owner yet, which is exactly the\n"
    "\t\t * case while mdss_fb_probe() is still registering it. It fails\n"
    "\t\t * there, and this call vetoed the whole bind: fbcon_startup()\n"
    "\t\t * returned NULL, do_bind_con_driver() unregistered fb_con again\n"
    "\t\t * and no userspace action could ever bring the console back. The\n"
    "\t\t * panel is already scanning the splash memory the boot loader\n"
    "\t\t * programmed, so keep the bind and let the console write into it.\n"
    "\t\t * The missing open is harmless: fbcon_exit() only reaches\n"
    "\t\t * mdss_fb_release(), which returns -EINVAL while ref_cnt is 0 and\n"
    "\t\t * therefore never touches the panel power state. */\n"
    "\t\tprintk(KERN_INFO \"fbcon: fb_open failed, binding anyway\\\\n\");\n"
    "\t}\n"
)


def fbcon_path(tree):
    return Path(tree) / RELATIVE


def is_patched(tree):
    text = fbcon_path(tree).read_text()
    return MARKER in text, ORIGINAL not in text


def apply(tree):
    """Return the state of the tree after patching. Raises if inapplicable."""
    path = fbcon_path(tree)
    if not path.is_file():
        raise SystemExit('kernel-fbcon: %s missing; is this the pinned tree?' % path)
    text = path.read_text()
    if MARKER in text:
        if ORIGINAL in text:
            raise SystemExit('kernel-fbcon: patched and unpatched hunk both present')
        print('kernel-fbcon: already patched, nothing to do')
        return 'patched'
    if ORIGINAL not in text:
        raise SystemExit('kernel-fbcon: the fbcon_startup() hunk this patch '
                         'expects is not in %s; refusing to guess' % path)
    if text.count(ORIGINAL) != 1:
        raise SystemExit('kernel-fbcon: %d copies of the hunk, expected 1'
                         % text.count(ORIGINAL))
    # The point of the patch is that fb_con is bound through fbcon_startup();
    # if the consw no longer names it, this helper is patching the wrong thing.
    if not re.search(r'\.con_startup\s*=\s*fbcon_startup', text):
        raise SystemExit('kernel-fbcon: fb_con no longer wires con_startup '
                         'to fbcon_startup; refusing')
    path.write_text(text.replace(ORIGINAL, PATCHED, 1))
    patched, gone = is_patched(tree)
    if not patched or not gone:
        raise SystemExit('kernel-fbcon: patch did not stick')
    print('kernel-fbcon: fbcon_startup() no longer lets fb_open veto the bind')
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
