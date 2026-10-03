/*
 * Bluetooth HCI driver for the Qualcomm WCNSS Bluetooth core
 * (WCN3620 "pronto"), transported over the two SMD channels on the
 * APPS <-> WCNSS edge:
 *
 *   APPS_RIVA_BT_CMD   HCI commands (tx) / HCI events (rx)
 *   APPS_RIVA_BT_ACL   ACL data
 *
 * Why this file exists: the downstream msm8917 kernel used by the Samsung
 * Galaxy J4+ (j4primelte) enables CONFIG_BT, CONFIG_BT_HIDP and CONFIG_UHID
 * but no HCI transport at all - BT_HCIUART, BT_HCIBTUSB, BT_HCIBTSDIO are all
 * disabled in the stock j4primelte_defconfig and the tree carries no
 * btqcomsmd.c. The Bluetooth core inside the WCNSS is therefore unreachable
 * and no hci device is ever created. This driver fills that gap.
 *
 * The channels only exist after the WCNSS firmware has booted (on postmarketOS
 * that happens when j4-wifi triggers the wcnss PIL), so the open is retried
 * from a delayed work queue and can be re-triggered at any time by writing 1
 * to /sys/module/hci_smd/parameters/enable.
 *
 * The public address can be programmed with the QCA EDL NVM write command by
 * passing it as a module parameter, e.g. hci_smd.bdaddr=02:11:22:33:44:55
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/slab.h>
#include <linux/skbuff.h>
#include <linux/workqueue.h>
#include <linux/delay.h>

#include <soc/qcom/smd.h>

#include <net/bluetooth/bluetooth.h>
#include <net/bluetooth/hci_core.h>

#ifndef hci_skb_pkt_type
#define hci_skb_pkt_type(skb) bt_cb((skb))->pkt_type
#endif

/* Not defined by this kernel's hci.h, which stops at HCI_SDIO. */
#ifndef HCI_SMD
#define HCI_SMD 7
#endif

/* QCA EDL (Embedded Download) NVM access; see mainline drivers/bluetooth/btqca.c */
#define EDL_NVM_ACCESS_OPCODE		(0xFC0B)
#define EDL_NVM_ACCESS_SET_REQ_CMD	(0x01)
#define EDL_NVM_TAG_ID_BDADDR		(0x02)

#define BT_SMD_ACL_NAME "APPS_RIVA_BT_ACL"
#define BT_SMD_CMD_NAME "APPS_RIVA_BT_CMD"
#define BT_SMD_EDGE     SMD_APPS_WCNSS

#define BT_SMD_MAX_TRIES 120
#define BT_SMD_RETRY_MS  2000

enum bt_smd_state {
	BT_SMD_STATE_IDLE = 0,
	BT_SMD_STATE_WAITING = 1,
	BT_SMD_STATE_UP = 2,
};

struct bt_smd_chan {
	smd_channel_t *ch;
	const char *name;
	struct bt_smd *bs;
	bool opened;
	__u8 rx_type;
};

struct bt_smd {
	struct hci_dev *hdev;
	struct bt_smd_chan acl;
	struct bt_smd_chan cmd;
	struct delayed_work work;
	int tries;
	int state;
	bool registered;
	bool bdaddr_set;
	bdaddr_t bdaddr;
};

static struct bt_smd bt_smd_dev;
static char *bt_smd_bdaddr;
static int bt_smd_enable;

static void bt_smd_rx(struct bt_smd_chan *chan)
{
	struct bt_smd *bs = chan->bs;
	struct hci_dev *hdev = bs->hdev;
	struct sk_buff *skb;
	int size, len;

	if (!chan->ch || !hdev)
		return;

	/* Read one HCI frame at a time. smd_read_avail() reports the bytes of
	 * the frame currently in the channel, which is what the downstream
	 * hci_smd implementation uses as well. */
	while ((size = smd_read_avail(chan->ch)) > 0) {
		if (size > HCI_MAX_FRAME_SIZE) {
			pr_err("hci_smd: oversized frame on %s (%d bytes)\n",
			       chan->name, size);
			smd_read_from_cb(chan->ch, NULL, size);
			hdev->stat.err_rx++;
			break;
		}

		skb = bt_skb_alloc(size, GFP_ATOMIC);
		if (!skb) {
			smd_read_from_cb(chan->ch, NULL, size);
			hdev->stat.err_rx++;
			break;
		}

		hci_skb_pkt_type(skb) = chan->rx_type;
		len = smd_read_from_cb(chan->ch, skb_put(skb, size), size);
		if (len != size) {
			kfree_skb(skb);
			hdev->stat.err_rx++;
			break;
		}

		hdev->stat.byte_rx += size;
		if (hci_recv_frame(hdev, skb) < 0)
			hdev->stat.err_rx++;
	}
}

static void bt_smd_notify(void *priv, unsigned int event)
{
	struct bt_smd_chan *chan = priv;

	switch (event) {
	case SMD_EVENT_OPEN:
		pr_info("hci_smd: channel %s opened\n", chan->name);
		chan->opened = true;
		break;
	case SMD_EVENT_DATA:
		bt_smd_rx(chan);
		break;
	case SMD_EVENT_CLOSE:
		pr_info("hci_smd: channel %s closed\n", chan->name);
		chan->opened = false;
		break;
	default:
		break;
	}
}

