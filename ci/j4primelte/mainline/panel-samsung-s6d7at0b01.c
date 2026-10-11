// SPDX-License-Identifier: GPL-2.0-only
/*
 * Samsung S6D7AT0B01 (modül PM6003XB1, "CST") 720x1480 video-mode DSI panel
 * Samsung Galaxy J4+ (SM-J415F, j4primelte) - TASLAK, henüz derlenmedi.
 *
 * Init dizisi telefondaki canlı downstream DTB'den
 * linux-mdss-dsi-panel-driver-generator ile üretildi; J4+'a özel kısımlar
 * downstream Samsung kaynağından (ProjectMedusaAndroid msm8917_Q):
 *  - display-on ayrı gönderiliyor: samsung,display_on_tx_cmds_revA
 *  - parlaklık 12 bit: mdss_brightness_tft_pwm()
 *  - güç sırası: mdss_dsi_panel_power_on() + lm3632a_panel_power()
 */

#include <linux/backlight.h>
#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/mod_devicetable.h>
#include <linux/module.h>
#include <linux/regulator/consumer.h>

#include <video/mipi_display.h>

#include <drm/drm_mipi_dsi.h>
#include <drm/drm_modes.h>
#include <drm/drm_panel.h>
#include <drm/drm_probe_helper.h>

#define S6D7AT0B01_MAX_BRIGHTNESS	4095

struct s6d7at0b01 {
	struct drm_panel panel;
	struct mipi_dsi_device *dsi;
	struct regulator *vdd;		/* GPIO 92, samsung,panel-extra-power-gpio1 */
	struct regulator *vsp;		/* LM3632 VPOS (ENP, GPIO 89) */
	struct regulator *vsn;		/* LM3632 VNEG (ENN, GPIO 90) */
	struct regulator_bulk_data *supplies;	/* vci (L17), vddio (L6) */
	struct gpio_desc *reset_gpio;
};

static const struct regulator_bulk_data s6d7at0b01_supplies[] = {
	{ .supply = "vci" },
	{ .supply = "vddio" },
};

static inline struct s6d7at0b01 *to_s6d7at0b01(struct drm_panel *panel)
{
	return container_of_const(panel, struct s6d7at0b01, panel);
}

/* qcom,mdss-dsi-reset-sequence = <1 15>, <0 1>, <1 5> (aktif düşük GPIO) */
static void s6d7at0b01_reset(struct s6d7at0b01 *ctx)
{
	gpiod_set_value_cansleep(ctx->reset_gpio, 0);
	usleep_range(15000, 16000);
	gpiod_set_value_cansleep(ctx->reset_gpio, 1);
	usleep_range(1000, 2000);
	gpiod_set_value_cansleep(ctx->reset_gpio, 0);
	usleep_range(5000, 6000);
}

/* Downstream sırası: extra power -> LM3632 (ENP, 5 ms, ENN) -> L17/L6 */
static int s6d7at0b01_power_on(struct s6d7at0b01 *ctx)
{
	int ret;

	ret = regulator_enable(ctx->vdd);
	if (ret)
		return ret;
	usleep_range(1500, 2000);

	ret = regulator_enable(ctx->vsp);
	if (ret)
		goto err_vdd;
	usleep_range(5000, 6000);

	ret = regulator_enable(ctx->vsn);
	if (ret)
		goto err_vsp;

	ret = regulator_bulk_enable(ARRAY_SIZE(s6d7at0b01_supplies), ctx->supplies);
	if (ret)
		goto err_vsn;

	return 0;

err_vsn:
	regulator_disable(ctx->vsn);
err_vsp:
	regulator_disable(ctx->vsp);
err_vdd:
	regulator_disable(ctx->vdd);
	return ret;
}

/* Downstream kapatma: reset 0 -> ENN, 5 ms, ENP -> extra power -> L17/L6 */
static void s6d7at0b01_power_off(struct s6d7at0b01 *ctx)
{
	gpiod_set_value_cansleep(ctx->reset_gpio, 1);
	regulator_disable(ctx->vsn);
	usleep_range(5000, 6000);
	regulator_disable(ctx->vsp);
	regulator_disable(ctx->vdd);
	regulator_bulk_disable(ARRAY_SIZE(s6d7at0b01_supplies), ctx->supplies);
}

