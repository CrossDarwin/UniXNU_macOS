// SPDX-License-Identifier: BSD-2-Clause-Patent
// The native driver owns the writable bounds. DiskManagement owns APFS resizing.
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/IOBSD.h>
#include <IOKit/storage/IOMedia.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/disk.h>
#include <fcntl.h>
#include <poll.h>
#include <spawn.h>
#include <unistd.h>
#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <uuid/uuid.h>

extern char **environ;
#define GIB (UINT64_C(1) << 30)
#define STATE_DIR "/var/db/unixnu"
static const char *state_dir = STATE_DIR;
static uint64_t initial_bytes, target_bytes, registry_id;
static char container_uuid[37], system_uuid[37], data_uuid[37], vm_uuid[37];
#ifdef APFS_GROW_FIXTURE
static char fixture_bsd[64];
static char fixture_resize_program[1024];
#endif

static CFTypeRef field(CFTypeRef object, CFStringRef key, CFTypeID type)
{
    if (!object || CFGetTypeID(object) != CFDictionaryGetTypeID()) return NULL;
    CFTypeRef value = CFDictionaryGetValue(object, key);
    return value && CFGetTypeID(value) == type ? value : NULL;
}
static bool number(CFTypeRef d, CFStringRef key, uint64_t *out)
{
    CFNumberRef v = field(d, key, CFNumberGetTypeID());
    int64_t n;
    if (!v || !CFNumberGetValue(v, kCFNumberSInt64Type, &n) || n < 0) return false;
    *out = (uint64_t)n;
    return true;
}
static bool string(CFTypeRef d, CFStringRef key, char *out, size_t size)
{
    CFStringRef v = field(d, key, CFStringGetTypeID());
    return v && CFStringGetCString(v, out, size, kCFStringEncodingUTF8);
}
static bool uuid_string(CFTypeRef d, CFStringRef key, char out[37])
{
    char value[37]; uuid_t parsed;
    if (!string(d, key, value, sizeof(value)) || uuid_parse(value, parsed)) return false;
    uuid_unparse_upper(parsed, out);
    return true;
}
static bool is_disk(const char *name)
{
    if (strncmp(name, "disk", 4) || !name[4]) return false;
    for (const char *p = name + 4; *p; p++) if ((*p < '0' || *p > '9') && *p != 's') return false;
    return true;
}
static CFPropertyListRef parse(const void *bytes, size_t length)
{
    CFDataRef data = CFDataCreate(NULL, bytes, (CFIndex)length);
    if (!data) return NULL;
    CFPropertyListRef value = CFPropertyListCreateWithData(NULL, data, kCFPropertyListImmutable, NULL, NULL);
    CFRelease(data);
    return value;
}
#ifdef APFS_GROW_FIXTURE
static CFPropertyListRef read_plist(const char *path)
{
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) return NULL;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size < 0 || st.st_size > 16 * 1024 * 1024) {
        close(fd); return NULL;
    }
    size_t length = (size_t)st.st_size, at = 0;
    char *bytes = malloc(length ? length : 1);
    if (!bytes) { close(fd); return NULL; }
    while (at < length) {
        ssize_t n = read(fd, bytes + at, length - at);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        at += (size_t)n;
    }
    close(fd);
    CFPropertyListRef value = at == length ? parse(bytes, length) : NULL;
    free(bytes);
    return value;
}
#endif

