# sheng APFS images

Build a native APFS System/Data volume group and its matching boot images for
sheng. The default workflow runs on macOS. Linux with mkosi is available for
the root filesystem packaging step.

## Build the images

Use the sheng macOS build environment with Python 3.11 or newer, rsync 3, the
Apple SDKs and the sheng and uefi-sheng checkouts. Set `UNIXNU_UNIQUE_CHIP_ID`
in the private build environment. Use an APFS work directory with room for
the decrypted IPSW components, APFS container and fastboot image.

```sh
python3 scripts/sheng-apfs/build-images.py \
  --device ../device --uefi ../uefi-sheng /path/to/sheng-images
```

The command extracts and decrypts the selected IPSW, creates and checks the
container, builds the runtime and boot images, and packages the root
filesystem. Enter the administrator password at the APFS creation step if
sudo prompts for it. Use `--ipsw PATH_OR_URL` to provide the pinned IPSW
yourself or `--rsync PATH` to select rsync 3.

New images start with a 48 GiB APFS container. On first boot, the native driver
exposes the actual userdata capacity minus a fixed 1 GiB diagnostic reserve.
The root service grows the container to that boundary. System, Data and VM then
share the expanded space without per-volume quotas. A larger device uses the
same initial image; `--size-gib` sets the initial capacity, not the final limit.

Each boot checks the actual container and volume UUIDs before resizing. A
completed expansion is skipped. A failed attempt preserves the data and records
the error for the next boot. Read `/var/log/unixnu-apfs-grow.log` and
`/var/db/unixnu/apfs-grow.json` for the initial, target and actual capacities.
The service does not format the disk or shrink a container.

Use `--user rinnrei --display-name RinnRei --auto-login` to provision a local
administrator and skip Setup Assistant. Enter the account password at the
prompt. Keep that workspace private.

The workspace contains `boot/boot.img`, `boot/payload.img`, `rootfs/rootfs.img`,
and `rootfs/rootfs.fastboot.img`. Verify the package from the workspace with
`shasum -a 256 -c SHA256SUMS`. Installing the root filesystem replaces
`userdata`.

Android sparse output is enabled by default. Flash `rootfs/rootfs.fastboot.img`
to avoid fastboot scanning the full logical capacity of `rootfs/rootfs.img`.
Use `--raw-only` only when the Android sparse transport is not needed.
Packaging checks the extra storage requirement before encoding.

```sh
fastboot -S 512M flash userdata rootfs/rootfs.fastboot.img
```

Use `--boot-template DIR` to reuse a verified sheng transport. The directory
must contain the version 0 `boot.img` and its `boot.json`. Use
`--runtime-dir DIR` to reuse an existing matched GPU addon and trust bundle.

Use `--prepare-only --container DIR` to rebuild the boot images against an
existing container. Repeated `--kernel-arg=NAME=VALUE` options add boot
arguments. With `boot-ui-trace=1`, macOS captures WindowServer, Dock, Finder
and loginwindow. `boot-callout-trace=1` records callback CPU totals and active
callbacks in the persistent log. `CALL kind=0` identifies a thread call;
`kind=1` identifies its timer action. Their CPU totals overlap.

Native APFS mounts the receipt's VM role volume at `/System/Volumes/VM`
before starting launchd. The boot log reports `vm-volume=0` after a verified
writable mount. `[vm backing]` records compressor mode, swap files and bytes.
Used swap can stay at zero while memory pressure is low.

Native APFS grants temporary I/O priority to user threads reading compressed
pages from swap. The next user fault or syscall clears the grant. Read
`vm.swapin_iotier_boost` and `vm.swapin_iotier_grants` for its state and counter;
`vm-swapin-io=0` disables the policy for comparison.

The disposable QEMU workload uses `test-memory-runtime.sh` and
`test-memory-reclaim.c`. It inherits its scheduling policy unless explicitly
given `--foreground`. Qualify its journal after readback:

```sh
python3 scripts/sheng-apfs/verify-memory-test.py --require-io-boost /path/to/reclaim.log
```

PASS requires compression, decompression, swap writes and swap reads, a complete
data check, and kernel I/O grants with unchanged workload priority.