static int bt_smd_write(struct bt_smd_chan *chan, struct sk_buff *skb)
{
	int ret;

	if (!chan->ch)
		return -ENODEV;

	if (smd_write_avail(chan->ch) < (int)skb->len)
		return -EBUSY;

	ret = smd_write(chan->ch, skb->data, skb->len);
	if (ret < 0)
		return ret;

	return 0;
}

static int bt_smd_send(struct hci_dev *hdev, struct sk_buff *skb)
{
	struct bt_smd *bs = hci_get_drvdata(hdev);
	int ret;

	switch (hci_skb_pkt_type(skb)) {
	case HCI_COMMAND_PKT:
		ret = bt_smd_write(&bs->cmd, skb);
		if (!ret)
			hdev->stat.cmd_tx++;
		break;
	case HCI_ACLDATA_PKT:
		ret = bt_smd_write(&bs->acl, skb);
		if (!ret) {
			hdev->stat.acl_tx++;
			hdev->stat.byte_tx += skb->len;
		}
		break;
	default:
		ret = -EILSEQ;
		break;
	}

	kfree_skb(skb);
	return ret;
}

static int bt_smd_open(struct hci_dev *hdev)
{
	return 0;
}

static int bt_smd_close(struct hci_dev *hdev)
{
	return 0;
}

static int bt_smd_parse_bdaddr(const char *str, bdaddr_t *out)
{
	unsigned int b[6];

	if (!str || !*str)
		return -EINVAL;

	if (sscanf(str, "%02x:%02x:%02x:%02x:%02x:%02x",
		   &b[0], &b[1], &b[2], &b[3], &b[4], &b[5]) != 6)
		return -EINVAL;

	out->b[0] = b[5];
	out->b[1] = b[4];
	out->b[2] = b[3];
	out->b[3] = b[2];
	out->b[4] = b[1];
	out->b[5] = b[0];
	return 0;
}

/* Program the public address with the QCA EDL NVM write command. */
static int bt_smd_program_bdaddr(struct hci_dev *hdev, const bdaddr_t *bdaddr)
{
	struct sk_buff *skb;
	u8 cmd[9];
	int err;

	cmd[0] = EDL_NVM_ACCESS_SET_REQ_CMD;
	cmd[1] = EDL_NVM_TAG_ID_BDADDR;
	cmd[2] = sizeof(bdaddr_t);
	memcpy(cmd + 3, bdaddr, sizeof(bdaddr_t));

	skb = __hci_cmd_sync_ev(hdev, EDL_NVM_ACCESS_OPCODE, sizeof(cmd), cmd,
				HCI_VENDOR_PKT, HCI_INIT_TIMEOUT);
	if (IS_ERR(skb)) {
		err = PTR_ERR(skb);
		pr_err("hci_smd: %s: public address command failed (%d)\n",
		       hdev->name, err);
		return err;
	}

	kfree_skb(skb);
	pr_info("hci_smd: %s: public address programmed\n", hdev->name);
	return 0;
}

static int bt_smd_setup(struct hci_dev *hdev)
{
	struct bt_smd *bs = hci_get_drvdata(hdev);
	int ret;

	if (!bs->bdaddr_set)
		return 0;

	/* A failure here must not keep the controller from coming up; the
	 * address can still be programmed later through the set_bdaddr
	 * callback (btmgmt public-addr + power cycle). */
	ret = bt_smd_program_bdaddr(hdev, &bs->bdaddr);
	if (ret)
		pr_err("hci_smd: continuing without a programmed address\n");

	return 0;
}

static int bt_smd_set_bdaddr(struct hci_dev *hdev, const bdaddr_t *bdaddr)
{
	return bt_smd_program_bdaddr(hdev, bdaddr);
}

static int bt_smd_register(struct bt_smd *bs)
{
	struct hci_dev *hdev;
	int ret;

	hdev = hci_alloc_dev();
	if (!hdev)
		return -ENOMEM;

	hci_set_drvdata(hdev, bs);
	bs->hdev = hdev;

	hdev->bus = HCI_SMD;
	hdev->dev_type = HCI_BREDR;
	hdev->open = bt_smd_open;
	hdev->close = bt_smd_close;
	hdev->send = bt_smd_send;
	hdev->setup = bt_smd_setup;
	hdev->set_bdaddr = bt_smd_set_bdaddr;
	set_bit(HCI_SETUP, &hdev->dev_flags);

	ret = hci_register_dev(hdev);
	if (ret < 0) {
		pr_err("hci_smd: hci_register_dev failed (%d)\n", ret);
		hci_free_dev(hdev);
		bs->hdev = NULL;
		return ret;
	}

	bs->registered = true;
	bs->state = BT_SMD_STATE_UP;
	pr_info("hci_smd: registered %s over WCNSS SMD\n", hdev->name);
	return 0;
}

