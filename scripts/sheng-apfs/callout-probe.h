/* SPDX-License-Identifier: BSD-2-Clause-Patent */
/* Included once by thread_call.c. Observers never wait or allocate. */
#ifndef BOOT_CALLOUT_HOST
#include <pexpert/pexpert.h>
#include <machine/machine_routines.h>
#include <kern/misc_protos.h>
extern int snprintf(char *, size_t, const char *, ...) __attribute__((format(printf, 3, 4)));
#endif

#define BOOT_CALLOUT_ACTIVE 64
#define BOOT_CALLOUT_TOTALS 128
struct boot_callout_active {
    uint64_t epoch, function, owner, tid, wall, cpu;
    unsigned kind;
};
struct boot_callout_total {
    uint64_t key, count, cpu, wall_max;
};
static struct boot_callout_active boot_callout_active[BOOT_CALLOUT_ACTIVE];
static struct boot_callout_total boot_callout_totals[BOOT_CALLOUT_TOTALS];
static uint64_t boot_callout_serial, boot_callout_dropped;
static unsigned boot_callout_enabled;
extern uint64_t recount_thread_time_mach(thread_t);
uint64_t boot_callout_begin(uint64_t function, uint64_t owner, unsigned kind);
void boot_callout_end(uint64_t token);
unsigned boot_callout_rows(char rows[][120], unsigned cap);

#define BC_LOAD(value) __atomic_load_n(&(value), __ATOMIC_ACQUIRE)
#define BC_STORE(value, data) __atomic_store_n(&(value), (data), __ATOMIC_RELEASE)

uint64_t
boot_callout_begin(uint64_t function, uint64_t owner, unsigned kind)
{
    if (!ml_get_interrupts_enabled()) return 0;
    if (!BC_LOAD(boot_callout_enabled)) {
        unsigned enabled = 0;
        PE_parse_boot_argn("boot-callout-trace", &enabled, sizeof enabled);
        BC_STORE(boot_callout_enabled, enabled == 1 ? 2 : 1);
    }
    if (BC_LOAD(boot_callout_enabled) != 2) return 0;
    for (unsigned i = 0; i < BOOT_CALLOUT_ACTIVE; ++i) {
        struct boot_callout_active *a = &boot_callout_active[i];
        uint64_t free = 0;
        if (!__atomic_compare_exchange_n(&a->epoch, &free, 1, false, __ATOMIC_ACQUIRE, __ATOMIC_RELAXED)) continue;
        uint64_t token = (__atomic_add_fetch(&boot_callout_serial, 1, __ATOMIC_RELAXED) << 8) | (i + 1);
        BC_STORE(a->function, function); BC_STORE(a->owner, owner); BC_STORE(a->kind, kind);
        BC_STORE(a->tid, current_thread()->thread_id);
        BC_STORE(a->wall, mach_absolute_time());
        BC_STORE(a->cpu, recount_thread_time_mach(current_thread()));
        BC_STORE(a->epoch, token);
        return token;
    }
    __atomic_fetch_add(&boot_callout_dropped, 1, __ATOMIC_RELAXED);
    return 0;
}

