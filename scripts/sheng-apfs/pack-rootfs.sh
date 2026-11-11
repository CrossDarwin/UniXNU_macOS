#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: %s [--raw-only] --partition-bytes BYTES CONTAINER_DIR OUTPUT_DIR\n' "${0##*/}"
}

raw_only=0
if [[ ${1:-} == --help ]]; then usage; exit 0; fi
if [[ ${1:-} == --raw-only ]]; then raw_only=1; shift; fi
if [[ $# != 4 || $1 != --partition-bytes || ! $2 =~ ^[1-9][0-9]*$ ]]; then
    usage >&2
    exit 2
fi
partition_bytes=$2
project=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
input=$(cd -- "$3" && pwd -P)
output="$(cd -- "$(dirname -- "$4")" && pwd -P)/$(basename -- "$4")"
[[ -f "$input/container.apfs" && -f "$input/container.json" ]] || {
    printf 'Expected container.apfs and container.json in %s\n' "$input" >&2
    exit 2
}
mkdir -- "$output"
trap 'rmdir -- "$output" 2>/dev/null || true' EXIT
mkosi -C "$project" --build-sources "$input:inputs" \
    --build-sources "$project/..:tools" --output-directory "$output" \
    --workspace-directory "$(dirname -- "$output")" \
    --environment "APFS_PARTITION_BYTES=$partition_bytes" \
    --environment "APFS_RAW_ONLY=$raw_only" build
trap - EXIT
