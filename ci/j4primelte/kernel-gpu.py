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

SMMU = DRM_DIR / 'msm_smmu.c'
ADRENO_GPU_H = DRM_DIR / 'adreno' / 'adreno_gpu.h'
MAKEFILE = DRM_DIR / 'Makefile'
ADRENO = DRM_DIR / 'adreno' / 'adreno_device.c'

# The Adreno 308 of the MSM8917. adreno_device.c only lists A530, so without
# this entry adreno_info() returns NULL for our chipid (0x03000620) and the
# DRM driver would come up without a GPU.
ADRENO_GPULIST_ANCHOR = 'static const struct adreno_info gpulist[] = {\n'

ADRENO_A306_ENTRY = """	{
		.rev   = ADRENO_REV(3, 0, 6, ANY_ID),
		.revn  = 306,
		.name  = "A306",
		.pm4fw = "a300_pm4.fw",
		.pfpfw = "a300_pfp.fw",
		.gmem  = SZ_128K,
		.init  = a3xx_gpu_init,
	},
"""

# msm_smmu.c carries its own module_init(), so linking it into msm.ko gives two
# init_module symbols. Build it as a module of its own and export the symbol
# msm_gpu.c and mdp5_kms.c call.
SMMU_OBJ = 'obj-$(CONFIG_DRM_MSM) += msm_smmu.o\n'
SMMU_MSM_ANCHOR = 'obj-$(CONFIG_DRM_MSM)\t+= msm.o\n'
SMMU_EXPORT = ('\n/* j4primelte: msm_smmu is a module of its own now, so msm.ko\n'
               ' * can call msm_smmu_new() through the module symbol table. */\n'
               'EXPORT_SYMBOL(msm_smmu_new);\n')

# a3xx_gpu.c and a4xx_gpu.c index a3xx_register_offsets[] with three register
# names this tree's enum never had, so neither file has ever compiled here. The
# positions matter: the register table is initialised positionally.
SCRATCH_OLD = '\tREG_ADRENO_CP_ME_RAM_RADDR,\n'
SCRATCH_NEW = ('\tREG_ADRENO_CP_ME_RAM_RADDR,\n'
               '\tREG_ADRENO_SCRATCH_ADDR,\n'
               '\tREG_ADRENO_SCRATCH_UMSK,\n')
SCRATCH2_OLD = '\tREG_ADRENO_SQ_GPR_MANAGEMENT,\n'
SCRATCH2_NEW = ('\tREG_ADRENO_SCRATCH_REG2,\n'
                '\tREG_ADRENO_SQ_GPR_MANAGEMENT,\n')

# The SDE code does not compile in this tree: sde_plane.c uses
# sde_drm_scaler_v1.lr/.tb, which include/uapi/drm/sde_drm.h never had, so SDE
# has never been buildable here. This SoC is MDP5 anyway.
SDE_KMS_OLD = '\tkms = sde_kms_init(dev);\n'
SDE_KMS_NEW = ('\tkms = ERR_PTR(-ENODEV);'
               '  /* j4primelte: SDE is not built; this SoC is MDP5 */\n')

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


def drop_sde_from_makefile(text):
    """Remove every SDE object and add the a3xx/a4xx GPU objects."""
    lines = text.splitlines(keepends=True)
    out = []
    dropped = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        block_start = stripped.startswith(('obj-$(CONFIG_DRM_MSM) += sde/',
                                           'obj-$(CONFIG_DRM_SDE_WB)'))
        if block_start:
            while i < len(lines):
                dropped.append(lines[i].strip())
                i += 1
                if not lines[i - 1].rstrip('\n').rstrip().endswith('\\'):
                    break
            continue
        if stripped.endswith('\\') and stripped.rstrip('\\').strip() == 'msm_smmu.o':

            dropped.append(stripped)
            i += 1
            continue
        if stripped.startswith('sde/sde_') or \
                stripped == 'msm-$(CONFIG_SYNC) += sde/sde_fence.o' or \
                stripped.startswith('obj-$(CONFIG_DRM_MSM) += display-manager/'):
            dropped.append(stripped)
            i += 1
            continue
        out.append(lines[i])
        if lines[i] == SMMU_MSM_ANCHOR and SMMU_OBJ not in text:
            out.append(SMMU_OBJ)
        if stripped.startswith('adreno/adreno_gpu.o'):
            # a3xx only: the Adreno 308. a4xx is left out on purpose, it calls
            # adreno_is_a4xx(), which this tree does not define either.
            out.append('\tadreno/a3xx_gpu.o \\\n')
        i += 1
    return ''.join(out), dropped


