// SPDX-License-Identifier: BSD-2-Clause-Patent
#pragma once

static constexpr uint64_t kApfsLogReserveBytes = 1ULL << 30;

// The physical store ends before the diagnostic reserve, regardless of the
// initial image size. Reject unaligned and undersized device geometry.
static uint64_t apfsMediaBytes(uint64_t partitionBytes)
{
    return !(partitionBytes & 4095) && partitionBytes > kApfsLogReserveBytes
        ? partitionBytes - kApfsLogReserveBytes : 0;
}

// Growth preserves identity while changing geometry. UUIDs are not seals.
static bool apfsContainerLayout(const uint8_t *block, uint64_t partitionBytes,
                                uint64_t expectedBytes, const uint8_t expectedUUID[16],
                                uint64_t *currentBytes = nullptr)
{
    const auto word = [block](unsigned at) {
        return uint32_t(block[at]) | uint32_t(block[at + 1]) << 8 |
               uint32_t(block[at + 2]) << 16 | uint32_t(block[at + 3]) << 24;
    };
    const uint64_t blocks = uint64_t(word(40)) | uint64_t(word(44)) << 32;
    const uint64_t usable = apfsMediaBytes(partitionBytes);
    if (word(32) != 0x4253584eU || word(36) != 4096 ||
        (word(24) & 0xffff) != 1 || expectedBytes % 4096 ||
        expectedBytes < 4ULL * 1024 * 1024 * 1024 ||
        !usable || expectedBytes > usable || blocks < expectedBytes / 4096 ||
        blocks > usable / 4096)
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
    if (currentBytes) *currentBytes = blocks * 4096;
    return true;
}
