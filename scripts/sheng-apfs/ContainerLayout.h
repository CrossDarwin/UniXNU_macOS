// SPDX-License-Identifier: BSD-2-Clause-Patent
#pragma once

// The experimental writable medium is bounded independently of the GPT and
// the tail used by the boot log. A UUID is an identity, not an integrity seal.
static bool apfsContainerLayout(const uint8_t *block, uint64_t partitionBytes,
                                uint64_t expectedBytes, const uint8_t expectedUUID[16])
{
    const auto word = [block](unsigned at) {
        return uint32_t(block[at]) | uint32_t(block[at + 1]) << 8 |
               uint32_t(block[at + 2]) << 16 | uint32_t(block[at + 3]) << 24;
    };
    if (word(32) != 0x4253584eU || word(36) != 4096 ||
        (word(24) & 0xffff) != 1 || expectedBytes % 4096 ||
        expectedBytes < 4ULL * 1024 * 1024 * 1024 ||
        partitionBytes < 1024ULL * 1024 * 1024 ||
        expectedBytes > partitionBytes - 1024ULL * 1024 * 1024 ||
        uint64_t(word(40)) + (uint64_t(word(44)) << 32) != expectedBytes / 4096)
        return false;
    uint64_t sum = 0, accumulated = 0;
    for (unsigned at = 8; at < 4096; at += 4) {
        sum = (sum + word(at)) % 0xffffffffULL;
        accumulated = (accumulated + sum) % 0xffffffffULL;
    }
    const uint64_t low = 0xffffffffULL - (sum + accumulated) % 0xffffffffULL;
    const uint64_t high = 0xffffffffULL - (sum + low) % 0xffffffffULL;
    if (word(0) != low || word(4) != high) return false;
    for (unsigned i = 0; i < 16; ++i)
        if (block[72 + i] != expectedUUID[i]) return false;
    return true;
}