// One child at a time; a quiet DiskManagement transaction stays alive.
static int command(char *const argv[], CFPropertyListRef *plist)
{
    int pipefd[2];
    if (pipe(pipefd)) return -1;
    fcntl(pipefd[0], F_SETFD, FD_CLOEXEC);
    fcntl(pipefd[1], F_SETFD, FD_CLOEXEC);
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions, pipefd[1], STDOUT_FILENO);
    posix_spawn_file_actions_addclose(&actions, pipefd[0]);
    posix_spawn_file_actions_addclose(&actions, pipefd[1]);
    pid_t child;
    int error = posix_spawn(&child, argv[0], &actions, NULL, argv, environ);
    posix_spawn_file_actions_destroy(&actions);
    close(pipefd[1]);
    if (error) { close(pipefd[0]); errno = error; return -1; }
    CFMutableDataRef data = plist ? CFDataCreateMutable(NULL, 0) : NULL;
    if (plist) *plist = NULL;
    bool valid = !plist || data;
    time_t heartbeat = time(NULL);
    for (;;) {
        struct pollfd pfd = {pipefd[0], POLLIN, 0};
        int ready = poll(&pfd, 1, 1000);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) { valid = false; break; }
        if (ready) {
            char buf[4096]; ssize_t n = read(pipefd[0], buf, sizeof(buf));
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) { if (n < 0) valid = false; break; }
            if (plist) {
                if (data && CFDataGetLength(data) + n <= 16 * 1024 * 1024)
                    CFDataAppendBytes(data, (const UInt8 *)buf, n);
                else valid = false;
            } else fwrite(buf, 1, (size_t)n, stdout);
        }
        if (time(NULL) - heartbeat >= 10) {
            printf("APFS grow - DiskManagement running - pid %d\n", child);
            heartbeat = time(NULL);
        }
    }
    close(pipefd[0]);
    int status = 0;
    while (waitpid(child, &status, 0) < 0) if (errno != EINTR) { valid = false; break; }
    if (valid && WIFEXITED(status) && WEXITSTATUS(status) == 0 && plist && data)
        *plist = parse(CFDataGetBytePtr(data), (size_t)CFDataGetLength(data));
    if (data) CFRelease(data);
    return valid && WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}
