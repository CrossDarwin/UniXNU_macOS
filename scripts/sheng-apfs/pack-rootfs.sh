#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: %s [--use-mkosi] [--raw-only [--link-raw]] --partition-bytes BYTES CONTAINER_DIR OUTPUT_DIR\n' "${0##*/}"
}

use_mkosi=0
raw_only=0
link_raw=0
direct_args=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --help) usage; exit 0 ;;
        --use-mkosi) use_mkosi=1 ;;
        --raw-only) raw_only=1; direct_args+=("$1") ;;
        --link-raw) link_raw=1; direct_args+=("$1") ;;
        *) break ;;
    esac
    shift
done
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
if [[ $link_raw == 1 && $raw_only == 0 ]]; then
    printf '%s\n' '--link-raw requires --raw-only' >&2
    exit 2
fi
if [[ $use_mkosi == 0 ]]; then
    exec python3 "$project/pack-rootfs.py" ${direct_args[@]+"${direct_args[@]}"} \
        --partition-bytes "$partition_bytes" "$input" "$output"
fi
command -v mkosi >/dev/null || {
    printf '%s\n' '--use-mkosi requires mkosi on a Linux host' >&2
    exit 2
}
if [[ -e $output || -L $output ]]; then
    printf 'Output directory already exists: %s\n' "$output" >&2
    exit 2
fi
staged=$(mktemp -d "$(dirname -- "$output")/.rootfs-mkosi.XXXXXX")
metadata=
cleanup() {
    local code=$?
    for path in "$metadata" "$staged"; do
        if [[ -n $path && -d $path ]]; then
            # Unlink the source hard link before changing permissions in staging.
            if [[ $link_raw == 1 && $path == "$staged" && -e "$path/rootfs.img" ]]; then
                rm -- "$path/rootfs.img"
            fi
            chmod -R u+rwX -- "$path" || true
            find "$path" -depth -delete || true
        fi
    done
    return "$code"
}
trap cleanup EXIT
build_output=$staged
workspace=$(dirname -- "$output")
if [[ $link_raw == 1 ]]; then
    ln -- "$input/container.apfs" "$staged/rootfs.img"
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
    cp -- "$build_output/rootfs.json" "$build_output/SHA256SUMS" "$staged/"
fi
python3 - "$staged" "$output" <<'PY'
from pathlib import Path
import os
import sys

stage, output = map(Path, sys.argv[1:])
if os.path.lexists(output):
    sys.exit(f'Output directory already exists: {output}')
stage.rename(output)
PY
