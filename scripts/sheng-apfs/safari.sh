#!/bin/bash
set -euo pipefail

cryptex=/System/Cryptexes/App/System/Applications/Safari.app
preboot=/System/Volumes/Preboot/Cryptexes/App/System/Applications/Safari.app
data=/System/Volumes/Data/Applications/Safari.app
applications=/Applications/Safari.app
lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister

is_safari() {
    [[ -r "$1/Contents/Info.plist" && -x "$1/Contents/MacOS/Safari" ]]
}

repair_view() {
    local root directory name target link
    root=/System/Volumes/Data
    directory="$root"
    for name in System Cryptexes; do
        directory="$directory/$name"
        [[ ! -L "$directory" ]] || return 1
        if [[ ! -e "$directory" ]]; then
            /bin/mkdir -m 755 "$directory" || return 1
        fi
        [[ -d "$directory" ]] || return 1
    done
    for name in App OS; do
        target=/System/Volumes/Preboot/Cryptexes/App
        [[ "$name" != OS ]] || target=/
        link="$directory/$name"
        if [[ -L "$link" ]]; then
            [[ "$(/usr/bin/readlink "$link")" == "$target" ]] || return 1
        elif [[ -e "$link" ]]; then
            return 1
        else
            /bin/ln -s "$target" "$link" || return 1
        fi
    done
    # The restricted Apple shortcut must retain its inode, flags and relative target.
    is_safari "$data"
}

register_safari() {
    # Register the application entry users launch, after its full Cryptex path resolves.
    is_safari "$cryptex" && is_safari "$preboot" && is_safari "$applications" &&
        "$lsregister" -f "$applications"
}

case "${1:-}" in
    repair) action=repair_view ;;
    register)
        directory="$HOME/Library/Logs/UniXNU"
        /bin/mkdir -p "$directory"
        exec >> "$directory/Safari.log" 2>&1
        action=register_safari ;;
    *) exit 64 ;;
esac

for attempt in 1 2 3 4 5; do
    if "$action"; then
        printf '%s\n' "Safari $1 completed"
        exit 0
    fi
    if [[ "$attempt" != 5 ]]; then
        /bin/sleep 3
    fi
done
printf '%s\n' "Safari $1 failed: App Cryptex bundle unavailable or inaccessible" >&2
exit 1
