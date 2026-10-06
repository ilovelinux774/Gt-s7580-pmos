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


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tree = Path(self.temp.name)
        drm = self.tree / gpu.DRM_DIR
        drm.mkdir(parents=True)
        (drm / 'msm_drv.c').write_text(FAKE_DRV)
        self.drv = drm / 'msm_drv.c'

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


class ConfigTests(unittest.TestCase):
    def test_enables_the_drm_stack(self):
        for symbol in ('DRM', 'DRM_MSM', 'DRM_FBDEV_EMULATION', 'DRM_PANEL',
                       'DRM_MIPI_DSI', 'CMA'):
            self.assertIn(symbol, gpu.ENABLE)

    def test_disables_the_two_conflicting_drivers(self):
        # KGSL makes adreno_register() a stub; the fb driver owns mdss_mdp.
        self.assertIn('MSM_KGSL', gpu.DISABLE)
        self.assertIn('FB_MSM_MDSS', gpu.DISABLE)


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