static int s6d7at0b01_on(struct s6d7at0b01 *ctx)
{
	struct mipi_dsi_multi_context dsi_ctx = { .dsi = ctx->dsi };

	/* qcom,mdss-dsi-on-command-state = "dsi_lp_mode" */
	ctx->dsi->mode_flags |= MIPI_DSI_MODE_LPM;

	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xfc, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x71, 0x0a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x73,
				     0xb0, 0x32, 0x10, 0x01, 0x48, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb1, 0x11, 0x32, 0x11, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb3,
				     0x01, 0xb9, 0x07, 0x70, 0x18, 0x68, 0x08,
				     0x08, 0x07, 0x80, 0x00, 0xe7, 0x00, 0xe7,
				     0x00, 0xe7);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xba,
				     0x01, 0x13, 0x02, 0x13, 0x02, 0x13, 0x02,
				     0x88, 0x02, 0x58, 0x00, 0x84, 0x08, 0x43,
				     0x03, 0x45, 0xc1, 0x00, 0x94, 0x00, 0x14,
				     0x00, 0x01, 0x20, 0x01, 0x10, 0xc2, 0x89,
				     0x50, 0x2c, 0x99, 0x46, 0x46, 0x00, 0x01,
				     0xf1, 0x01, 0x01, 0x28, 0x32, 0x38, 0x3f,
				     0x48, 0x50, 0x54, 0xff, 0xff, 0xff, 0xff,
				     0xff, 0xff, 0xff, 0xff, 0xff, 0x2f, 0x48,
				     0x64, 0x98, 0xbf, 0xdf, 0xff, 0xff, 0xff,
				     0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x00,
				     0x00, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00,
				     0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
				     0x00, 0x00, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xbb, 0x21, 0x68, 0x00, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xec,
				     0x00, 0x08, 0x00, 0x07, 0x00, 0x15, 0x14,
				     0x02, 0x0f, 0x0d, 0x0c, 0x0e, 0x05, 0x09,
				     0x06, 0x02, 0x02, 0x00, 0x00, 0x02, 0x03,
				     0x03, 0x02, 0x08, 0x02, 0x07, 0x02, 0x15,
				     0x14, 0x02, 0x0e, 0x0c, 0x0f, 0x0d, 0x04,
				     0x09, 0x06, 0x02, 0x02, 0x00, 0x00, 0x02,
				     0x03, 0x03);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xed,
				     0x12, 0x40, 0x00, 0x15, 0x11, 0x00, 0x0c,
				     0x13, 0x40, 0x00, 0x15, 0x11, 0x00, 0x0c,
				     0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x10,
				     0x00, 0x41, 0x00, 0x00, 0x00, 0x00, 0x10,
				     0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x20,
				     0x00, 0x21, 0x00, 0x00, 0x00, 0x00, 0x10,
				     0x00, 0x40, 0x01, 0x72, 0x61, 0x00, 0xe0,
				     0x20, 0x41, 0x01, 0x72, 0x61, 0x00, 0xc0,
				     0x40, 0x08, 0x92, 0x4c, 0x40, 0x00, 0x80,
				     0x28, 0x00, 0x3c, 0x00, 0x00, 0x00, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xee,
				     0x12, 0xc4, 0x80, 0x19, 0x05, 0x06, 0x00,
				     0x00, 0x68, 0x12, 0xc5, 0x80, 0x19, 0x05,
				     0x06, 0x00, 0x00, 0x68, 0x12, 0xc6, 0x80,
				     0x19, 0x05, 0x06, 0x00, 0x00, 0x6a, 0x12,
				     0xc7, 0x80, 0x19, 0x05, 0x06, 0x00, 0x00,
				     0x6a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xef,
				     0x00, 0x00, 0x00, 0x00, 0x00, 0x0c, 0x40,
				     0x76, 0x76, 0x10, 0x18, 0x09, 0x80, 0x00,
				     0x08, 0x50, 0x10, 0x00, 0x00, 0x48, 0x00,
				     0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
				     0x00, 0x00, 0x11, 0x77, 0x00, 0x22, 0x00,
				     0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
				     0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
				     0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
				     0x00, 0x00, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf2,
				     0x48, 0x23, 0x33, 0x33, 0xe3, 0x44, 0x43,
				     0x00, 0x01, 0x00, 0x00, 0x0d, 0x84, 0x00,
				     0x80, 0x00, 0xa6, 0x1e, 0x08, 0x03, 0x34,
				     0x00, 0x00, 0x00, 0x00, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xb0, 0x02);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf4,
				     0x00, 0x17, 0x8a, 0xde, 0xde, 0x05, 0x0a,
				     0x16, 0x95, 0x55, 0x59, 0x3b, 0x10, 0x55,
				     0x15, 0x26, 0xfa, 0x00, 0x00, 0x00, 0x00,
				     0x00, 0x00, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf9,
				     0x88, 0x88, 0x00, 0xc0, 0xc0, 0x04, 0x7a,
				     0x00, 0x0d, 0xc0, 0x94, 0x0c, 0x4b, 0xa0,
				     0x1f, 0xa0, 0x00, 0x00, 0x00, 0x00, 0x00,
				     0x0c, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00,
				     0x00, 0x00, 0x00, 0x0c, 0x00, 0x11, 0x00,
				     0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x0c,
				     0x00, 0x11, 0x00, 0x00, 0x77, 0xff, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xfe,
				     0x02, 0x00, 0x01, 0x4c, 0x00, 0x00, 0x00,
				     0x60, 0x00, 0x03, 0x00, 0x00, 0x00, 0x00,
				     0x00, 0x00, 0x00, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc0,
				     0x03, 0x08, 0x29, 0x01, 0x00, 0x80, 0x01,
				     0x24, 0x0f, 0xff, 0x30, 0x40, 0x86, 0xda,
				     0x8e);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc1,
				     0xe5, 0xa6, 0x80, 0x25, 0x66, 0xa7, 0xa7,
				     0xac, 0xb3, 0xb3, 0xb3, 0xb3, 0xba, 0xbd,
				     0xbf, 0xbf, 0xc4, 0xd6, 0xe8, 0xff, 0x88,
				     0x96, 0xa5, 0xb4, 0xbe, 0xc8, 0xcd, 0xd0,
				     0xd3, 0xd7, 0xdf, 0xe6, 0xea, 0xf0, 0xf5,
				     0xff);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xc8,
				     0x00, 0x24, 0x00, 0x41, 0x3a, 0x00, 0x00,
				     0x00, 0xb0, 0x02, 0x00, 0x00, 0x00, 0x85,
				     0x48, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
				     0x40, 0x4b);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xe8,
				     0x02, 0x0d, 0x17, 0x13, 0x1a, 0x20, 0x1f,
				     0x21, 0x27, 0x2d, 0x2d, 0x2a, 0x29, 0x27,
				     0x27, 0x2e, 0x30, 0x02, 0x0d, 0x17, 0x13,
				     0x1a, 0x20, 0x1f, 0x21, 0x27, 0x27, 0x27,
				     0x2a, 0x29, 0x27, 0x27, 0x2e, 0x34);
	/* 0x51 FF F1 = 12 bit tam parlaklık (bkz. s6d7at0b01_bl_update_status) */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, MIPI_DCS_SET_DISPLAY_BRIGHTNESS,
				     0xff, 0xf1);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, MIPI_DCS_WRITE_CONTROL_DISPLAY,
				     0x24);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, MIPI_DCS_WRITE_POWER_SAVE, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x11, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xf0, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0xfc, 0xa5, 0xa5);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0xa5, 0xa5);
	mipi_dsi_msleep(&dsi_ctx, 120);

	/* samsung,display_on_tx_cmds_revA: 9F kilidi aç, 0x29, 17 ms, kilitle */
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, MIPI_DCS_SET_DISPLAY_ON, 0x00);
	mipi_dsi_msleep(&dsi_ctx, 17);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0xa5, 0xa5);

	return dsi_ctx.accum_err;
}

