// SPDX-License-Identifier: BSD-2-Clause-Patent
// A contiguous extent larger than the original store must cross its old end.
#include <sys/fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    const off_t length = (off_t)5 * 1024 * 1024 * 1024;
    int verify = !strcmp(argv[1], "verify");
    if (!verify && strcmp(argv[1], "create")) return 2;
    int fd = open(argv[2], verify ? O_RDONLY : O_RDWR | O_CREAT | O_EXCL, 0600);
    if (fd < 0) { perror("open"); return 1; }
    if (!verify) {
        fstore_t store = {.fst_flags = F_ALLOCATECONTIG, .fst_posmode = F_PEOFPOSMODE, .fst_length = length};
        if (fcntl(fd, F_PREALLOCATE, &store) || store.fst_bytesalloc < length || ftruncate(fd, length)) {
            perror("contiguous APFS allocation"); close(fd); return 1;
        }
        for (off_t offset = 0; offset < length; offset += length - 4096) {
            uint64_t tag[512]; for (unsigned j = 0; j < 512; j++) tag[j] = (uint64_t)offset ^ (UINT64_C(0xabc12345678) + j);
            if (pwrite(fd, tag, sizeof(tag), offset) != sizeof(tag)) { perror("write"); return 1; }
        }
        if (fsync(fd)) { perror("fsync"); return 1; }
    }
    for (off_t offset = 0; offset < length; offset += length - 4096) {
        uint64_t tag[512];
        if (pread(fd, tag, sizeof(tag), offset) != sizeof(tag)) return 1;
        for (unsigned j = 0; j < 512; j++) if (tag[j] != ((uint64_t)offset ^ (UINT64_C(0xabc12345678) + j))) return 1;
    }
    close(fd);
    printf("APFS grow - %s - contiguous 5 GiB file endpoints verified\n", argv[1]);
    return 0;
}
