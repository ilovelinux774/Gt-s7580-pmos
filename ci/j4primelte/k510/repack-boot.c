/*
 * repack-boot: read, validate and build Android v0 boot images.
 *
 * Written because every off-the-shelf mkbootimg we could get into CI either
 * pulls in a whole Python environment or silently "fixes" things it should
 * not. This tool does exactly two things and refuses to guess:
 *
 *   repack-boot inspect  <boot.img>
 *       Print the header, decode the payloads and check that the declared
 *       sizes match the file, that the magic is ANDROID!, and that page-size
 *       arithmetic lines up. Exit 1 on any inconsistency.
 *
 *   repack-boot build --kernel K --ramdisk R --out O
 *                      [--page-size N] [--kernel-addr A] [--ramdisk-addr A]
 *                      [--second-addr A] [--tags-addr A] [--cmdline S]
 *                      [--board S] [--no-second]
 *       Write a v0 boot image. --out is never overwritten unless --force.
 *
 *   repack-boot repack <donor.img> --kernel-new K --out O [--dt QCDT]
 *                      [--cmdline-append S] [--cmdline-replace S]
 *       Copy the donor's v0 header/ramdisk and replace its kernel and optional
 *       Qualcomm DT table. On this legacy Qualcomm format, the word at header
 *       offset 40 is dt_size and QCDT follows the ramdisk/second-stage payload.
 *       The output is read back and validated before it is kept.
 *
 * Only the v0 boot layout used by the J4+ is supported. Android's later header
 * versions are not silently interpreted as Qualcomm dt_size fields.
 *
 * Addresses are accepted as hex (0x...) or decimal.
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define ANDROID_MAGIC "ANDROID!"
#define MAGIC_LEN 8
#define CMDLINE_MAX 512
#define NAME_MAX_BOOT 16
#define ID_LEN 32
#define HEADER_LEN 1632

static const char *prog = "repack-boot";

struct header {
	uint32_t kernel_size;
	uint32_t kernel_addr;
	uint32_t ramdisk_size;
	uint32_t ramdisk_addr;
	uint32_t second_size;
	uint32_t second_addr;
	uint32_t tags_addr;
	uint32_t page_size;
	uint32_t dt_size;
	uint32_t unused;
	char name[NAME_MAX_BOOT];
	char cmdline[CMDLINE_MAX];
	char id[ID_LEN];
	char extra_cmdline[CMDLINE_MAX];
};

static void die(const char *fmt, ...) __attribute__((noreturn, format(printf, 1, 2)));

#include <stdarg.h>
static void die(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	fprintf(stderr, "%s: error: ", prog);
	vfprintf(stderr, fmt, ap);
	fputc('\n', stderr);
	va_end(ap);
	exit(1);
}

static uint32_t get32(const unsigned char *p)
{
	return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) |
	       ((uint32_t)p[3] << 24);
}

static void put32(unsigned char *p, uint32_t v)
{
	p[0] = v & 0xff;
	p[1] = (v >> 8) & 0xff;
	p[2] = (v >> 16) & 0xff;
	p[3] = (v >> 24) & 0xff;
}

static uint32_t get_be32(const unsigned char *p)
{
	return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 |
	       (uint32_t)p[2] << 8 | (uint32_t)p[3];
}

/* Validate the legacy Qualcomm QCDT container used after the boot ramdisk. */
static uint32_t validate_qcdt(const unsigned char *dt, size_t len, uint32_t page)
{
	if (len < 16 || memcmp(dt, "QCDT", 4) != 0)
		die("dt_size is nonzero but the Qualcomm DT payload lacks QCDT magic");
	uint32_t version = get32(dt + 4);
	uint32_t count = get32(dt + 8);
	uint32_t record_size;
	switch (version) {
	case 1: record_size = 20; break;
	case 2: record_size = 24; break;
	case 3: record_size = 40; break;
	default: die("unsupported QCDT version %u", version);
	}
	if (count == 0 || count > (len - 16) / record_size)
		die("invalid QCDT record count %u", count);
	size_t records_end = 12 + (size_t)count * record_size;
	if (records_end + 4 > len || get32(dt + records_end) != 0)
		die("QCDT end-of-table marker is missing or truncated");
	if (len % page != 0)
		die("QCDT size %zu is not aligned to boot page size %u", len, page);
	for (uint32_t i = 0; i < count; i++) {
		const unsigned char *record = dt + 12 + (size_t)i * record_size;
		uint32_t offset = get32(record + record_size - 8);
		uint32_t size = get32(record + record_size - 4);
		if (!size || offset % page || size % page ||
		    (size_t)offset + size > len)
			die("QCDT record %u has an invalid offset/size", i);
		if (get_be32(dt + offset) != 0xd00dfeed)
			die("QCDT record %u does not point to an FDT", i);
		uint32_t fdt_size = get_be32(dt + offset + 4);
		if (fdt_size < 40 || fdt_size > size)
			die("QCDT record %u has an invalid FDT totalsize", i);
	}
	return count;
}