static int s6d7at0b01_off(struct s6d7at0b01 *ctx)
{
	struct mipi_dsi_multi_context dsi_ctx = { .dsi = ctx->dsi };

	/* qcom,mdss-dsi-off-command-state = "dsi_hs_mode" */
	ctx->dsi->mode_flags &= ~MIPI_DSI_MODE_LPM;

	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0x5a, 0x5a);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, MIPI_DCS_SET_DISPLAY_OFF, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, MIPI_DCS_ENTER_SLEEP_MODE, 0x00);
	mipi_dsi_dcs_write_seq_multi(&dsi_ctx, 0x9f, 0xa5, 0xa5);
	mipi_dsi_msleep(&dsi_ctx, 84);

	return dsi_ctx.accum_err;
}

static int s6d7at0b01_prepare(struct drm_panel *panel)
{
	struct s6d7at0b01 *ctx = to_s6d7at0b01(panel);
	struct device *dev = &ctx->dsi->dev;
	int ret;

	ret = s6d7at0b01_power_on(ctx);
	if (ret < 0) {
		dev_err(dev, "Failed to enable regulators: %d\n", ret);
		return ret;
	}

	/* qcom,mdss-dsi-init-delay-us = <1000> */
	usleep_range(1000, 2000);
	s6d7at0b01_reset(ctx);

	ret = s6d7at0b01_on(ctx);
	if (ret < 0) {
		dev_err(dev, "Failed to initialize panel: %d\n", ret);
		s6d7at0b01_power_off(ctx);
		return ret;
	}

	return 0;
}