The Data Cryptex view preserves the original Safari shortcut. Native APFS boot
repairs missing view paths and registers Safari in the Aqua session. Read
`/var/log/unixnu-safari-repair.log` and
`~/Library/Logs/UniXNU/Safari.log` when Safari is absent from the launcher.

If packaging fails after the boot build, retry the last step:

```sh
python3 scripts/sheng-apfs/build-images.py --pack-only /path/to/sheng-images
```

To use mkosi, run the macOS command with `--prepare-only`, transfer the
workspace with sparse files preserved, and run this on Linux with mkosi
26 or newer:

```sh
python3 scripts/sheng-apfs/build-images.py --use-mkosi /path/to/sheng-images
```

## Build the container separately

Use Python 3 and rsync 3 on macOS. Prepare the decrypted `OS`, `Cryptex1,SystemOS`, and
`Cryptex1,AppOS` images from the same restore build. Pass the source APFS
volume UUIDs when an image has more than one volume.

The build VM must allow unrestricted filesystem access so root can preserve BSD
restricted flags and rootless attributes. The command tests this before copying.

```sh
sudo python3 scripts/sheng-apfs/make-container.py --size-gib 48 \
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

## Grow an existing container

A populated image needs at least 8 GiB of free APFS space for first boot and
swap. New builds start at 48 GiB and expand on the device. Reusing an image keeps
its size on the build host. To expand a local image while preserving its accounts,
files and volume UUIDs, grow an APFS clone on the macOS build host:

```sh
sudo python3 scripts/sheng-apfs/make-container.py \
  --grow-container /path/to/current-container --size-gib 64 \
  /path/to/larger-container
```

The command checks the filesystem and volume identities and writes a new
receipt. Rebuild the boot package with that receipt, then package the larger
container. Installing an older local image replaces changes made on the device;
use a current device backup when those changes must be kept.

## Add xART to an existing container

If macOS refuses to create xART with `-69624`, the new container builder
creates an empty volume, assigns the xART role offline, then checks the image
with `fsck_apfs`. For a previously built container that lacks xART, use the
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

## Provision a local account

On the macOS build host, clone the populated container and create the first
administrator. The tool prompts for the password and marks Setup Assistant
complete. Use `--auto-login` for an unattended test device.

```sh
sudo python3 scripts/sheng-apfs/provision-data.py with-xart private-container \
  --user localadmin --display-name 'Local Admin' --auto-login
```

Use the new receipt to build the matching kernel and payload. Keep the
provisioned container private because it contains the account credentials.

## Package an existing container

Use the target partition's capacity in bytes. For Linux with mkosi, add
`--use-mkosi` before `--partition-bytes`. Preserve sparse files when
transferring the container, for example with `rsync -aS`.

```sh
scripts/sheng-apfs/pack-rootfs.sh --partition-bytes 249497890816 \
  /path/to/apfs-container /path/to/sheng-package
cd /path/to/sheng-package
shasum -a 256 -c SHA256SUMS
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
./unixnu build kernel payload --os macos-27 --device ../device --uefi ../uefi-sheng \
  --runtime-dir /path/to/prepared-runtime
./unixnu bootimg --os macos-27 --device ../device --uefi ../uefi-sheng
```

The kernel binds the minimum initial size and container UUID, and limits the
writable medium to userdata minus the diagnostic reserve. The payload binds
the System, Data and VM volume UUIDs. System mounts read-only, with the dyld cache in that volume.
The prepared runtime directory supplies the final addon and its matching trust
bundle through `generated.json`.
The APFS backend provides writable Data and reserves the end of `userdata`
for diagnostics. This development image has no System volume seal or per-block
signature verification. This profile also accepts older development images whose
dyld cache lost restricted flags, limited to the bound read-only System volume
at `/System/Library/dyld`; signature validation remains enabled.
Installing it replaces the existing `userdata` layout.

## Test automatic growth

Run the disposable image test inside a macOS QEMU VM with at least 6 GiB free:

```sh
python3 scripts/sheng-apfs/test-grow-macos.py /path/to/new-test-directory
```

The test grows a 4 GiB container to 8 GiB, checks retry and repeated-start behavior,
verifies Data and VM writes across the old boundary, and checks the reserved tail.
It removes the disposable device image after success. Use `--skip-io` to repeat
the metadata and failure tests without the large allocation.