static void parse_header(const unsigned char *buf, struct header *h)
{
	if (memcmp(buf, ANDROID_MAGIC, MAGIC_LEN) != 0)
		die("not an Android boot image (missing ANDROID! magic)");
	h->kernel_size = get32(buf + 8);
	h->kernel_addr = get32(buf + 12);
	h->ramdisk_size = get32(buf + 16);
	h->ramdisk_addr = get32(buf + 20);
	h->second_size = get32(buf + 24);
	h->second_addr = get32(buf + 28);
	h->tags_addr = get32(buf + 32);
	h->page_size = get32(buf + 36);
	h->dt_size = get32(buf + 40);
	h->unused = get32(buf + 44);
	memcpy(h->name, buf + 48, NAME_MAX_BOOT);
	memcpy(h->cmdline, buf + 64, CMDLINE_MAX);
	memcpy(h->id, buf + 64 + CMDLINE_MAX, ID_LEN);
	memcpy(h->extra_cmdline, buf + 64 + CMDLINE_MAX + ID_LEN, CMDLINE_MAX);
	/* Make the fixed-size fields safe to printf(). */
	h->name[NAME_MAX_BOOT - 1] = '\0';
	h->cmdline[CMDLINE_MAX - 1] = '\0';
	h->extra_cmdline[CMDLINE_MAX - 1] = '\0';
}

static unsigned char *read_file(const char *path, size_t *len)
{
	FILE *f = fopen(path, "rb");
	if (!f)
		die("cannot open %s: %s", path, strerror(errno));
	if (fseek(f, 0, SEEK_END) != 0)
		die("cannot seek %s", path);
	long n = ftell(f);
	if (n < 0)
		die("cannot size %s", path);
	rewind(f);
	unsigned char *buf = malloc((size_t)n ? (size_t)n : 1);
	if (!buf)
		die("out of memory");
	if (fread(buf, 1, (size_t)n, f) != (size_t)n)
		die("short read on %s", path);
	fclose(f);
	*len = (size_t)n;
	return buf;
}

static uint32_t round_up(uint32_t value, uint32_t multiple)
{
	if (multiple == 0)
		return value;
	uint32_t rem = value % multiple;
	return rem ? value + (multiple - rem) : value;
}

/* --- inspect ------------------------------------------------------------ */

static void print_pages(const char *label, const unsigned char *buf, size_t len,
			uint32_t max_show)
{
	/* Show that a payload looks like something, not just a byte count. */
	printf("  %-9s %8zu bytes", label, len);
	if (len >= 2 && buf[0] == 0x1f && buf[1] == 0x8b)
		printf("  (gzip)");
	else if (len >= 4 && memcmp(buf, "\xfd" "7zXZ", 4) == 0)
		printf("  (xz)");
	else if (len >= 4 && (get32(buf) & 0xffff) == 0x0403)
		printf("  (lz4 frame)");
	else if (len >= 4 && memcmp(buf, "ANDROID!", 8) == 0)
		printf("  (nested boot image?)");
	else if (len >= 4 && get32(buf) == 0xd00dfeed)
		printf("  (device tree)");
	else if (len >= 4 && (get32(buf) & 0xff) == 0x27 && (get32(buf) >> 24) == 0x1f)
		printf("  (xz, headerless)");
	if (max_show && len > max_show)
		printf("  [offset 0x%zx]", (size_t)0);
	printf("\n");
}