static int s6d7at0b01_unprepare(struct drm_panel *panel)
{
	struct s6d7at0b01 *ctx = to_s6d7at0b01(panel);
	struct device *dev = &ctx->dsi->dev;
	int ret;

	ret = s6d7at0b01_off(ctx);
	if (ret < 0)
		dev_err(dev, "Failed to un-initialize panel: %d\n", ret);

	s6d7at0b01_power_off(ctx);

	return 0;
}

/* DTB: 720x1480, hfp 83, hpw 8, hbp 100, vfp 56, vpw 8, vbp 8, 60 Hz, 66x137 mm */
static const struct drm_display_mode s6d7at0b01_mode = {
	.clock = (720 + 83 + 8 + 100) * (1480 + 56 + 8 + 8) * 60 / 1000,
	.hdisplay = 720,
	.hsync_start = 720 + 83,
	.hsync_end = 720 + 83 + 8,
	.htotal = 720 + 83 + 8 + 100,
	.vdisplay = 1480,
	.vsync_start = 1480 + 56,
	.vsync_end = 1480 + 56 + 8,
	.vtotal = 1480 + 56 + 8 + 8,
	.width_mm = 66,
	.height_mm = 137,
	.type = DRM_MODE_TYPE_DRIVER,
};

static int s6d7at0b01_get_modes(struct drm_panel *panel,
				struct drm_connector *connector)
{
	return drm_connector_helper_get_modes_fixed(connector, &s6d7at0b01_mode);
}

static const struct drm_panel_funcs s6d7at0b01_panel_funcs = {
	.prepare = s6d7at0b01_prepare,
	.unprepare = s6d7at0b01_unprepare,
	.get_modes = s6d7at0b01_get_modes,
};

/*
 * mdss_brightness_tft_pwm(): payload[1] = seviye[11:4],
 * payload[2] = seviye[3:0] << 4 | BIT(0). Tam parlaklık = 51 FF F1.
 * Panel bu değeri PWM olarak LM3632'nin PWM girişine veriyor.
 */
static int s6d7at0b01_bl_update_status(struct backlight_device *bl)
{
	struct mipi_dsi_device *dsi = bl_get_data(bl);
	u16 level = backlight_get_brightness(bl);
	u8 payload[3] = {
		MIPI_DCS_SET_DISPLAY_BRIGHTNESS,
		(level >> 4) & 0xff,
		((level & 0xf) << 4) | BIT(0),
	};
	ssize_t ret;

	dsi->mode_flags &= ~MIPI_DSI_MODE_LPM;
	ret = mipi_dsi_dcs_write_buffer(dsi, payload, sizeof(payload));
	dsi->mode_flags |= MIPI_DSI_MODE_LPM;

	return ret < 0 ? ret : 0;
}

static const struct backlight_ops s6d7at0b01_bl_ops = {
	.update_status = s6d7at0b01_bl_update_status,
};