void
boot_callout_end(uint64_t token)
{
    unsigned index = (unsigned)(token & 255);
    if (!token || !index || index > BOOT_CALLOUT_ACTIVE) return;
    struct boot_callout_active *a = &boot_callout_active[index - 1];
    if (BC_LOAD(a->epoch) != token) return;
    uint64_t cpu = 0, wall = 0;
    absolutetime_to_nanoseconds(recount_thread_time_mach(current_thread()) - BC_LOAD(a->cpu), &cpu);
    absolutetime_to_nanoseconds(mach_absolute_time() - BC_LOAD(a->wall), &wall);
    uint64_t key = (BC_LOAD(a->function) & ~3ull) | (BC_LOAD(a->kind) & 1);
    BC_STORE(a->epoch, 0);
    for (unsigned i = 0; i < BOOT_CALLOUT_TOTALS; ++i) {
        struct boot_callout_total *t = &boot_callout_totals[((key >> 2) + i) % BOOT_CALLOUT_TOTALS];
        uint64_t free = 0;
        if (BC_LOAD(t->key) != key &&
            !__atomic_compare_exchange_n(&t->key, &free, key, false, __ATOMIC_ACQ_REL, __ATOMIC_RELAXED) && free != key) continue;
        __atomic_fetch_add(&t->count, 1, __ATOMIC_RELAXED);
        __atomic_fetch_add(&t->cpu, cpu, __ATOMIC_RELAXED);
        uint64_t old = BC_LOAD(t->wall_max);
        for (unsigned retry = 0; retry < 4 && wall > old; ++retry)
            if (__atomic_compare_exchange_n(&t->wall_max, &old, wall, false, __ATOMIC_RELEASE, __ATOMIC_RELAXED)) break;
        return;
    }
    __atomic_fetch_add(&boot_callout_dropped, 1, __ATOMIC_RELAXED);
}

unsigned
boot_callout_rows(char rows[][120], unsigned cap)
{
    if (!cap || BC_LOAD(boot_callout_enabled) != 2) return 0;
    unsigned n = 0, top[4] = {BOOT_CALLOUT_TOTALS, BOOT_CALLOUT_TOTALS, BOOT_CALLOUT_TOTALS, BOOT_CALLOUT_TOTALS};
    uint64_t used[4] = {0};
    for (unsigned i = 0; i < BOOT_CALLOUT_TOTALS; ++i) {
        uint64_t cpu = BC_LOAD(boot_callout_totals[i].cpu);
        for (unsigned k = 0; k < 4; ++k) if (cpu > used[k]) {
            for (unsigned j = 3; j > k; --j) {top[j] = top[j-1]; used[j] = used[j-1];}
            top[k] = i; used[k] = cpu; break;
        }
    }
    snprintf(rows[n++], 120, "callouts: trace=1 dropped=%llu; CPU totals include nested timer actions",
             (unsigned long long)BC_LOAD(boot_callout_dropped));
    for (unsigned i = 0; i < 4 && n < cap && top[i] != BOOT_CALLOUT_TOTALS; ++i) {
        struct boot_callout_total *t = &boot_callout_totals[top[i]];
        uint64_t key = BC_LOAD(t->key);
        snprintf(rows[n++], 120, "CALL kind=%u fn=%llx calls=%llu cpu=%llums max-wall=%llums", (unsigned)(key & 1),
                 (unsigned long long)(key & ~3ull), (unsigned long long)BC_LOAD(t->count),
                 (unsigned long long)(used[i]/1000000), (unsigned long long)(BC_LOAD(t->wall_max)/1000000));
    }
    uint64_t now = mach_absolute_time();
    for (unsigned i = 0; i < BOOT_CALLOUT_ACTIVE && n < cap; ++i) {
        struct boot_callout_active *a = &boot_callout_active[i];
        uint64_t epoch = BC_LOAD(a->epoch);
        if (epoch <= 1) continue;
        uint64_t began = BC_LOAD(a->wall), function = BC_LOAD(a->function), owner = BC_LOAD(a->owner), tid = BC_LOAD(a->tid);
        unsigned kind = BC_LOAD(a->kind);
        if (BC_LOAD(a->epoch) != epoch || now < began) continue;
        uint64_t age = 0; absolutetime_to_nanoseconds(now - began, &age);
        if (age < 100000000) continue;
        snprintf(rows[n++], 120, "CALL ACTIVE kind=%u fn=%llx owner=%llx tid=%llx wall=%llums", kind,
                 (unsigned long long)function, (unsigned long long)owner, (unsigned long long)tid,
                 (unsigned long long)(age/1000000));
    }
    return n;
}
#undef BC_LOAD
#undef BC_STORE
