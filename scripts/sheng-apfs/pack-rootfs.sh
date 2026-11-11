#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: %s [--raw-only [--link-raw]] --partition-bytes BYTES CONTAINER_DIR OUTPUT_DIR\n' "${0##*/}"
}

raw_only=0
link_raw=0
if [[ ${1:-} == --help ]]; then usage; exit 0; fi
if [[ ${1:-} == --raw-only ]]; then raw_only=1; shift; fi
if [[ $raw_only == 1 && ${1:-} == --link-raw ]]; then link_raw=1; shift; fi
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
metadata=
cleanup() {
    local code=$?
    if [[ -n "$metadata" ]]; then
        chmod -R u+rwX -- "$metadata"
        find "$metadata" -depth -delete
    fi
    if [[ $code != 0 ]]; then
        if [[ $link_raw == 1 && -e "$output/rootfs.img" ]]; then rm -- "$output/rootfs.img"; fi
        rmdir -- "$output" 2>/dev/null || true
    fi
}
trap cleanup EXIT
build_output=$output
workspace=$(dirname -- "$output")
if [[ $link_raw == 1 ]]; then
    ln -- "$input/container.apfs" "$output/rootfs.img"
    # Keep mkosi's Linux filesystem metadata off a mounted macOS workspace.
    metadata=$(mktemp -d)
    build_output="$metadata/output"
    workspace="$metadata"
fi
mkosi -C "$project" --build-sources "$input:inputs" \
    --build-sources "$project/..:tools" --output-directory "$build_output" \
    --workspace-directory "$workspace" \
    --environment "APFS_PARTITION_BYTES=$partition_bytes" \
    --environment "APFS_RAW_ONLY=$raw_only" --environment "APFS_LINK_RAW=$link_raw" build
if [[ $link_raw == 1 ]]; then
    cp -- "$build_output/rootfs.json" "$build_output/SHA256SUMS" "$output/"
fi
