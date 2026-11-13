// SPDX-License-Identifier: BSD-2-Clause-Patent
#include <stdint.h>
#include <string.h>
#include <assert.h>
#include <stdio.h>
#include <limits.h>
#include <initializer_list>
#include "ContainerLayout.h"
static uint8_t block[4096], uuid[16] = {42};
static void put(unsigned at, uint32_t value) { memcpy(block + at, &value, 4); }
static void seal() {
    uint64_t sum = 0, accumulated = 0;
    for (unsigned i = 8; i < sizeof(block); i += 4) {
        uint32_t word; memcpy(&word, block + i, 4);
        sum = (sum + word) % 0xffffffffULL;
        accumulated = (accumulated + sum) % 0xffffffffULL;
    }
    uint64_t low = 0xffffffffULL - (sum + accumulated) % 0xffffffffULL;
    put(0, low); put(4, 0xffffffffULL - (sum + low) % 0xffffffffULL);
}
static void image(uint64_t bytes) {
    memset(block, 0, sizeof(block));
    put(24, 1); put(32, 0x4253584e); put(36, 4096);
    put(40, bytes / 4096); put(44, (bytes / 4096) >> 32);
    memcpy(block + 72, uuid, 16); seal();
}
int main() {
    constexpr uint64_t gib = 1ULL << 30;
    for (uint64_t partition : {49*gib, 128*gib, 256*gib, 512*gib}) {
        const uint64_t usable = apfsMediaBytes(partition);
        assert(usable == partition - gib);
        image(48*gib); uint64_t actual = 0;
        assert(apfsContainerLayout(block, partition, 48*gib, uuid, &actual) && actual == 48*gib);
        image(usable);
        for (unsigned boot = 0; boot < 2; boot++)
            assert(apfsContainerLayout(block, partition, 48*gib, uuid, &actual) && actual == usable);
        image(usable + 4096); assert(!apfsContainerLayout(block, partition, 48*gib, uuid));
        image(47*gib); assert(!apfsContainerLayout(block, partition, 48*gib, uuid));
    }
    image(48*gib); assert(!apfsContainerLayout(block, 48*gib, 48*gib, uuid));
    assert(!apfsContainerLayout(block, 128*gib+1, 48*gib, uuid));
    assert(!apfsContainerLayout(block, gib, 48*gib, uuid));
    block[72] ^= 1; seal(); assert(!apfsContainerLayout(block, 128*gib, 48*gib, uuid));
    image(48*gib); block[4000] ^= 1; assert(!apfsContainerLayout(block, 128*gib, 48*gib, uuid));
    image(48*gib); put(40, UINT32_MAX); put(44, UINT32_MAX); seal();
    assert(!apfsContainerLayout(block, 128*gib, 48*gib, uuid));
    assert(apfsMediaBytes(UINT64_MAX) == 0);
    puts("APFS grow - geometry, repeated boot and reserve boundaries - PASS");
}
