# sheng APFS images

This experiment builds a native APFS System/Data volume group for sheng. Use
macOS to create and check the container, then use mkosi on Linux to package it.
Keep the system image cache in the VM and place the Linux output on a data disk.

## Build the container

Use Python 3 on macOS. Prepare the decrypted `OS`, `Cryptex1,SystemOS`, and
`Cryptex1,AppOS` images from the same restore build. Pass the source APFS
volume UUIDs when an image has more than one volume.

```sh
sudo python3 scripts/sheng-apfs/make-container.py --size-gib 32 \
  --system /path/to/OS.dmg --system-os /path/to/SystemOS.dmg \
  --app-os /path/to/AppOS.dmg /path/to/apfs-container
```

The build copies the System volume and dyld cache, seeds Data, and stages
AppOS under Preboot. It checks the container with `fsck_apfs -n` and exports
`container.apfs` and `container.json`. Allow space for both the working image
and the exported container. Build with `--fixture --size-gib 4` to use empty
volumes. The Intel macOS VM used for this experiment requires
`--without-xart`; its APFS tools return error `-69624` when adding that role.

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

## Boot requirements

Keep this package off the tablet until the sheng UFS driver can publish the
single writable container and the boot payload can authenticate and select
its System volume. The current `boot.img` expects the legacy `SHNGROOT`
layout. A matching APFS `boot.img` and `payload.img` are pending.
