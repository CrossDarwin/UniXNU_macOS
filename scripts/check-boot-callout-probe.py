#!/usr/bin/env python3
"""Exercise callback accounting, active records and saturation without XNU."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
header = root / 'scripts/sheng-apfs/callout-probe.h'
harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <pthread.h>
struct thread { uint64_t thread_id; };
typedef struct thread *thread_t;
static _Thread_local struct thread self = { 1 };
static _Thread_local uint64_t wall, cpu;
static unsigned requested = 1;
static bool interrupts = true;
static thread_t current_thread(void) { return &self; }
static uint64_t mach_absolute_time(void) { return wall; }
static uint64_t recount_thread_time_mach(thread_t t) { (void)t; return cpu; }
static void absolutetime_to_nanoseconds(uint64_t ticks, uint64_t *ns) { *ns = ticks * 2; }
static bool ml_get_interrupts_enabled(void) { return interrupts; }
static int PE_parse_boot_argn(const char *key, void *value, int size) {
    assert(!strcmp(key, "boot-callout-trace") && size == sizeof(unsigned));
    *(unsigned *)value = requested; return 1;
}
#define BOOT_CALLOUT_HOST 1
#include "callout-probe.h"
static void *worker(void *arg) {
    self.thread_id = (uintptr_t)arg + 10;
    for (unsigned i = 0; i < 1000; ++i) {
        uint64_t token = boot_callout_begin(0x1000, 0x9000, 0);
        assert(token); cpu += 500; wall += 1000; boot_callout_end(token);
    }
    return NULL;
}
int main(void) {
    requested = 0; assert(!boot_callout_begin(0x1000, 0, 0));
    boot_callout_enabled = 0; requested = 1;
    interrupts = false; assert(!boot_callout_begin(0x1000, 0, 0)); interrupts = true;
    uint64_t a = boot_callout_begin(0x1000, 0x9000, 0);
    uint64_t b = boot_callout_begin(0x2000, 0x8000, 1);
    wall += 60000000; cpu += 1000000;
    char rows[80][120]; unsigned count = boot_callout_rows(rows, 80);
    assert(count == 3 && strstr(rows[1], "CALL ACTIVE") && strstr(rows[2], "kind=1"));
    boot_callout_end(b); boot_callout_end(a); boot_callout_end(a);
    count = boot_callout_rows(rows, 80);
    assert(count == 3 && strstr(rows[1], "calls=1 cpu=2ms max-wall=120ms"));
    pthread_t threads[8];
    for (uintptr_t i = 0; i < 8; ++i) assert(!pthread_create(&threads[i], NULL, worker, (void *)i));
    for (unsigned i = 0; i < 8; ++i) assert(!pthread_join(threads[i], NULL));
    uint64_t calls = 0, used = 0;
    for (unsigned i = 0; i < BOOT_CALLOUT_TOTALS; ++i) if (boot_callout_totals[i].key == 0x1000) {
        calls += boot_callout_totals[i].count; used += boot_callout_totals[i].cpu;
    }
    assert(calls == 8001 && used == 10000000);
    uint64_t tokens[BOOT_CALLOUT_ACTIVE];
    for (unsigned i = 0; i < BOOT_CALLOUT_ACTIVE; ++i) assert((tokens[i] = boot_callout_begin(0x3000, 0, 0)));
    assert(!boot_callout_begin(0x3000, 0, 0) && boot_callout_dropped == 1);
    for (unsigned i = 0; i < BOOT_CALLOUT_ACTIVE; ++i) boot_callout_end(tokens[i]);
    assert(!boot_callout_rows(rows, 0));
    for (unsigned i = 0; i < BOOT_CALLOUT_ACTIVE; ++i) assert(!boot_callout_active[i].epoch);
    puts("PASS - callback CPU time, nested timer actions, active records, concurrent totals and bounded saturation");
}
'''
with tempfile.TemporaryDirectory(prefix='sheng-callout-') as directory:
    work = Path(directory)
    (work / 'callout-probe.h').write_bytes(header.read_bytes())
    (work / 'test.c').write_text(harness)
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-pthread',
                    '-fsanitize=address,undefined', str(work / 'test.c'), '-o', str(work / 'test')], check=True)
    subprocess.run([str(work / 'test')], check=True)