static void record(const char *state, uint64_t actual)
{
    char path[1024], temporary[1024];
    snprintf(path, sizeof(path), "%s/apfs-grow.json", state_dir);
    snprintf(temporary, sizeof(temporary), "%s/.apfs-grow.%d", state_dir, getpid());
    int fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) { perror("APFS grow - state file"); return; }
    dprintf(fd, "{\"state\":\"%s\",\"container_uuid\":\"%s\",\"initial_bytes\":%" PRIu64
            ",\"target_bytes\":%" PRIu64 ",\"actual_bytes\":%" PRIu64 "}\n",
            state, container_uuid, initial_bytes, target_bytes, actual);
    int ok = fsync(fd);
    if (close(fd)) ok = -1;
    if (ok || rename(temporary, path)) { perror("APFS grow - save state"); unlink(temporary); }
}
static bool identities(CFTypeRef properties)
{
    return number(properties, CFSTR("UniXNUAPFSInitialBytes"), &initial_bytes) &&
        number(properties, CFSTR("UniXNUAPFSTargetBytes"), &target_bytes) &&
        initial_bytes >= 4 * GIB && !(initial_bytes % 4096) &&
        target_bytes >= initial_bytes && !(target_bytes % 4096) &&
        uuid_string(properties, CFSTR("UniXNUAPFSContainerUUID"), container_uuid) &&
        uuid_string(properties, CFSTR("UniXNUAPFSSystemUUID"), system_uuid) &&
        uuid_string(properties, CFSTR("UniXNUAPFSDataUUID"), data_uuid) &&
        uuid_string(properties, CFSTR("UniXNUAPFSVMUUID"), vm_uuid) &&
        strcmp(system_uuid, data_uuid) && strcmp(data_uuid, vm_uuid) && strcmp(system_uuid, vm_uuid);
}
static int discover(void)
{
    io_iterator_t iterator = 0;
    if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOBlockStorageDevice"), &iterator)) return 0;
    unsigned found = 0; bool valid = false;
    io_service_t service;
    while ((service = IOIteratorNext(iterator))) {
        CFTypeRef enabled = IORegistryEntryCreateCFProperty(service, CFSTR("UniXNUAPFSGrow"), NULL, 0);
        if (enabled == kCFBooleanTrue) {
            found++;
            CFMutableDictionaryRef properties = NULL;
            valid = IORegistryEntryCreateCFProperties(service, &properties, NULL, 0) == KERN_SUCCESS &&
                identities(properties) && IORegistryEntryGetRegistryEntryID(service, &registry_id) == KERN_SUCCESS;
            if (properties) CFRelease(properties);
        }
        if (enabled) CFRelease(enabled);
        IOObjectRelease(service);
    }
    IOObjectRelease(iterator);
    return found == 1 && valid ? 1 : found ? -1 : 0;
}
static bool bound_store(const char *bsd)
{
    if (!is_disk(bsd)) return false;
    io_service_t media = IOServiceGetMatchingService(kIOMainPortDefault, IOBSDNameMatching(kIOMainPortDefault, 0, bsd));
    if (!media) return false;
    CFTypeRef size = IORegistryEntryCreateCFProperty(media, CFSTR(kIOMediaSizeKey), NULL, 0);
    CFTypeRef writable = IORegistryEntryCreateCFProperty(media, CFSTR(kIOMediaWritableKey), NULL, 0);
    int64_t bytes = 0;
    bool good = writable == kCFBooleanTrue && size && CFGetTypeID(size) == CFNumberGetTypeID() &&
        CFNumberGetValue(size, kCFNumberSInt64Type, &bytes) && bytes > 0 && (uint64_t)bytes == target_bytes;
    if (size) CFRelease(size);
    if (writable) CFRelease(writable);
#ifdef APFS_GROW_FIXTURE
    if (fixture_bsd[0]) { IOObjectRelease(media); return good && !strcmp(bsd, fixture_bsd); }
#endif
    bool owner = false;
    for (unsigned depth = 0; depth < 64 && media; depth++) {
        uint64_t identity = 0;
        if (IORegistryEntryGetRegistryEntryID(media, &identity) == KERN_SUCCESS && identity == registry_id) owner = true;
        io_registry_entry_t parent = 0;
        IORegistryEntryGetParentEntry(media, kIOServicePlane, &parent);
        IOObjectRelease(media); media = parent;
    }
    if (media) IOObjectRelease(media);
    return good && owner;
}
static int inspect(char reference[64], uint64_t *actual)
{
    CFPropertyListRef list = NULL;
    char *const argv[] = {"/usr/sbin/diskutil", "apfs", "list", "-plist", NULL};
    if (command(argv, &list) || !list) return 0;
    CFArrayRef containers = field(list, CFSTR("Containers"), CFArrayGetTypeID());
    unsigned matches = 0; bool valid = false;
    for (CFIndex i = 0; containers && i < CFArrayGetCount(containers); i++) {
        CFTypeRef c = CFArrayGetValueAtIndex(containers, i);
        char uuid[37];
        if (!uuid_string(c, CFSTR("APFSContainerUUID"), uuid) || strcmp(uuid, container_uuid)) continue;
        matches++;
        CFArrayRef stores = field(c, CFSTR("PhysicalStores"), CFArrayGetTypeID());
        CFArrayRef volumes = field(c, CFSTR("Volumes"), CFArrayGetTypeID());
        char bsd[64];
        valid = stores && CFArrayGetCount(stores) == 1 &&
            string(CFArrayGetValueAtIndex(stores, 0), CFSTR("DeviceIdentifier"), bsd, sizeof(bsd)) && bound_store(bsd) &&
            string(c, CFSTR("ContainerReference"), reference, 64) && is_disk(reference) &&
            number(c, CFSTR("CapacityCeiling"), actual) && *actual >= initial_bytes && *actual <= target_bytes;
        unsigned mask = 0;
        for (CFIndex j = 0; valid && volumes && j < CFArrayGetCount(volumes); j++) {
            CFTypeRef v = CFArrayGetValueAtIndex(volumes, j);
            CFArrayRef roles = field(v, CFSTR("Roles"), CFArrayGetTypeID());
            char id[37];
            if (!uuid_string(v, CFSTR("APFSVolumeUUID"), id)) { valid = false; break; }
            const char *ids[] = {system_uuid, data_uuid, vm_uuid};
            const CFStringRef role[] = {CFSTR("System"), CFSTR("Data"), CFSTR("VM")};
            for (unsigned k = 0; k < 3; k++) if (!strcmp(id, ids[k])) {
                if ((mask & (1U << k)) || !roles || CFArrayGetCount(roles) != 1 ||
                    !CFEqual(CFArrayGetValueAtIndex(roles, 0), role[k])) valid = false;
                mask |= 1U << k;
            }
        }
        valid = valid && mask == 7;
    }
    CFRelease(list);
    return matches == 1 && valid ? 1 : matches ? -1 : 0;
}
int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
#ifndef APFS_GROW_FIXTURE
    if (geteuid() != 0) { fprintf(stderr, "APFS grow - root required\n"); return 1; }