def apply(tree):
    tree = Path(tree)
    if not (tree / DRM_DIR).is_dir():
        raise SystemExit('not a kernel tree with drivers/gpu/drm/msm: %s' % tree)
    drv = tree / DRV
    text = drv.read_text()
    text, changed_dt = patch(text, DT_MATCH_OLD, DT_MATCH_NEW,
                             'qcom,mdss_mdp" }, /* mdp5', 'dt_match')
    text, changed_gpus = patch(text, GPUS_OLD, GPUS_NEW,
                               'j4primelte: Samsung\'s device tree has no "gpus"',
                               'gpus fallback')
    text, changed_sde = patch(text, SDE_KMS_OLD, SDE_KMS_NEW,
                              'j4primelte: SDE is not built', 'SDE KMS call')
    if changed_dt or changed_gpus or changed_sde:
        drv.write_text(text)

    makefile = tree / MAKEFILE
    made, dropped = drop_sde_from_makefile(makefile.read_text())
    changed_make = bool(dropped) and 'adreno/a3xx_gpu.o' not in makefile.read_text()
    if changed_make:
        makefile.write_text(made)

    smmu = tree / SMMU
    smmu_text = smmu.read_text()
    if 'EXPORT_SYMBOL(msm_smmu_new)' not in smmu_text:
        smmu.write_text(smmu_text.rstrip('\n') + '\n' + SMMU_EXPORT)

    regs = tree / ADRENO_GPU_H
    regs_text = regs.read_text()
    changed_regs = False
    if 'REG_ADRENO_SCRATCH_ADDR,' not in regs_text:
        if SCRATCH_OLD not in regs_text:
            raise SystemExit('ME_RAM_RADDR anchor not found in %s' % regs)
        regs_text = regs_text.replace(SCRATCH_OLD, SCRATCH_NEW, 1)
        changed_regs = True
    if 'REG_ADRENO_SCRATCH_REG2,' not in regs_text:
        if SCRATCH2_OLD not in regs_text:
            raise SystemExit('SQ_GPR_MANAGEMENT anchor not found in %s' % regs)
        regs_text = regs_text.replace(SCRATCH2_OLD, SCRATCH2_NEW, 1)
        changed_regs = True
    if changed_regs:
        regs.write_text(regs_text)

    adreno = tree / ADRENO
    gpu_text = adreno.read_text()
    changed_gpu = 'A306' not in gpu_text
    if changed_gpu:
        if ADRENO_GPULIST_ANCHOR not in gpu_text:
            raise SystemExit('gpulist anchor not found in %s' % adreno)
        adreno.write_text(gpu_text.replace(ADRENO_GPULIST_ANCHOR,
                                           ADRENO_GPULIST_ANCHOR
                                           + ADRENO_A306_ENTRY, 1))

    print('patched %s (dt_match=%s gpus=%s sde=%s makefile=%s dropped=%d '
          'adreno=%s regs=%s)'
          % (drv.name, changed_dt, changed_gpus, changed_sde,
             changed_make, len(dropped), changed_gpu, changed_regs))
    return drv


def check(tree):
    tree = Path(tree)
    drv = (tree / DRV).read_text()
    makefile = (tree / MAKEFILE).read_text()
    adreno = (tree / ADRENO).read_text()
    regs = (tree / ADRENO_GPU_H).read_text()
    ok = ('qcom,mdss_mdp" }, /* mdp5' in drv
          and 'j4primelte: Samsung\'s device tree has no "gpus"' in drv
          and 'j4primelte: SDE is not built' in drv
          and 'adreno/a3xx_gpu.o' in makefile
          and 'sde/sde_plane.o' not in makefile
          and 'A306' in adreno
          and 'REG_ADRENO_SCRATCH_ADDR,' in regs
          and 'REG_ADRENO_SCRATCH_REG2,' in regs
          and SMMU_OBJ in makefile
          and 'EXPORT_SYMBOL(msm_smmu_new)' in (tree / SMMU).read_text())
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
