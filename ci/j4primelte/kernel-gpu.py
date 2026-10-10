#!/usr/bin/env python3
"""Stage the MSM DRM driver for the J4+ so freedreno can drive the Adreno 308.

The pinned 3.18 kernel carries drivers/gpu/drm/msm with MDP5 KMS and the a3xx
(Adreno 308) GPU, but none of it has ever been built here, so on the way to a
working module this helper has to repair a fair amount of dead code:

- msm_drv.c's dt_match[] excludes "qcom,mdss_mdp" because the framebuffer
  driver owns that node. get_mdp_ver() already maps it to KMS_MDP5.
- add_components() reads "connectors"/"gpus" phandle lists; Samsung's tree has
  neither, so the GPU would never become a component.
- SDE, display-manager and dsi are not buildable or not usable here: sde_plane.c
  uses register structs the UAPI header never had, and dsi-staging depends on
  them. They are dropped, together with the calls msm_drv.c makes into them.
- a3xx_gpu.c and a4xx_gpu.c index three register names this tree's enum lacks;
  a4xx additionally calls adreno_is_a4xx(), which does not exist, so only a3xx
  is built.
- adreno_device.c's gpulist[] only lists A530, so the Adreno 308 was unknown.
- msm_smmu.c carries its own module_init(), so it is built as a module beside
  msm.ko and the two IOMMU helpers it calls are exported.

Configuration comes from build-kernel.sh. Freedreno itself is already installed
in userspace (mesa-dri-gallium); what it needs is /dev/dri/card0.
"""
import argparse
import sys
from pathlib import Path

DRM_DIR = Path('drivers') / 'gpu' / 'drm' / 'msm'
DRV = DRM_DIR / 'msm_drv.c'
MAKEFILE = DRM_DIR / 'Makefile'
ADRENO = DRM_DIR / 'adreno' / 'adreno_device.c'
ADRENO_GPU_H = DRM_DIR / 'adreno' / 'adreno_gpu.h'
SMMU = DRM_DIR / 'msm_smmu.c'
IOMMU_MAP = Path('drivers') / 'iommu' / 'msm_dma_iommu_mapping.c'

# (name, old, new, marker) - marker None means "patched once old is gone".
MSM_DRV_PATCHES = [
    ('dt_match', '''static const struct of_device_id dt_match[] = {
\t{ .compatible = "qcom,mdp" },      /* mdp4 */
\t{ .compatible = "qcom,sde-kms" },  /* sde  */''',
     '''static const struct of_device_id dt_match[] = {
\t{ .compatible = "qcom,mdp" },      /* mdp4 */
\t{ .compatible = "qcom,mdss_mdp" }, /* mdp5 (j4primelte: fb driver is off) */
\t{ .compatible = "qcom,sde-kms" },  /* sde  */''',
     'qcom,mdss_mdp" }, /* mdp5'),
    ('gpus', '''\tadd_components(&pdev->dev, &match, "connectors");
\tadd_components(&pdev->dev, &match, "gpus");''',
     '''\tadd_components(&pdev->dev, &match, "connectors");
\tadd_components(&pdev->dev, &match, "gpus");
\t/* j4primelte: Samsung's device tree has no "gpus" phandle list, so the
\t * Adreno node would never become a component and the DRM device would come
\t * up without a GPU. Fall back to the node the adreno driver binds to.
\t */
\tif (!of_find_property(pdev->dev.of_node, "gpus", NULL)) {
\t\tstruct device_node *gpu;

\t\tgpu = of_find_compatible_node(NULL, NULL, "qcom,kgsl-3d0");
\t\tif (gpu)
\t\t\tcomponent_match_add(&pdev->dev, &match, compare_of, gpu);
\t}''',
     'j4primelte: Samsung\'s device tree has no "gpus"'),
    ('sde_kms', '\tkms = sde_kms_init(dev);\n',
     '\tkms = ERR_PTR(-ENODEV);  /* j4primelte: SDE is not built */\n',
     'j4primelte: SDE is not built'),
    # display-manager and the DSI stack are not built, so init must not call in.
    ('display_manager', '\tdisplay_manager_register();\n', '', None),
    ('display_manager_exit', '\tdisplay_manager_unregister();\n', '', None),
    ('dsi_register', '\tmsm_dsi_register();\n', '', None),
    ('dsi_unregister', '\tmsm_dsi_unregister();\n', '', None),
    # The SDE writeback ioctl handler lives in the code we dropped.
    ('sde_wb', 'DRM_IOCTL_DEF_DRV(SDE_WB_CONFIG, sde_wb_config, '
     'DRM_UNLOCKED|DRM_AUTH),',
     'DRM_IOCTL_DEF_DRV(SDE_WB_CONFIG, drm_noop, DRM_UNLOCKED|DRM_AUTH),',
     'DRM_IOCTL_DEF_DRV(SDE_WB_CONFIG, drm_noop'),
]