static void bt_smd_close_channels(struct bt_smd *bs)
{
	if (bs->cmd.ch) {
		smd_close(bs->cmd.ch);
		bs->cmd.ch = NULL;
		bs->cmd.opened = false;
	}
	if (bs->acl.ch) {
		smd_close(bs->acl.ch);
		bs->acl.ch = NULL;
		bs->acl.opened = false;
	}
}

static int bt_smd_open_channels(struct bt_smd *bs)
{
	int ret;

	ret = smd_named_open_on_edge(BT_SMD_ACL_NAME, BT_SMD_EDGE,
				     &bs->acl.ch, &bs->acl, bt_smd_notify);
	if (ret) {
		bs->acl.ch = NULL;
		return ret;
	}

	ret = smd_named_open_on_edge(BT_SMD_CMD_NAME, BT_SMD_EDGE,
				     &bs->cmd.ch, &bs->cmd, bt_smd_notify);
	if (ret) {
		bs->cmd.ch = NULL;
		bt_smd_close_channels(bs);
		return ret;
	}

	pr_info("hci_smd: WCNSS BT channels opened\n");
	return 0;
}

static void bt_smd_work(struct work_struct *work)
{
	struct bt_smd *bs = container_of(work, struct bt_smd, work.work);
	int ret;

	if (bs->registered)
		return;

	if (bt_smd_bdaddr && !bs->bdaddr_set) {
		if (!bt_smd_parse_bdaddr(bt_smd_bdaddr, &bs->bdaddr))
			bs->bdaddr_set = true;
		else
			pr_err("hci_smd: ignoring malformed bdaddr '%s'\n",
			       bt_smd_bdaddr);
	}

	ret = bt_smd_open_channels(bs);
	if (!ret) {
		ret = bt_smd_register(bs);
		if (!ret)
			return;
		bt_smd_close_channels(bs);
	}

	bs->state = BT_SMD_STATE_WAITING;
	bs->tries++;
	if (bs->tries >= BT_SMD_MAX_TRIES) {
		pr_err("hci_smd: WCNSS BT channels not available after %d tries\n",
		       bs->tries);
		return;
	}

	schedule_delayed_work(&bs->work, msecs_to_jiffies(BT_SMD_RETRY_MS));
}

static int bt_smd_enable_set(const char *val, const struct kernel_param *kp)
{
	int enable = 0;
	int ret;

	ret = kstrtoint(val, 0, &enable);
	if (ret)
		return ret;

	if (enable && !bt_smd_dev.registered) {
		bt_smd_dev.tries = 0;
		schedule_delayed_work(&bt_smd_dev.work, 0);
	}

	return 0;
}

static const struct kernel_param_ops bt_smd_enable_ops = {
	.set = bt_smd_enable_set,
	.get = param_get_int,
};

module_param_cb(enable, &bt_smd_enable_ops, &bt_smd_enable, 0644);
MODULE_PARM_DESC(enable, "Write 1 to (re)start looking for the WCNSS BT channels");

module_param(bt_smd_bdaddr, charp, 0644);
MODULE_PARM_DESC(bt_smd_bdaddr, "Public address to program, e.g. 02:11:22:33:44:55");

module_param_named(state, bt_smd_dev.state, int, 0444);
MODULE_PARM_DESC(state, "0 idle, 1 waiting for the WCNSS BT channels, 2 up");

static int __init bt_smd_init(void)
{
	INIT_DELAYED_WORK(&bt_smd_dev.work, bt_smd_work);

	bt_smd_dev.acl.name = BT_SMD_ACL_NAME;
	bt_smd_dev.acl.bs = &bt_smd_dev;
	bt_smd_dev.acl.rx_type = HCI_ACLDATA_PKT;

	bt_smd_dev.cmd.name = BT_SMD_CMD_NAME;
	bt_smd_dev.cmd.bs = &bt_smd_dev;
	bt_smd_dev.cmd.rx_type = HCI_EVENT_PKT;

	bt_smd_dev.tries = 0;
	bt_smd_dev.state = BT_SMD_STATE_WAITING;

	/* The WCNSS firmware - and with it these two channels - only exists
	 * once userspace boots it, so start probing a little later and keep
	 * retrying until it shows up. */
	schedule_delayed_work(&bt_smd_dev.work, msecs_to_jiffies(10000));

	pr_info("hci_smd: WCNSS Bluetooth HCI over SMD registered\n");
	return 0;
}

static void __exit bt_smd_exit(void)
{
	cancel_delayed_work_sync(&bt_smd_dev.work);

	if (bt_smd_dev.registered) {
		hci_unregister_dev(bt_smd_dev.hdev);
		hci_free_dev(bt_smd_dev.hdev);
		bt_smd_dev.hdev = NULL;
		bt_smd_dev.registered = false;
	}

	bt_smd_close_channels(&bt_smd_dev);
	bt_smd_dev.state = BT_SMD_STATE_IDLE;
}

module_init(bt_smd_init);
module_exit(bt_smd_exit);

MODULE_AUTHOR("j4primelte postmarketOS bring-up");
MODULE_DESCRIPTION("Bluetooth HCI driver for the Qualcomm WCNSS (SMD) core");
MODULE_LICENSE("GPL v2");