#endif
#ifdef APFS_GROW_FIXTURE
    if (argc != 3) return 2;
    CFPropertyListRef fixture = read_plist(argv[1]);
    bool ready = fixture && identities(fixture) && string(fixture, CFSTR("FixtureBSD"), fixture_bsd, sizeof(fixture_bsd)) && is_disk(fixture_bsd);
    if (fixture) string(fixture, CFSTR("FixtureResizeProgram"), fixture_resize_program, sizeof(fixture_resize_program));
    if (fixture) CFRelease(fixture);
    if (!ready) return 2;
    state_dir = argv[2];
#else
    (void)argv;
    if (argc != 1) return 2;
#endif
    if (mkdir(state_dir, 0700) && errno != EEXIST) { perror("APFS grow - state directory"); return 1; }
    struct stat st;
    if (lstat(state_dir, &st) || !S_ISDIR(st.st_mode) || st.st_uid != geteuid() || (st.st_mode & 0022)) return 1;
    char path[1024];
    snprintf(path, sizeof(path), "%s/apfs-grow.lock", state_dir);
    int lock = open(path, O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (lock < 0) { perror("APFS grow - lock"); return 1; }
    if (flock(lock, LOCK_EX | LOCK_NB)) { puts("APFS grow - another check owns the lock"); close(lock); return 0; }
    int found = 0;
    char reference[64] = {0}; uint64_t actual = 0;
    puts("APFS grow - checking native container and DiskManagement");
    for (unsigned attempt = 0; attempt < 60; attempt++) {
#ifdef APFS_GROW_FIXTURE
        int device = fixture_bsd[0] ? 1 : discover();
#else
        int device = discover();
#endif
        found = device == 1 ? inspect(reference, &actual) : device;
        if (found) break;
        if (!(attempt % 5)) printf("APFS grow - waiting for disk services - elapsed %us\n", attempt * 2);
        sleep(2);
    }
    if (found != 1) {
        puts("APFS grow - unavailable or mismatched container - no resize attempted");
        record("failed", actual); close(lock); return 1;
    }
    printf("APFS grow - initial %" PRIu64 " - current %" PRIu64 " - target %" PRIu64 " bytes - %s\n",
           initial_bytes, actual, target_bytes, reference);
    if (actual == target_bytes) {
        puts("APFS grow - already complete"); record("complete", actual); close(lock); return 0;
    }
    record("running", actual);
    char *resize[] = {"/usr/sbin/diskutil", "apfs", "resizeContainer", reference, "0", NULL};
#ifdef APFS_GROW_FIXTURE
    if (fixture_resize_program[0]) resize[0] = fixture_resize_program;
#endif
    int result = command(resize, NULL);
    char after[64] = {0};
    found = inspect(after, &actual);
    bool complete = found == 1 && actual == target_bytes;
    printf("APFS grow - %s - diskutil status %d - actual %" PRIu64 " bytes\n", complete ? "complete" : "failed", result, actual);
    record(complete ? "complete" : "failed", actual);
    close(lock);
    return complete ? 0 : 1;
}