ADRENO_GPULIST_ANCHOR = 'static const struct adreno_info gpulist[] = {\n'

ADRENO_A306_ENTRY = '''	{
		.rev   = ADRENO_REV(3, 0, 6, ANY_ID),
		.revn  = 306,
		.name  = "A306",
		.pm4fw = "a300_pm4.fw",
		.pfpfw = "a300_pfp.fw",
		.gmem  = SZ_128K,
		.init  = a3xx_gpu_init,
	},
'''

SCRATCH_OLD = '\tREG_ADRENO_CP_ME_RAM_RADDR,\n'
SCRATCH_NEW = ('\tREG_ADRENO_CP_ME_RAM_RADDR,\n'
               '\tREG_ADRENO_SCRATCH_ADDR,\n'
               '\tREG_ADRENO_SCRATCH_UMSK,\n')
SCRATCH2_OLD = '\tREG_ADRENO_SQ_GPR_MANAGEMENT,\n'
SCRATCH2_NEW = ('\tREG_ADRENO_SCRATCH_REG2,\n'
                '\tREG_ADRENO_SQ_GPR_MANAGEMENT,\n')

SMMU_OBJ = 'obj-$(CONFIG_DRM_MSM) += msm_smmu.o\n'
SMMU_MSM_ANCHOR = 'obj-$(CONFIG_DRM_MSM)\t+= msm.o\n'
SMMU_EXPORT = ('\n/* j4primelte: msm_smmu is a module of its own now. */\n'
               'EXPORT_SYMBOL(msm_smmu_new);\n')

IOMMU_EXPORT = ('\n#include <linux/export.h>\n'
                '/* j4primelte: msm_smmu.ko needs these from the built-in '
                'IOMMU helper. */\n'
                'EXPORT_SYMBOL(msm_dma_map_sg_attrs);\n'
                'EXPORT_SYMBOL(msm_dma_unmap_sg);\n')

# Symbols build-kernel.sh must set for a working, boot-safe configuration.
# No DSI symbols: the panel is not wired up for DRM yet, and dsi-staging only
# pulls in the SDE code that cannot be built.
ENABLE = [
    'DRM',
    'DRM_MSM',
    'DRM_FBDEV_EMULATION',
    'DRM_KMS_HELPER',
    'DRM_PANEL',
    'DRM_MIPI_DSI',
    'CMA',
    'DMA_CMA',
]

# MSM_KGSL turns adreno_register() into an empty stub; FB_MSM_MDSS owns the
# mdss_mdp node the KMS driver needs; KLAPSE calls the MDSS KCAL colour
# functions, so it cannot be linked once the framebuffer driver is gone.
DISABLE = [
    'MSM_KGSL',
    'FB_MSM_MDSS',
    'KLAPSE',
    # On by default in this defconfig, so they have to be turned off, not just
    # left out of ENABLE: dsi-staging is the only user of the SDE code that
    # cannot be compiled here, and it does the 64 bit maths that modpost cannot
    # resolve for a module. The panel has no DRM device tree node yet either.
    'DRM_MSM_DSI_STAGING',
    'DRM_MSM_DSI_PLL',
    'DRM_MSM_DSI_28NM_PHY',
    # The SDE era HDMI stack: on by default, calls into sde_kms_info. The
    # plain hdmi/ driver stays, msm_drv.c calls hdmi_register() either way.
    'DRM_SDE_HDMI',
]


