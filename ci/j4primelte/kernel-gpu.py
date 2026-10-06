#!/usr/bin/env python3
"""Stage the MSM DRM driver for the J4+ so freedreno can drive the Adreno 308.

The pinned 3.18 kernel carries drivers/gpu/drm/msm with MDP5 KMS and the a3xx
(Adreno 308) GPU, but it can never bind on this phone:

- msm_drv.c's dt_match[] deliberately excludes "qcom,mdss_mdp", because the
  downstream framebuffer driver still uses that node. get_mdp_ver() already
  maps "qcom,mdss_mdp" to KMS_MDP5, so the table is the only thing missing.
- add_components() reads "connectors" and "gpus" phandle lists from the device
  tree. Samsung's tree has neither, so the GPU never becomes a component and
  the DRM device would come up without a GPU at all.
- With CONFIG_MSM_KGSL set, adreno_register() is compiled as an empty stub,
  so the DRM adreno driver is never registered.

This helper applies the first two as source patches (idempotent) and reports
the configuration; build-kernel.sh applies the configuration. The driver is
built as a module and blacklisted: a failed probe can never stop the boot.

Freedreno itself is already installed in userspace (mesa-dri-gallium); what it
needs is /dev/dri/card0, which this is about.
"""
import argparse
import sys
from pathlib import Path

DRM_DIR = Path('drivers') / 'gpu' / 'drm' / 'msm'
DRV = DRM_DIR / 'msm_drv.c'

# The fb driver is disabled in this configuration, so the KMS driver may claim
# the node that Samsung's framebuffer driver used to own.
DT_MATCH_OLD = '''static const struct of_device_id dt_match[] = {
\t{ .compatible = "qcom,mdp" },      /* mdp4 */
\t{ .compatible = "qcom,sde-kms" },  /* sde  */'''

DT_MATCH_NEW = '''static const struct of_device_id dt_match[] = {
\t{ .compatible = "qcom,mdp" },      /* mdp4 */
\t{ .compatible = "qcom,mdss_mdp" }, /* mdp5 (j4primelte: fb driver is off) */
\t{ .compatible = "qcom,sde-kms" },  /* sde  */'''

GPUS_OLD = '''\tadd_components(&pdev->dev, &match, "connectors");
\tadd_components(&pdev->dev, &match, "gpus");'''

GPUS_NEW = '''\tadd_components(&pdev->dev, &match, "connectors");
\tadd_components(&pdev->dev, &match, "gpus");
\t/* j4primelte: Samsung's device tree has no "gpus" phandle list, so the
\t * Adreno node would never become a component of the DRM device and the
\t * device would come up without a GPU. Fall back to the node the adreno
\t * driver binds to (qcom,kgsl-3d0), keeping the reference for the match.
\t */
\tif (!of_find_property(pdev->dev.of_node, "gpus", NULL)) {
\t\tstruct device_node *gpu;

\t\tgpu = of_find_compatible_node(NULL, NULL, "qcom,kgsl-3d0");
\t\tif (gpu)
\t\t\tcomponent_match_add(&pdev->dev, &match, compare_of, gpu);
\t}'''

# Symbols build-kernel.sh must set for a working, boot-safe configuration.
ENABLE = [
    'DRM',
    'DRM_MSM',
    'DRM_MSM_DSI_STAGING',
    'DRM_MSM_DSI_PLL',
    'DRM_MSM_DSI_28NM_PHY',
    'DRM_FBDEV_EMULATION',
    'DRM_KMS_HELPER',
    'DRM_PANEL',
    'DRM_MIPI_DSI',
    'CMA',
    'DMA_CMA',
]

# MSM_KGSL turns adreno_register() into an empty stub; FB_MSM_MDSS owns the
# mdss_mdp node the KMS driver needs.
DISABLE = [
    'MSM_KGSL',
    'FB_MSM_MDSS',
]


def patch(text, old, new, marker, what):
    if marker in text:
        return text, False
    if old not in text:
        raise SystemExit('anchor not found for %s in msm_drv.c' % what)
    return text.replace(old, new, 1), True


def apply(tree):
    tree = Path(tree)
    if not (tree / DRM_DIR).is_dir():
        raise SystemExit('not a kernel tree with drivers/gpu/drm/msm: %s' % tree)
    path = tree / DRV
    text = path.read_text()
    text, changed_dt = patch(text, DT_MATCH_OLD, DT_MATCH_NEW,
                             'qcom,mdss_mdp" }, /* mdp5', 'dt_match')
    text, changed_gpus = patch(text, GPUS_OLD, GPUS_NEW,
                               'j4primelte: Samsung\'s device tree has no "gpus"',
                               'gpus fallback')
    if changed_dt or changed_gpus:
        path.write_text(text)
    print('patched %s (dt_match=%s gpus=%s)'
          % (path.name, changed_dt, changed_gpus))
    return path


def check(tree):
    text = (Path(tree) / DRV).read_text()
    ok = ('qcom,mdss_mdp" }, /* mdp5' in text
          and 'j4primelte: Samsung\'s device tree has no "gpus"' in text)
    print('patched' if ok else 'not patched')
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('tree', help='path to the pinned kernel source tree')
    parser.add_argument('--check', action='store_true',
                        help='only report whether the tree is already patched')
    args = parser.parse_args()
    if args.check:
        return check(args.tree)
    apply(args.tree)
    return 0


if __name__ == '__main__':
    sys.exit(main())
