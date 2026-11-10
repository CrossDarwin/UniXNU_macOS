# Build a sheng macOS test package

The `sheng macOS test package` workflow builds macOS 27.3 (`26C5035r`) for the
256 GB sheng layout. It runs on the `xcode-27` ARM64 macOS runner and pins the
device and UEFI repositories in the workflow.

## Run

Set the repository Actions secret `UNIXNU_UNIQUE_CHIP_ID` to the installation's
eight-digit hexadecimal platform seed. Keep that value for later builds.

Run the workflow on `sheng/macos-actions`. Select `init_data` for a first
installation when no cached Data seed exists. Later builds reuse the seed.
The initial branch push also permits first-install seed creation.

The workflow verifies every original root block with VerifiedRoot, generates
the signed policy handoff and builds the kernel, firmware and payload. The
userdata image includes the OS, SystemOS and App windows, policy hash tables,
zeroed persistent storage and log space.

## Download

Download both `sheng-macos-installation-seed` and `sheng-macos-test-<run-id>`
within one day. Preserve `data.dbr` for subsequent builds.

Reassemble the downloaded parts in their directory:

```sh
cat sheng-macos-test.tar.zst.part-* > sheng-macos-test.tar.zst
sha256sum -c sheng-macos-test.tar.zst.sha256
tar --zstd -xf sheng-macos-test.tar.zst
sha256sum -c SHA256SUMS
```

`boot.img` is the temporary boot candidate. `userdata-full.img` is an Android
sparse image for the entire 249497890816-byte userdata partition. Flashing it
erases Android user data. Verify the connected device and partition size before
flashing. `test-package.json` records the source revisions and image geometry;
a successful build does not mark hardware testing complete.