static int cmd_inspect(int argc, char **argv)
{
	if (argc < 1)
		die("usage: inspect <boot.img>");
	size_t len;
	unsigned char *buf = read_file(argv[0], &len);
	struct header h;
	parse_header(buf, &h);

	if (h.page_size == 0 || (h.page_size & (h.page_size - 1)) != 0)
		die("page_size=%u is not a power of two", h.page_size);

	size_t k_off = h.page_size;
	size_t r_off = k_off + round_up(h.kernel_size, h.page_size);
	size_t s_off = r_off + round_up(h.ramdisk_size, h.page_size);
	size_t end = s_off + round_up(h.second_size, h.page_size);

	printf("%s\n", argv[0]);
	printf("  size          %zu bytes\n", len);
	printf("  page_size     %u\n", h.page_size);
	printf("  kernel        %u bytes at 0x%08x (file offset %zu)\n",
	       h.kernel_size, h.kernel_addr, k_off);
	printf("  ramdisk       %u bytes at 0x%08x (file offset %zu)\n",
	       h.ramdisk_size, h.ramdisk_addr, r_off);
	printf("  second        %u bytes at 0x%08x (file offset %zu)\n",
	       h.second_size, h.second_addr, s_off);
	printf("  tags_addr     0x%08x\n", h.tags_addr);
	printf("  name          \"%s\"\n", h.name);
	printf("  cmdline       \"%s\"\n", h.cmdline);
	if (h.extra_cmdline[0])
		printf("  extra_cmdline \"%s\"\n", h.extra_cmdline);

	print_pages("kernel", buf + k_off, h.kernel_size, 0);
	print_pages("ramdisk", buf + r_off, h.ramdisk_size, 0);

	/* --- Qualcomm v0 DT table and image consistency ------------------- */
	int bad = 0;
	if (end > len) {
		fprintf(stderr, "  MISMATCH: header declares %zu bytes, file is %zu\n", end, len);
		bad = 1;
	}
	if (h.dt_size) {
		if (end + h.dt_size > len) {
			fprintf(stderr, "  MISMATCH: dt_size declares %u bytes at offset %zu, file is %zu\n",
				h.dt_size, end, len);
			bad = 1;
		} else {
			uint32_t entries = validate_qcdt(buf + end, h.dt_size, h.page_size);
			printf("  qcdt          v%u, %u match records, %u bytes at offset %zu\n",
			       get32(buf + end + 4), entries, h.dt_size, end);
		}
	} else if (len > end + h.page_size) {
		fprintf(stderr, "  NOTE: %zu trailing bytes after the last payload (dt_size is zero)\n",
			len - end);
	}
	if (h.kernel_size == 0)
		die("kernel payload is empty");
	if (h.ramdisk_size == 0)
		fprintf(stderr, "  NOTE: no ramdisk (kernel would need a built-in initramfs)\n");

	int have_id = 0;
	for (int i = 0; i < ID_LEN; i++)
		if (h.id[i] && h.id[i] != '0')
			have_id = 1;
	printf("  sha1 id       %s\n", have_id ? "present (not re-verified)" : "absent");

	free(buf);
	if (bad)
		return 1;
	printf("  OK\n");
	return 0;
}

/* --- build -------------------------------------------------------------- */

static uint32_t parse_addr(const char *s)
{
	char *end = NULL;
	errno = 0;
	unsigned long v = strtoul(s, &end, 0);
	if (errno || !end || *end != '\0' || v > 0xffffffffUL)
		die("bad address: %s", s);
	return (uint32_t)v;
}

