#!/bin/sh
set -eu

mkdir -p /Users/Shared/unixnu-test
directory=$(mktemp -d /Users/Shared/unixnu-test/memory.XXXXXXXX)
finish() {
    result=$?
    trap - EXIT
    printf '%s\n' "$result" > "$directory/workload.exit"
    exit "$result"
}
trap finish EXIT

printf 'Memory test - output %s\n' "$directory"
/sbin/mount
# The workload's Mach counters are authoritative; this is supplementary output.
if ! /usr/sbin/sysctl vm.swapusage; then
    printf 'Memory test - swapusage sysctl unavailable - using Mach sampling\n'
fi
/private/var/ngpu/test/bin/QemuReclaim "$directory" "${1:-1792}"
printf 'Memory test - data integrity verified - counter qualification pending\n'
