// SPDX-License-Identifier: BSD-2-Clause-Patent
// Disposable guest workload. Never stage this stress test in the production addon.
#define main diagnostics_main
#include "DesktopDiagnostics.c"
#undef main
#include <sys/mman.h>

static int report_fd;
static uint64_t report_seq;
static char report_session[37];
static void report(const char *phase) {
    size_t n = 0;
    char *text = sample(++report_seq, report_session, 0, 0, 0, 0, true, &n);
    if (!text) exit(10);
    char *payload;
    int len = asprintf(&payload, "%.*s,\"phase\":\"%s\"}", (int)n - 1, text, phase);
    free(text);
    if (len < 0) exit(10);
    char *record;
    int bytes = asprintf(&record, "{\"crc32\":\"%08x\",\"sample\":%s}\n", crc32(payload, (size_t)len), payload);
    free(payload);
    if (bytes < 0 || write_all(report_fd, record, (size_t)bytes) || fsync(report_fd) || fcntl(report_fd, F_FULLFSYNC)) {
        perror("Persist workload phase"); exit(11);
    }
    free(record);
    printf("Memory test - %s - committed\n", phase); fflush(stdout);
}
static uint32_t step(uint32_t x) { x ^= x << 13; x ^= x >> 17; return x ^ (x << 5); }
static void cache_read(int fd, size_t bytes, uint64_t expected) {
    unsigned char buffer[65536]; uint64_t sum = 0;
    for (size_t at = 0; at < bytes; at += sizeof buffer) {
        if (pread(fd, buffer, sizeof buffer, (off_t)at) != sizeof buffer) { perror("Cache read"); exit(12); }
        for (size_t i = 0; i < sizeof buffer; ++i) sum += buffer[i];
    }
    if (sum != expected) { fprintf(stderr, "Cache data mismatch\n"); exit(13); }
}
int main(int argc, char **argv) {
    if (argc < 2 || argc > 3) { fprintf(stderr, "Usage: QemuReclaim DIRECTORY [WORKLOAD_MIB]\n"); return 2; }
    unsigned mib = argc == 3 ? number(argv[2]) : 832;
    if (mib < 64 || mib > 4096) return 2;
    char log[1024], file[1024];
    if (strlen(argv[1]) > 850) return 2;
    snprintf(log, sizeof log, "%s/reclaim.log", argv[1]);
    snprintf(file, sizeof file, "%s/cache-test.bin", argv[1]);
    report_fd = open_log(log);
    if (report_fd < 0 || sync_parent(log)) { perror("Report"); return 3; }
    uuid_t id; uuid_generate(id); uuid_unparse_lower(id, report_session);
    const size_t size = (size_t)mib * 1024 * 1024, page = 4096, cache_bytes = 192ULL * 1024 * 1024;
    report("baseline");
    int cache = open(file, O_CREAT | O_EXCL | O_RDWR | O_NOFOLLOW, 0600);
    if (cache < 0) { perror("Create cache fixture"); return 4; }
    unsigned char buffer[65536]; uint32_t x = 0x9384eda1; uint64_t sum = 0;
    for (size_t i = 0; i < sizeof buffer; ++i) { x = step(x); buffer[i] = (unsigned char)x; sum += buffer[i]; }
    for (size_t at = 0; at < cache_bytes; at += sizeof buffer)
        if (write_all(cache, (char *)buffer, sizeof buffer)) { perror("Cache write"); return 5; }
    if (fsync(cache) || fcntl(cache, F_FULLFSYNC)) return 6;
    sum *= cache_bytes / sizeof buffer;
    report("cache_written");
    cache_read(cache, cache_bytes, sum); report("cache_read_first");
    cache_read(cache, cache_bytes, sum); report("cache_read_warm");
    uint8_t seed[4096]; x = 0x9473ae21;
    for (size_t i = 0; i < sizeof seed; ++i) { x = step(x); seed[i] = (uint8_t)x; }
    uint8_t *p = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 7; }
    for (size_t off = 0; off < size; off += page) {
        uint64_t tag = off ^ UINT64_C(0x93a21def98765432);
        // The first 64 MiB is compressible; the remaining pages force backing-store pressure.
        if (off >= 64 * 1024 * 1024) memcpy(p + off, seed, page);
        else memset(p + off, 'A', page);
        memcpy(p + off, &tag, 8); memcpy(p + off + page - 8, &tag, 8);
        if (!((off + page) % (64 * 1024 * 1024))) {
            char phase[64]; snprintf(phase, sizeof phase, "allocated_%zu_MiB", (off + page) / (1024 * 1024));
            report(phase);
        }
    }
    report("pressure_hold"); sleep(20);
    for (size_t off = 0; off < size; off += page) {
        uint64_t a, b, tag = off ^ UINT64_C(0x93a21def98765432);
        memcpy(&a, p + off, 8); memcpy(&b, p + off + page - 8, 8);
        if (a != tag || b != tag) return 8;
        if (off >= 64 * 1024 * 1024) { if (memcmp(p + off + 8, seed + 8, page - 16)) return 9; }
        else for (size_t i = 8; i < page - 8; ++i) if (p[off + i] != 'A') return 9;
    }
    report("memory_verified");
    if (munmap(p, size)) return 10;
    sleep(15); report("released");
    cache_read(cache, cache_bytes, sum); report("cache_read_after_pressure");
    close(cache);
    if (unlink(file) || sync_parent(file)) return 11;
    report("PASS");
    close(report_fd);
    return 0;
}
