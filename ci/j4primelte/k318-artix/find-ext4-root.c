#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define EXT4_SUPER_OFFSET 1024
#define EXT4_SUPER_SIZE 1024
#define EXT4_MAGIC_OFFSET 0x38
#define EXT4_LABEL_OFFSET 0x78
#define EXT4_LABEL_SIZE 16

static int has_ext4_label(const char *path, const char *wanted)
{
    unsigned char super[EXT4_SUPER_SIZE];
    char label[EXT4_LABEL_SIZE + 1];
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
    if (fd < 0)
        return 0;

    ssize_t count = pread(fd, super, sizeof(super), EXT4_SUPER_OFFSET);
    close(fd);
    if (count != (ssize_t)sizeof(super))
        return 0;

    unsigned int magic = (unsigned int)super[EXT4_MAGIC_OFFSET] |
                         ((unsigned int)super[EXT4_MAGIC_OFFSET + 1] << 8);
    if (magic != 0xef53)
        return 0;

    memcpy(label, super + EXT4_LABEL_OFFSET, EXT4_LABEL_SIZE);
    label[EXT4_LABEL_SIZE] = '\0';
    for (int i = EXT4_LABEL_SIZE - 1; i >= 0; --i) {
        if (label[i] != '\0' && label[i] != ' ')
            break;
        label[i] = '\0';
    }
    return strcmp(label, wanted) == 0;
}

int main(int argc, char **argv)
{
    if (argc != 2 || argv[1][0] == '\0' || strlen(argv[1]) > EXT4_LABEL_SIZE) {
        fprintf(stderr, "usage: %s <ext4-label-up-to-16-bytes>\n", argv[0]);
        return 2;
    }

    DIR *directory = opendir("/dev");
    if (directory == NULL) {
        perror("opendir /dev");
        return 1;
    }

    struct dirent *entry;
    while ((entry = readdir(directory)) != NULL) {
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0)
            continue;

        char path[512];
        int length = snprintf(path, sizeof(path), "/dev/%s", entry->d_name);
        if (length < 0 || (size_t)length >= sizeof(path))
            continue;

        struct stat info;
        if (stat(path, &info) != 0 || !S_ISBLK(info.st_mode))
            continue;
        if (has_ext4_label(path, argv[1])) {
            puts(path);
            closedir(directory);
            return 0;
        }
    }

    closedir(directory);
    return 1;
}
