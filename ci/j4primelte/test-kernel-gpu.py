#!/usr/bin/env python3
"""Unit tests for the MSM DRM staging helper."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gpu = load('kernel-gpu')

FAKE_DRV = '''static int add_components(struct device *dev, struct component_match **m,
\t\tconst char *name)
{
}

static int msm_pdev_probe(struct platform_device *pdev)
{
\tstruct component_match *match = NULL;
#ifdef CONFIG_OF
\tadd_components(&pdev->dev, &match, "connectors");
\tadd_components(&pdev->dev, &match, "gpus");
#endif
\treturn component_master_add_with_match(&pdev->dev, &msm_drm_ops, match);
}

	switch (get_mdp_ver(pdev)) {
	case KMS_MDP5:
		kms = mdp5_kms_init(dev);
		break;
	case KMS_SDE:
		kms = sde_kms_init(dev);
		break;
	}

/*
 * Since "qcom,mdss_mdp" is still being used by fb driver,
 * we can't enable it for kms driver
 */
static const struct of_device_id dt_match[] = {
\t{ .compatible = "qcom,mdp" },      /* mdp4 */
\t{ .compatible = "qcom,sde-kms" },  /* sde  */
\t{}
};
'''


FAKE_MAKEFILE = """msm-y := \\
\thdmi/hdmi.o \\
\tsde/sde_crtc.o \\
\tsde/sde_kms.o \\
\tsde/sde_plane.o \\
\tsde/sde_connector.o \\
\tmsm_smmu.o \\
\tmsm_drv.o

ifneq ($(CONFIG_QCOM_KGSL),y)
msm-y += adreno/adreno_device.o \\
\tadreno/adreno_gpu.o \\
\tadreno/a5xx_gpu.o \\
\tadreno/adreno_perfcounter.o
endif

msm-$(CONFIG_SYNC) += sde/sde_fence.o

obj-$(CONFIG_DRM_MSM)\t+= msm.o

obj-$(CONFIG_DRM_MSM) += sde/sde_hw_catalog.o \\
\tsde/sde_formats.o

obj-$(CONFIG_DRM_MSM) += display-manager/display_manager.o

obj-$(CONFIG_DRM_SDE_WB) += sde/sde_wb.o \\
\tsde/sde_encoder_phys_wb.o
"""

FAKE_SMMU = """struct msm_mmu *msm_smmu_new(struct drm_device *dev,
\tstruct device *d, enum msm_mmu_domain_type domain)
{
\treturn NULL;
}

MODULE_LICENSE("GPL v2");
"""

FAKE_REGS = """enum adreno_regs {
\tREG_ADRENO_CP_TIMESTAMP,
\tREG_ADRENO_CP_ME_RAM_RADDR,
\tREG_ADRENO_CP_ROQ_ADDR,
\tREG_ADRENO_PA_SC_AA_CONFIG,
\tREG_ADRENO_SQ_GPR_MANAGEMENT,
\tREG_ADRENO_REGISTER_MAX,
};
"""

FAKE_ADRENO = """struct msm_gpu *a3xx_gpu_init(struct drm_device *dev);
struct msm_gpu *a5xx_gpu_init(struct drm_device *dev);

