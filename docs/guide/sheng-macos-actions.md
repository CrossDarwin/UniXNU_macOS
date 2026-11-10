# Build a sheng macOS test package

The `sheng macOS test package` workflow builds macOS 27.3 (`26C5035r`) for the
256 GB sheng layout. It runs on the `xcode-27` ARM64 macOS runner and pins the
device and UEFI repositories in the workflow.

## Run

Set the repository Actions secret `UNIXNU_UNIQUE_CHIP_ID` to the installation's
eight-digit hexadecimal platform seed. Keep that value for later builds.

Manually run the workflow on `sheng/macos-actions`. Select `init_data` for a first
installation when no cached Data seed exists. Later builds reuse the seed.
Pushing commits does not start a build.

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

## Progress and reuse

IPSW extraction reports bytes, percentage, average speed and estimated remaining
time every ten seconds. A quiet transfer reports how long it has received no new
data. Failed HTTP requests report retries and backoff.

Later runs reuse up to 2 GiB of downloaded HTTP ranges, a source archive up to
1 GiB and a compiler-output archive up to 2 GiB. Data seeds keep their separate
installation cache. Root image ranges require a matching URL, size and HTTP
validator; cached blocks are hashed before use. The first downloaded ranges are
retained when the budget fills, so sequential reads do not evict the data needed
by the next run. Data beyond the budget is downloaded again.

Source revisions select the source cache. Compiler outputs additionally require
matching tracked inputs, workspace, Python, Xcode, SDK, LLVM and Rust versions.
Oversized archives are skipped. Download caches are saved after failed runs too;
no installation seed is regenerated on a cache miss without `init_data`.

These caches take effect on the next manual run and are populated by that run.
They do not change a workflow already running.

## Disk space

The workflow removes unused Xcode installations and simulators while preserving
selected Xcode. It requires at least 50 GiB free before downloading OS images
and stops early with the available capacity if the runner is too small.

After decryption, encrypted AEA copies are removed while plain images and
verification records remain. Root preparation produces a layout and hash report;
the final sparse image is assembled directly. Compression streams into split
files without retaining a second complete archive.
