# sheng APFS images

This experiment builds a native APFS System/Data volume group for sheng. Use
macOS to create and check the container, then use mkosi on Linux to package it.
Keep the system image cache in the VM and place the Linux output on a data disk.

## Build the container

Use Python 3 and rsync 3 on macOS. Prepare the decrypted `OS`, `Cryptex1,SystemOS`, and
`Cryptex1,AppOS` images from the same restore build. Pass the source APFS
volume UUIDs when an image has more than one volume.

The build VM must allow unrestricted filesystem access so root can preserve BSD
restricted flags and rootless attributes. The command tests this before copying.

```sh
sudo python3 scripts/sheng-apfs/make-container.py --size-gib 32 --without-xart \
  --rsync /path/to/rsync \
  --system /path/to/OS.dmg --system-os /path/to/SystemOS.dmg \
  --app-os /path/to/AppOS.dmg /path/to/apfs-container
```

The build copies System, the arm64e shared cache, the Data template, and AppOS.
It restores ownership, modes, BSD flags and rootless attributes from all three
source volumes. It checks the container with `fsck_apfs -n` and writes `container.apfs` and
`container.json`. Repeat the same command after a failure to resume copying.
Build with `--fixture --size-gib 4` to use empty volumes.

To repair an existing container's metadata, pass its original source images.
The command creates an APFS clone in a new output directory and verifies the
volume identities and filesystem before writing a new receipt.

```sh
sudo python3 scripts/sheng-apfs/make-container.py \
  --repair-metadata /path/to/with-xart \
  --system /path/to/OS.dmg --system-os /path/to/SystemOS.dmg \
  --app-os /path/to/AppOS.dmg /path/to/repaired-container
```

## Add xART offline

If macOS reports `-69624` when creating xART, build with `--without-xart` and
keep the original `container.json`. On Linux, use the
[DroidVM APFS tool](https://github.com/Droid-VM/macOS-DroidVM/blob/9db2f6f263173b660536530c2d102f480280cdb3/scripts/apfs-add-volumes.py)
with its pinned apfsprogs dependency. Run it on a copy of the raw container.

```sh
mkdir with-xart
cp --reflink=auto --sparse=always apfs-container/container.apfs with-xart/container.apfs
python3 "$DROIDVM/scripts/apfs-add-volumes.py" add with-xart/container.apfs xART:X
```

Copy `with-xart` to macOS for verification. The command checks the filesystem,
the xART role, and the existing volume identities before writing a new
`container.json`. Use both files from `with-xart` for packaging.

```sh
python3 scripts/sheng-apfs/make-container.py \
  --verify-xart apfs-container/container.json with-xart
```

## Package on Linux

Copy both files to Linux. Use sparse transfer or extraction for
`container.apfs`, such as `rsync -aS`. Install mkosi 26 or later and Python 3.
Use the target partition's capacity in bytes.

```sh
scripts/sheng-apfs/pack-rootfs.sh --partition-bytes 249497890816 \
  /path/to/apfs-container /path/to/sheng-package
cd /path/to/sheng-package
sha256sum -c SHA256SUMS
```

The command produces:

| File | Purpose |
| --- | --- |
| `rootfs.img` | Raw APFS container for disk-image tests |
| `rootfs.fastboot.img` | Android sparse representation, verified against the raw bytes |
| `rootfs.json` | Container geometry, System UUID, volume roles, and validation status |
| `SHA256SUMS` | Package checksums |

Use `--raw-only` before `--partition-bytes` to omit the fastboot image.
Use `--raw-only --link-raw` when both directories are on the same filesystem
and the source should share storage with `rootfs.img`. Keep the linked image
read-only during boot tests by using a QEMU overlay.

## Build the matching boot image

Use the sheng macOS build workspace with the prepared trust bundle and GPU addon.
The writable backend uses ShengUfs revision
`57f031c99d7f69ffa611626bebaeab0731e93254`.

```sh
export UNIXNU_APFS_CONTAINER=/path/to/with-xart/container.json
./unixnu build kernel payload --os macos-27 --device ../device --uefi ../uefi-sheng
./unixnu bootimg --os macos-27 --device ../device --uefi ../uefi-sheng
```

The kernel binds the container size and UUID. The payload binds the System and
Data volume UUIDs. System mounts read-only, with the dyld cache in that volume.
The APFS backend provides writable Data and reserves the end of `userdata`
for diagnostics. This development image has no System volume seal or per-block
signature verification. This profile also accepts older development images whose
dyld cache lost restricted flags, limited to the bound read-only System volume
at `/System/Library/dyld`; signature validation remains enabled.
Installing it replaces the existing `userdata` layout.