static const struct adreno_info gpulist[] = {
\t{
\t\t.rev   = ADRENO_REV(5, 3, 0, 0),
\t\t.name  = "A530",
\t\t.init  = a5xx_gpu_init,
\t},
};
"""


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        drm = self.tree / gpu.DRM_DIR
        (drm / 'adreno').mkdir(parents=True)
        (drm / 'msm_drv.c').write_text(FAKE_DRV)
        (drm / 'Makefile').write_text(FAKE_MAKEFILE)
        (drm / 'adreno' / 'adreno_device.c').write_text(FAKE_ADRENO)
        (drm / 'adreno' / 'adreno_gpu.h').write_text(FAKE_REGS)
        (drm / 'msm_smmu.c').write_text(FAKE_SMMU)
        self.drv = drm / 'msm_drv.c'
        self.makefile = drm / 'Makefile'
        self.adreno = drm / 'adreno' / 'adreno_device.c'

    def test_apply_patches_both_spots(self):
        gpu.apply(self.tree)
        text = self.drv.read_text()
        self.assertIn('{ .compatible = "qcom,mdss_mdp" }, /* mdp5', text)
        self.assertIn('of_find_compatible_node(NULL, NULL, "qcom,kgsl-3d0")',
                      text)

    def test_apply_is_idempotent(self):
        gpu.apply(self.tree)
        first = self.drv.read_text()
        gpu.apply(self.tree)
        self.assertEqual(self.drv.read_text(), first)

    def test_check_reports_state(self):
        self.assertEqual(gpu.check(self.tree), 1)
        gpu.apply(self.tree)
        self.assertEqual(gpu.check(self.tree), 0)

    def test_rejects_a_tree_without_the_driver(self):
        other = Path(self.temp.name) / 'elsewhere'
        other.mkdir()
        with self.assertRaises(SystemExit):
            gpu.apply(other)

    def test_patch_keeps_the_original_entries(self):
        gpu.apply(self.tree)
        text = self.drv.read_text()
        self.assertIn('{ .compatible = "qcom,mdp" },      /* mdp4 */', text)
        self.assertIn('{ .compatible = "qcom,sde-kms" },  /* sde  */', text)
        self.assertIn('add_components(&pdev->dev, &match, "connectors");', text)


    def test_makefile_drops_sde_and_adds_the_gpu_objects(self):
        gpu.apply(self.tree)
        text = self.makefile.read_text()
        self.assertNotIn('sde/sde_plane.o', text)
        self.assertNotIn('sde/sde_hw_catalog.o', text)
        self.assertNotIn('display-manager/display_manager.o', text)
        self.assertNotIn('sde/sde_fence.o', text)
        self.assertIn('adreno/a3xx_gpu.o', text)
        self.assertNotIn('adreno/a4xx_gpu.o', text,
                         'a4xx calls adreno_is_a4xx(), which is not defined')
        self.assertIn('adreno/a5xx_gpu.o', text,
                      'a5xx must stay: gpulist references a5xx_gpu_init')
        self.assertIn('hdmi/hdmi.o', text, 'unrelated objects must survive')

    def test_adreno_gpulist_gains_the_adreno_308(self):
        gpu.apply(self.tree)
        text = self.adreno.read_text()
        self.assertIn('ADRENO_REV(3, 0, 6, ANY_ID)', text)
        self.assertIn('a3xx_gpu_init', text)
        self.assertIn('a300_pm4.fw', text)
        self.assertIn('A530', text, 'the existing entries must survive')

    def test_sde_kms_call_is_stubbed(self):
        gpu.apply(self.tree)
        text = self.drv.read_text()
        self.assertNotIn('sde_kms_init(dev)', text)
        self.assertIn('j4primelte: SDE is not built', text)

    def test_makefile_patch_is_idempotent(self):
        gpu.apply(self.tree)
        first = self.makefile.read_text()
        gpu.apply(self.tree)
        self.assertEqual(self.makefile.read_text(), first)


    def test_register_enum_gains_the_missing_a3xx_registers(self):
        gpu.apply(self.tree)
        text = (self.tree / gpu.ADRENO_GPU_H).read_text()
        # a3xx_register_offsets[] is initialised positionally, so the order
        # has to match the order a3xx_gpu.c writes them in.
        lines = [line.strip() for line in text.splitlines()]
        self.assertEqual(lines.index('REG_ADRENO_SCRATCH_ADDR,') - 1,
                         lines.index('REG_ADRENO_CP_ME_RAM_RADDR,'))
        self.assertEqual(lines.index('REG_ADRENO_SCRATCH_UMSK,') - 1,
                         lines.index('REG_ADRENO_SCRATCH_ADDR,'))
        self.assertEqual(lines.index('REG_ADRENO_SCRATCH_REG2,') + 1,
                         lines.index('REG_ADRENO_SQ_GPR_MANAGEMENT,'))
        self.assertIn('REG_ADRENO_REGISTER_MAX,', lines)

    def test_register_patch_is_idempotent(self):
        gpu.apply(self.tree)
        first = (self.tree / gpu.ADRENO_GPU_H).read_text()
        gpu.apply(self.tree)
        self.assertEqual((self.tree / gpu.ADRENO_GPU_H).read_text(), first)


    def test_msm_smmu_becomes_its_own_module(self):
        gpu.apply(self.tree)
        text = self.makefile.read_text()
        # It carries its own module_init(), so linking it into msm.ko would
        # produce a second init_module.
        self.assertIn(gpu.SMMU_OBJ, text)
        objects = text.split('msm-y :=', 1)[1].split('ifneq', 1)[0]
        self.assertNotIn('msm_smmu.o', objects)
        self.assertEqual(text.count('msm_smmu.o'), 1)

    def test_msm_smmu_exports_the_symbol_msm_needs(self):
        gpu.apply(self.tree)
        text = (self.tree / gpu.SMMU).read_text()
        self.assertIn('EXPORT_SYMBOL(msm_smmu_new)', text)
        self.assertIn('msm_smmu_new', text)


class ConfigTests(unittest.TestCase):
    def test_enables_the_drm_stack(self):
        for symbol in ('DRM', 'DRM_MSM', 'DRM_FBDEV_EMULATION', 'DRM_PANEL',
                       'DRM_MIPI_DSI', 'CMA'):
            self.assertIn(symbol, gpu.ENABLE)

    def test_disables_the_two_conflicting_drivers(self):
        # KGSL makes adreno_register() a stub; the fb driver owns mdss_mdp.
        self.assertIn('MSM_KGSL', gpu.DISABLE)
        self.assertIn('FB_MSM_MDSS', gpu.DISABLE)
        self.assertIn('KLAPSE', gpu.DISABLE,
                      'klapse.c links against the MDSS KCAL colour functions')


class WiringTests(unittest.TestCase):
    def test_build_pipeline_runs_the_helper(self):
        script = (HERE / 'build-kernel.sh').read_text()
        self.assertIn('kernel-gpu.py', script)

    def test_driver_is_built_as_a_module_and_blacklisted(self):
        script = (HERE / 'build-kernel.sh').read_text()
        self.assertIn('--module DRM_MSM', script)
        blacklist = HERE / 'rootfs/etc/modprobe.d/j4-drm.conf'
        self.assertTrue(blacklist.is_file(), 'module must not autoload at boot')
        self.assertIn('blacklist msm', blacklist.read_text())

    def test_workflow_runs_these_tests(self):
        workflow = (HERE.parent.parent / '.github/workflows/build-medusa.yml').read_text()
        self.assertIn('test-kernel-gpu.py', workflow)


if __name__ == '__main__':
    unittest.main()