/* Write `data` padded to `page`, return bytes written. */
static size_t write_padded(FILE *f, const unsigned char *data, size_t len, uint32_t page)
{
	if (len && fwrite(data, 1, len, f) != len)
		die("short write");
	size_t pad = round_up((uint32_t)len, page) - len;
	for (size_t i = 0; i < pad; i++)
		if (fputc(0, f) == EOF)
			die("short write while padding");
	return len + pad;
}

/* Minimal SHA-1, used only for the header's informational id field. */
struct sha1 { uint32_t h[5]; uint64_t bytes; unsigned char buf[64]; size_t fill; };

static uint32_t rol32(uint32_t v, int n) { return (v << n) | (v >> (32 - n)); }

static void sha1_block(struct sha1 *s, const unsigned char *p)
{
	uint32_t w[80];
	for (int i = 0; i < 16; i++)
		w[i] = (uint32_t)p[i * 4] << 24 | (uint32_t)p[i * 4 + 1] << 16 |
		       (uint32_t)p[i * 4 + 2] << 8 | p[i * 4 + 3];
	for (int i = 16; i < 80; i++)
		w[i] = rol32(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
	uint32_t a = s->h[0], b = s->h[1], c = s->h[2], d = s->h[3], e = s->h[4];
	for (int i = 0; i < 80; i++) {
		uint32_t f, k;
		if (i < 20) { f = (b & c) | (~b & d); k = 0x5a827999; }
		else if (i < 40) { f = b ^ c ^ d; k = 0x6ed9eba1; }
		else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8f1bbcdc; }
		else { f = b ^ c ^ d; k = 0xca62c1d6; }
		uint32_t t = rol32(a, 5) + f + e + k + w[i];
		e = d; d = c; c = rol32(b, 30); b = a; a = t;
	}
	s->h[0] += a; s->h[1] += b; s->h[2] += c; s->h[3] += d; s->h[4] += e;
}

static void sha1_init(struct sha1 *s)
{
	s->h[0] = 0x67452301; s->h[1] = 0xefcdab89; s->h[2] = 0x98badcfe;
	s->h[3] = 0x10325476; s->h[4] = 0xc3d2e1f0;
	s->bytes = 0; s->fill = 0;
}

static void sha1_update(struct sha1 *s, const unsigned char *p, size_t n)
{
	s->bytes += n;
	while (n--) {
		s->buf[s->fill++] = *p++;
		if (s->fill == 64) { sha1_block(s, s->buf); s->fill = 0; }
	}
}

static void sha1_final(struct sha1 *s, unsigned char out[20])
{
	uint64_t bits = s->bytes * 8;
	unsigned char pad = 0x80;
	sha1_update(s, &pad, 1);
	unsigned char zero = 0;
	while (s->fill != 56) sha1_update(s, &zero, 1);
	unsigned char lenbe[8];
	for (int i = 0; i < 8; i++) lenbe[i] = (unsigned char)(bits >> (56 - 8 * i));
	sha1_update(s, lenbe, 8);
	for (int i = 0; i < 5; i++) {
		out[i * 4] = (unsigned char)(s->h[i] >> 24);
		out[i * 4 + 1] = (unsigned char)(s->h[i] >> 16);
		out[i * 4 + 2] = (unsigned char)(s->h[i] >> 8);
		out[i * 4 + 3] = (unsigned char)s->h[i];
	}
}

/* SHA-1 over exactly what AOSP's mkbootimg hashes. */
static void boot_id(const unsigned char *k, size_t klen, const unsigned char *r,
		    size_t rlen, const unsigned char *s, size_t slen,
		    const unsigned char *dt, size_t dtlen, unsigned char out[20])
{
	struct sha1 st;
	unsigned char b[4];
	sha1_init(&st);
	sha1_update(&st, k, klen);
	put32(b, (uint32_t)klen);
	sha1_update(&st, b, 4);
	sha1_update(&st, r, rlen);
	put32(b, (uint32_t)rlen);
	sha1_update(&st, b, 4);
	if (s) {
		sha1_update(&st, s, slen);
		put32(b, (uint32_t)slen);
		sha1_update(&st, b, 4);
	}
	if (dt) {
		sha1_update(&st, dt, dtlen);
		put32(b, (uint32_t)dtlen);
		sha1_update(&st, b, 4);
	}
	sha1_final(&st, out);
}