static struct backlight_device *
s6d7at0b01_create_backlight(struct mipi_dsi_device *dsi)
{
	struct device *dev = &dsi->dev;
	const struct backlight_properties props = {
		.type = BACKLIGHT_RAW,
		.brightness = S6D7AT0B01_MAX_BRIGHTNESS / 2,
		.max_brightness = S6D7AT0B01_MAX_BRIGHTNESS,
	};

	return devm_backlight_device_register(dev, dev_name(dev), dev, dsi,
					      &s6d7at0b01_bl_ops, &props);
}

static int s6d7at0b01_probe(struct mipi_dsi_device *dsi)
{
	struct device *dev = &dsi->dev;
	struct s6d7at0b01 *ctx;
	int ret;

	ctx = devm_drm_panel_alloc(dev, struct s6d7at0b01, panel,
				   &s6d7at0b01_panel_funcs,
				   DRM_MODE_CONNECTOR_DSI);
	if (IS_ERR(ctx))
		return PTR_ERR(ctx);

	ctx->vdd = devm_regulator_get(dev, "vdd");
	if (IS_ERR(ctx->vdd))
		return dev_err_probe(dev, PTR_ERR(ctx->vdd), "Failed to get vdd\n");

	ctx->vsp = devm_regulator_get(dev, "vsp");
	if (IS_ERR(ctx->vsp))
		return dev_err_probe(dev, PTR_ERR(ctx->vsp), "Failed to get vsp\n");

	ctx->vsn = devm_regulator_get(dev, "vsn");
	if (IS_ERR(ctx->vsn))
		return dev_err_probe(dev, PTR_ERR(ctx->vsn), "Failed to get vsn\n");

	ret = devm_regulator_bulk_get_const(dev, ARRAY_SIZE(s6d7at0b01_supplies),
					    s6d7at0b01_supplies, &ctx->supplies);
	if (ret < 0)
		return ret;

	ctx->reset_gpio = devm_gpiod_get(dev, "reset", GPIOD_OUT_HIGH);
	if (IS_ERR(ctx->reset_gpio))
		return dev_err_probe(dev, PTR_ERR(ctx->reset_gpio),
				     "Failed to get reset-gpios\n");

	ctx->dsi = dsi;
	mipi_dsi_set_drvdata(dsi, ctx);

	/* 4 hat, RGB888, burst video, h-sync-pulse, sürekli saat, EOT ekle */
	dsi->lanes = 4;
	dsi->format = MIPI_DSI_FMT_RGB888;
	dsi->mode_flags = MIPI_DSI_MODE_VIDEO | MIPI_DSI_MODE_VIDEO_BURST |
			  MIPI_DSI_MODE_VIDEO_HSE;

	ctx->panel.prepare_prev_first = true;

	ctx->panel.backlight = s6d7at0b01_create_backlight(dsi);
	if (IS_ERR(ctx->panel.backlight))
		return dev_err_probe(dev, PTR_ERR(ctx->panel.backlight),
				     "Failed to create backlight\n");

	drm_panel_add(&ctx->panel);

	ret = mipi_dsi_attach(dsi);
	if (ret < 0) {
		drm_panel_remove(&ctx->panel);
		return dev_err_probe(dev, ret, "Failed to attach to DSI host\n");
	}

	return 0;
}

static void s6d7at0b01_remove(struct mipi_dsi_device *dsi)
{
	struct s6d7at0b01 *ctx = mipi_dsi_get_drvdata(dsi);
	int ret;

	ret = mipi_dsi_detach(dsi);
	if (ret < 0)
		dev_err(&dsi->dev, "Failed to detach from DSI host: %d\n", ret);

	drm_panel_remove(&ctx->panel);
}

static const struct of_device_id s6d7at0b01_of_match[] = {
	{ .compatible = "samsung,s6d7at0b01-pm6003xb1" },
	{ /* sentinel */ }
};
MODULE_DEVICE_TABLE(of, s6d7at0b01_of_match);

static struct mipi_dsi_driver s6d7at0b01_driver = {
	.probe = s6d7at0b01_probe,
	.remove = s6d7at0b01_remove,
	.driver = {
		.name = "panel-samsung-s6d7at0b01",
		.of_match_table = s6d7at0b01_of_match,
	},
};
module_mipi_dsi_driver(s6d7at0b01_driver);

MODULE_DESCRIPTION("DRM driver for Samsung S6D7AT0B01 PM6003XB1 panel (Galaxy J4+)");
MODULE_LICENSE("GPL");