def patch(text, old, new, marker, what):
    if marker is not None and marker in text:
        return text, False
    if old not in text:
        if marker is None:
            return text, False
        raise SystemExit('anchor not found for %s in msm_drv.c' % what)
    return text.replace(old, new, 1), True


def drop_objects(text):
    """Remove the objects that cannot be built, and add the a3xx GPU."""
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
        if (stripped.startswith('sde/sde_')
                or stripped.startswith('hdmi-staging/')
                or stripped == 'msm-$(CONFIG_SYNC) += sde/sde_fence.o'
                or stripped.startswith('obj-$(CONFIG_DRM_MSM) += display-manager/')
                or stripped.startswith('msm-$(CONFIG_DRM_SDE_HDMI)')):
            dropped.append(stripped)
            i += 1
            continue
        if stripped.endswith('\\') and stripped.rstrip('\\').strip() == 'msm_smmu.o':
            dropped.append(stripped)
            i += 1
            continue
        out.append(lines[i])
        if lines[i] == SMMU_MSM_ANCHOR and SMMU_OBJ not in text:
            out.append(SMMU_OBJ)
        if (stripped.startswith('adreno/adreno_gpu.o')
                and 'adreno/a3xx_gpu.o' not in text):
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
    changes = []
    for name, old, new, marker in MSM_DRV_PATCHES:
        text, changed = patch(text, old, new, marker, name)
        changes.append('%s=%s' % (name, changed))
    drv.write_text(text)

    makefile = tree / MAKEFILE
    made, dropped = drop_objects(makefile.read_text())
    makefile.write_text(made)

    adreno = tree / ADRENO
    gpu_text = adreno.read_text()
    changed_gpu = 'A306' not in gpu_text
    if changed_gpu:
        if ADRENO_GPULIST_ANCHOR not in gpu_text:
            raise SystemExit('gpulist anchor not found in %s' % adreno)
        adreno.write_text(gpu_text.replace(ADRENO_GPULIST_ANCHOR,
                                           ADRENO_GPULIST_ANCHOR
                                           + ADRENO_A306_ENTRY, 1))

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

    smmu = tree / SMMU
    smmu_text = smmu.read_text()
    if 'EXPORT_SYMBOL(msm_smmu_new)' not in smmu_text:
        smmu.write_text(smmu_text.rstrip('\n') + '\n' + SMMU_EXPORT)

    iommu = tree / IOMMU_MAP
    iommu_text = iommu.read_text()
    if 'EXPORT_SYMBOL(msm_dma_unmap_sg)' not in iommu_text:
        iommu.write_text(iommu_text.rstrip('\n') + '\n' + IOMMU_EXPORT)

    print('patched %s (%s) makefile dropped=%d adreno=%s regs=%s'
          % (drv.name, ' '.join(changes), len(dropped), changed_gpu,
             changed_regs))
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
          and 'display_manager_register();' not in drv
          and 'msm_dsi_register();' not in drv
          and 'adreno/a3xx_gpu.o' in makefile
          and 'sde/sde_plane.o' not in makefile
          and 'hdmi-staging/' not in makefile
          and SMMU_OBJ in makefile
          and 'A306' in adreno
          and 'REG_ADRENO_SCRATCH_ADDR,' in regs
          and 'REG_ADRENO_SCRATCH_REG2,' in regs
          and 'EXPORT_SYMBOL(msm_smmu_new)' in (tree / SMMU).read_text()
          and 'EXPORT_SYMBOL(msm_dma_unmap_sg)' in (tree / IOMMU_MAP).read_text())
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