static int cmd_build(int argc, char **argv)
{
	const char *kernel = NULL, *ramdisk = NULL, *out = NULL, *second = NULL;
	const char *cmdline = "", *board = "";
	uint32_t page = 2048, kaddr = 0x8000, raddr = 0x2000000, taddr = 0x100, saddr = 0xf00000;
	int have_second = 0, force = 0;

	for (int i = 0; i < argc; i++) {
		const char *a = argv[i];
		const char **slot = NULL;
		if (!strcmp(a, "--kernel") && i + 1 < argc) slot = &kernel;
		else if (!strcmp(a, "--ramdisk") && i + 1 < argc) slot = &ramdisk;
		else if (!strcmp(a, "--out") && i + 1 < argc) slot = &out;
		else if (!strcmp(a, "--second") && i + 1 < argc) { slot = &second; have_second = 1; }
		else if (!strcmp(a, "--cmdline") && i + 1 < argc) { cmdline = argv[++i]; continue; }
		else if (!strcmp(a, "--board") && i + 1 < argc) { board = argv[++i]; continue; }
		else if (!strcmp(a, "--page-size") && i + 1 < argc) { page = parse_addr(argv[++i]); continue; }
		else if (!strcmp(a, "--kernel-addr") && i + 1 < argc) { kaddr = parse_addr(argv[++i]); continue; }
		else if (!strcmp(a, "--ramdisk-addr") && i + 1 < argc) { raddr = parse_addr(argv[++i]); continue; }
		else if (!strcmp(a, "--tags-addr") && i + 1 < argc) { taddr = parse_addr(argv[++i]); continue; }
		else if (!strcmp(a, "--second-addr") && i + 1 < argc) { saddr = parse_addr(argv[++i]); continue; }
		else if (!strcmp(a, "--no-second")) { have_second = 0; continue; }
		else if (!strcmp(a, "--force")) { force = 1; continue; }
		else die("unknown or incomplete argument: %s", a);
		if (slot) { *slot = argv[++i]; }
	}
	if (!kernel) die("--kernel is required");
	if (!ramdisk) die("--ramdisk is required");
	if (!out) die("--out is required");
	if (strlen(cmdline) >= CMDLINE_MAX) die("cmdline is longer than %d bytes", CMDLINE_MAX - 1);
	if (strlen(board) >= NAME_MAX_BOOT) die("board name is longer than %d bytes", NAME_MAX_BOOT - 1);
	if (page != 2048 && page != 4096) die("page-size must be 2048 or 4096, got %u", page);
	if (!force) {
		struct stat st;
		if (stat(out, &st) == 0)
			die("%s already exists (pass --force to replace it)", out);
	}

	size_t klen, rlen = 0, slen = 0;
	unsigned char *kbuf = read_file(kernel, &klen);
	unsigned char *rbuf = read_file(ramdisk, &rlen);
	unsigned char *sbuf = have_second ? read_file(second, &slen) : NULL;
	if (klen == 0) die("kernel payload is empty");
	if (rlen == 0) die("ramdisk payload is empty");

	unsigned char hdr[HEADER_LEN];
	memset(hdr, 0, sizeof(hdr));
	memcpy(hdr, ANDROID_MAGIC, MAGIC_LEN);
	put32(hdr + 8, (uint32_t)klen);
	put32(hdr + 12, kaddr);
	put32(hdr + 16, (uint32_t)rlen);
	put32(hdr + 20, raddr);
	put32(hdr + 24, (uint32_t)slen);
	put32(hdr + 28, saddr);
	put32(hdr + 32, taddr);
	put32(hdr + 36, page);
	put32(hdr + 40, 0);          /* header_version: v0 only */
	put32(hdr + 44, 0);          /* os_version */
	memcpy(hdr + 48, board, strlen(board));
	memcpy(hdr + 64, cmdline, strlen(cmdline));

	unsigned char digest[20];
	boot_id(kbuf, klen, rbuf, rlen, sbuf, slen, NULL, 0, digest);
	/* The id field is 32 raw bytes. AOSP's mkbootimg famously writes 40 hex
	 * characters here and spills into extra_cmdline; Samsung's variant wrote
	 * the 20-byte digest and left the rest zero. Match the latter, so a
	 * repack of an unmodified image is byte-identical outside this field. */
	memcpy(hdr + 64 + CMDLINE_MAX, digest, 20);

	FILE *f = fopen(out, "wb");
	if (!f) die("cannot create %s: %s", out, strerror(errno));
	write_padded(f, hdr, sizeof(hdr), page);
	write_padded(f, kbuf, klen, page);
	write_padded(f, rbuf, rlen, page);
	if (sbuf) write_padded(f, sbuf, slen, page);
	if (fclose(f) != 0) die("cannot finish writing %s", out);

	printf("%s\n", out);
	printf("  page_size %u, kernel 0x%08x+%zu, ramdisk 0x%08x+%zu\n",
	       page, kaddr, klen, raddr, rlen);
	free(kbuf); free(rbuf); free(sbuf);
	return 0;
}


/* --- repack ------------------------------------------------------------- */

/* Append to a fixed-size cmdline field, refusing to truncate silently. */
static void cmdline_append(char *field, size_t size, const char *extra)
{
	size_t used = strnlen(field, size - 1);
	size_t need = strlen(extra) + (used ? 1 : 0);
	if (used + need >= size)
		die("cmdline would grow past %zu bytes; refusing to truncate", size - 1);
	if (used)
		field[used++] = ' ';
	memcpy(field + used, extra, strlen(extra) + 1);
}

static int cmd_repack(int argc, char **argv)
{
	const char *donor = NULL, *kernel = NULL, *dt_path = NULL, *out = NULL;
	const char *append = NULL, *replace = NULL;
	int force = 0;

	for (int i = 0; i < argc; i++) {
		const char *a = argv[i];
		if (!strcmp(a, "--kernel-new") && i + 1 < argc) kernel = argv[++i];
		else if (!strcmp(a, "--dt") && i + 1 < argc) dt_path = argv[++i];
		else if (!strcmp(a, "--out") && i + 1 < argc) out = argv[++i];
		else if (!strcmp(a, "--cmdline-append") && i + 1 < argc) append = argv[++i];
		else if (!strcmp(a, "--cmdline-replace") && i + 1 < argc) replace = argv[++i];
		else if (!strcmp(a, "--force")) force = 1;
		else if (a[0] == '-' && a[1] == '-') die("unknown argument: %s", a);
		else if (!donor) donor = a;
		else die("unexpected argument: %s", a);
	}
	if (!donor) die("usage: repack <donor.img> --kernel-new K --dt QCDT --out O");
	if (!kernel) die("--kernel-new is required");
	if (!out) die("--out is required");
	if (append && replace) die("--cmdline-append and --cmdline-replace are mutually exclusive");
	if (!force) {
		struct stat st;
		if (stat(out, &st) == 0)
			die("%s already exists (pass --force to replace it)", out);
	}

	size_t dlen, klen, new_dt_len = 0;
	unsigned char *dbuf = read_file(donor, &dlen);
	unsigned char *kbuf = read_file(kernel, &klen);
	unsigned char *new_dt = dt_path ? read_file(dt_path, &new_dt_len) : NULL;
	struct header h;
	parse_header(dbuf, &h);
	if (h.page_size != 2048 && h.page_size != 4096)
		die("donor page_size=%u is unexpected", h.page_size);
	if (klen == 0)
		die("new kernel payload is empty");
	if (klen > 0xffffffffUL)
		die("new kernel payload is absurdly large");
	if (new_dt_len > 0xffffffffUL)
		die("QCDT payload is larger than the boot header can describe");
	if (new_dt && new_dt_len != 0)
		(void)validate_qcdt(new_dt, new_dt_len, h.page_size);

	size_t r_off = h.page_size + round_up(h.kernel_size, h.page_size);
	size_t s_off = r_off + round_up(h.ramdisk_size, h.page_size);
	size_t old_dt_off = s_off + round_up(h.second_size, h.page_size);
	if (old_dt_off > dlen)
		die("donor is truncated before end of its second stage");
	if (h.dt_size && old_dt_off + h.dt_size > dlen)
		die("donor is truncated: dt_size declares %u bytes at offset %zu", h.dt_size, old_dt_off);
	if (h.dt_size)
		(void)validate_qcdt(dbuf + old_dt_off, h.dt_size, h.page_size);

	/* A supplied table replaces the donor's table; otherwise preserve it. */
	const unsigned char *dt_data = new_dt ? new_dt : (h.dt_size ? dbuf + old_dt_off : NULL);
	size_t dt_len = new_dt ? new_dt_len : h.dt_size;
	if (replace) {
		if (strlen(replace) >= CMDLINE_MAX)
			die("--cmdline-replace is longer than %d bytes", CMDLINE_MAX - 1);
		memset(h.cmdline, 0, CMDLINE_MAX);
		memcpy(h.cmdline, replace, strlen(replace));
	}
	if (append)
		cmdline_append(h.cmdline, CMDLINE_MAX, append);

	/* Keep the donor's ramdisk and second stage verbatim. */
	const unsigned char *ramdisk = dbuf + r_off;
	unsigned char digest[20];
	boot_id(kbuf, klen, ramdisk, h.ramdisk_size,
		h.second_size ? dbuf + s_off : NULL, h.second_size,
		dt_data, dt_len, digest);

	unsigned char hdr[HEADER_LEN];
	memset(hdr, 0, sizeof(hdr));
	memcpy(hdr, ANDROID_MAGIC, MAGIC_LEN);
	put32(hdr + 8, (uint32_t)klen);
	put32(hdr + 12, h.kernel_addr);
	put32(hdr + 16, h.ramdisk_size);
	put32(hdr + 20, h.ramdisk_addr);
	put32(hdr + 24, h.second_size);
	put32(hdr + 28, h.second_addr);
	put32(hdr + 32, h.tags_addr);
	put32(hdr + 36, h.page_size);
	put32(hdr + 40, (uint32_t)dt_len);
	put32(hdr + 44, h.unused);
	memcpy(hdr + 48, h.name, NAME_MAX_BOOT);
	memcpy(hdr + 64, h.cmdline, CMDLINE_MAX);
	memcpy(hdr + 64 + CMDLINE_MAX, digest, 20);
	memcpy(hdr + 64 + CMDLINE_MAX + ID_LEN, h.extra_cmdline, CMDLINE_MAX);

	/* Write to a temporary name, validate, then move into place. */
	char *tmp = malloc(strlen(out) + 8);
	if (!tmp)
		die("out of memory");
	sprintf(tmp, "%s.tmp", out);
	FILE *f = fopen(tmp, "wb");
	if (!f)
		die("cannot create %s: %s", tmp, strerror(errno));
	write_padded(f, hdr, sizeof(hdr), h.page_size);
	write_padded(f, kbuf, klen, h.page_size);
	write_padded(f, ramdisk, h.ramdisk_size, h.page_size);
	if (h.second_size)
		write_padded(f, dbuf + s_off, h.second_size, h.page_size);
	if (dt_data)
		write_padded(f, dt_data, dt_len, h.page_size);
	if (fclose(f) != 0)
		die("cannot finish writing %s", tmp);

	/* --- validate the artifact by reading it back --------------------- */
	size_t vlen;
	unsigned char *vbuf = read_file(tmp, &vlen);
	struct header v;
	parse_header(vbuf, &v);
	size_t v_k_off = v.page_size;
	size_t v_r_off = v_k_off + round_up(v.kernel_size, v.page_size);
	size_t v_s_off = v_r_off + round_up(v.ramdisk_size, v.page_size);
	size_t v_dt_off = v_s_off + round_up(v.second_size, v.page_size);
	int ok = 1;
	if (v.kernel_size != (uint32_t)klen) { fprintf(stderr, "verify: kernel size mismatch\n"); ok = 0; }
	if (v.ramdisk_size != h.ramdisk_size) { fprintf(stderr, "verify: ramdisk size mismatch\n"); ok = 0; }
	if (v.page_size != h.page_size) { fprintf(stderr, "verify: page size mismatch\n"); ok = 0; }
	if (v.dt_size != dt_len) { fprintf(stderr, "verify: dt_size mismatch\n"); ok = 0; }
	if (v_dt_off + v.dt_size > vlen) { fprintf(stderr, "verify: image shorter than declared\n"); ok = 0; }
	if (memcmp(vbuf + v_k_off, kbuf, klen) != 0) {
		fprintf(stderr, "verify: kernel payload does not read back\n"); ok = 0;
	}
	if (memcmp(vbuf + v_r_off, ramdisk, h.ramdisk_size) != 0) {
		fprintf(stderr, "verify: ramdisk payload does not read back\n"); ok = 0;
	}
	if (dt_len && memcmp(vbuf + v_dt_off, dt_data, dt_len) != 0) {
		fprintf(stderr, "verify: Qualcomm DT payload does not read back\n"); ok = 0;
	}
	if (ok && v.dt_size)
		(void)validate_qcdt(vbuf + v_dt_off, v.dt_size, v.page_size);
	free(vbuf);
	if (!ok) {
		remove(tmp);
		die("refusing to keep a bad image");
	}
	if (rename(tmp, out) != 0) {
		int e = errno;
		remove(tmp);
		die("cannot rename %s -> %s: %s", tmp, out, strerror(e));
	}
	free(tmp);
	free(dbuf);
	free(kbuf);
	free(new_dt);

	printf("%s\n", out);
	printf("  donor        %s\n", donor);
	printf("  page_size    %u\n", h.page_size);
	printf("  kernel       %zu bytes -> 0x%08x (was %u bytes)\n",
	       klen, h.kernel_addr, h.kernel_size);
	printf("  ramdisk      %u bytes kept from donor\n", h.ramdisk_size);
	size_t new_dt_off = h.page_size + round_up((uint32_t)klen, h.page_size) +
			    round_up(h.ramdisk_size, h.page_size) +
			    round_up(h.second_size, h.page_size);
	printf("  Qualcomm DT  %zu bytes at boot-image offset %zu\n", dt_len, new_dt_off);
	printf("  tags_addr    0x%08x\n", h.tags_addr);
	printf("  cmdline      \"%s\"\n", h.cmdline);
	printf("  verified     yes\n");
	return 0;
}

static int usage(void)
{
	fprintf(stderr,
		"usage:\n"
		"  %s inspect <boot.img>\n"
		"  %s build --kernel K --ramdisk R --out O [options]\n"
		"  %s repack <donor.img> --kernel-new K --dt QCDT --out O\n"
		"         [--cmdline-append S] [--cmdline-replace S] [--force]\n"
		"options: --page-size 2048|4096 --kernel-addr A --ramdisk-addr A\n"
		"         --tags-addr A --second-addr A --second S --no-second\n"
		"         --cmdline S --board S --force\n",
		prog, prog, prog);
	return 2;
}

int main(int argc, char **argv)
{
	if (argc < 2) return usage();
	if (!strcmp(argv[1], "inspect")) return cmd_inspect(argc - 2, argv + 2);
	if (!strcmp(argv[1], "build")) return cmd_build(argc - 2, argv + 2);
	if (!strcmp(argv[1], "repack")) return cmd_repack(argc - 2, argv + 2);
	return usage();
}
