"""The target OS image (IPSW): a local file or a remote URL, read member by member.

A remote IPSW is never downloaded as a whole. The zip directory and the members that are needed are read
with HTTP range requests, so the same code serves a local file and an https URL. Members are cached under
out/ipsw/<build>/ with their sha256, and checked against the pins of board.toml [ipsw.sha256] when given.

The board describes the image in [ipsw]: url (the default remote source), product, version, build, device_class,
chip_id, board_id, and the components the steps use.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
import hashlib
import io
import json
import os
import platform
import plistlib
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from .util import Error, expand, log, lzfse, sha256_file, tool
from .progress import Progress

CHUNK = 8 << 20


class RangeDiskCache:
    """Keep the first downloaded ranges within a fixed budget across runner restarts."""
    def __init__(self, root, identity, limit):
        self.root, self.limit = Path(root), limit
        self.directory = self.root / hashlib.sha256(identity.encode()).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.used = sum(path.stat().st_size for path in self.root.glob('*/*.block'))

    def get(self, start, size):
        path = self.directory / f'{start:016x}.block'
        if not path.is_file():
            return None
        data = path.read_bytes()
        if len(data) == size + 32 and hashlib.sha256(data[32:]).digest() == data[:32]:
            return data[32:]
        self.used -= len(data)
        path.unlink()
        log(f'ipsw range cache: discarded corrupt block at {start:#x}')
        return None

    def put(self, start, data):
        path = self.directory / f'{start:016x}.block'
        if (path.exists() or self.used + len(data) + 32 > self.limit
                or shutil.disk_usage(self.root).free < len(data) + 8 * 2**30):
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix='.partial', delete=False) as output:
                temporary = Path(output.name)
                output.write(hashlib.sha256(data).digest())
                output.write(data)
            temporary.replace(path)
            self.used += len(data) + 32
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class RangeFile(io.RawIOBase):
    """A read-only seekable view of a remote file, fetched with HTTP range requests and a small block cache."""

    def __init__(self, url, block=CHUNK, retries=4, workers=None):
        self.url, self.block, self.retries = url, block, retries
        self.workers = int(os.environ.get('UNIXNU_IPSW_WORKERS', '4')) if workers is None else workers
        if not 1 <= self.workers <= 8 or block <= 0 or retries <= 0:
            raise Error('IPSW workers must be 1..8; block size and retries must be positive')
        self.prefetch_end = 0
        self.stats_lock = threading.Lock()
        self.position = 0
        self.cache = {}
        self.downloaded = self.reused = 0
        request = urllib.request.Request(url, method='HEAD')
        with urllib.request.urlopen(request, timeout=60) as response:
            self.size = int(response.headers['Content-Length'])
            validator = response.headers.get('ETag') or response.headers.get('Last-Modified')
            self.validator = validator if validator and not validator.startswith('W/') else response.headers.get('Last-Modified')
        self.disk_cache = None
        directory = os.environ.get('UNIXNU_IPSW_RANGE_CACHE')
        if directory and validator:
            limit = int(os.environ.get('UNIXNU_IPSW_CACHE_MIB', '5120')) * 2**20
            if limit < 0:
                raise Error('UNIXNU_IPSW_CACHE_MIB must not be negative')
            self.disk_cache = RangeDiskCache(directory, f'{url}\n{self.size}\n{validator}', limit)
        probe = urllib.request.Request(url, headers={'Range': 'bytes=0-0'})
        with urllib.request.urlopen(probe, timeout=60) as response:
            if response.status != 206:
                raise Error(f'{url} does not support range requests')

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = {0: offset, 1: self.position + offset, 2: self.size + offset}[whence]
        return self.position

    @contextmanager
    def prefetch(self, compressed_bytes):
        """Read ahead over blocks intersecting the selected ZIP member; metadata stays serial."""
        previous = self.prefetch_end
        self.prefetch_end = min(self.size, self.position + compressed_bytes)
        log(f'ipsw: {self.workers} range connections - {self.block / 2**20:g} MiB blocks'
            f' - read-ahead bound {self.workers * self.block / 2**20:g} MiB')
        try:
            yield
        finally:
            self.prefetch_end = previous

    def _fetch(self, index, stop):
        start = index * self.block
        end = min(start + self.block, self.size) - 1
        for attempt in range(self.retries):
            if stop.is_set():
                raise Error('IPSW range read cancelled')
            try:
                headers = {'Range': f'bytes={start}-{end}'}
                if self.validator:
                    headers['If-Range'] = self.validator
                with urllib.request.urlopen(urllib.request.Request(self.url, headers=headers), timeout=30) as response:
                    if response.status != 206:
                        raise OSError('range request did not return HTTP 206')
                    expected = f'bytes {start}-{end}/{self.size}'
                    if response.headers.get('Content-Range') != expected:
                        raise OSError('range response does not match the requested offset or source size')
                    data = response.read(end - start + 2)
                if len(data) != end - start + 1:
                    raise OSError('short or oversized range response')
                with self.stats_lock:
                    self.downloaded += len(data)
                return data
            except (OSError, urllib.error.URLError) as error:
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                if attempt == self.retries - 1:
                    raise
                log(f'ipsw: retry {attempt + 1}/{self.retries - 1} at byte {start} - {type(error).__name__}'
                    f' - backoff {2 ** attempt}s')
                if stop.wait(2 ** attempt):
                    raise Error('IPSW range read cancelled') from error

    def _remember(self, index, data):
        if index not in self.cache and len(self.cache) >= max(4, self.workers):
            self.cache.pop(next(iter(self.cache)))
        self.cache[index] = data

    def _block(self, index):
        if index in self.cache:
            return self.cache[index]
        count = 1
        if index * self.block < self.prefetch_end:
            count = min(self.workers, (self.prefetch_end + self.block - 1) // self.block - index)
        missing = []
        for number in range(index, index + count):
            if number in self.cache:
                continue
            start = number * self.block
            size = min(self.block, self.size - start)
            data = self.disk_cache.get(start, size) if self.disk_cache else None
            if data is not None:
                self._remember(number, data)
                self.reused += len(data)
            else:
                missing.append(number)
        stop = threading.Event()
        # Workers only fetch. Cache writes remain ordered on the reader thread.
        if missing:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(missing)), thread_name_prefix='ipsw-range') as pool:
                futures = [(number, pool.submit(self._fetch, number, stop)) for number in missing]
                try:
                    failure = None
                    for number, future in futures:
                        try:
                            data = future.result()
                        except Exception as error:
                            failure = failure or error
                            continue
                        self._remember(number, data)
                        if self.disk_cache:
                            self.disk_cache.put(number * self.block, data)
                    if failure is not None:
                        raise failure
                except BaseException:
                    stop.set()
                    for _, future in futures:
                        future.cancel()
                    raise
        return self.cache[index]

    def readinto(self, buffer):
        want = min(len(buffer), self.size - self.position)
        done = 0
        while done < want:
            index, inside = divmod(self.position, self.block)
            data = self._block(index)[inside:inside + want - done]
            buffer[done:done + len(data)] = data
            done += len(data)
            self.position += len(data)
        return done


def spec_of(ctx):
    """The source: --ipsw, UNIXNU_IPSW, or the url of board.toml [ipsw]. A URL is remote, anything else a local path."""
    spec = ctx.option_text('ipsw', 'UNIXNU_IPSW') or ctx.board.get('ipsw', {}).get('url')
    if not spec:
        raise Error('no IPSW: pass --ipsw PATH_OR_URL (or UNIXNU_IPSW), or set url in board.toml [ipsw]')
    return spec


class Ipsw:
    def __init__(self, ctx, spec=None):
        self.ctx = ctx
        self.config = ctx.board.get('ipsw')
        if not self.config:
            raise Error('board.toml has no [ipsw]')
        self.spec = spec or spec_of(ctx)
        self.remote = self.spec.startswith(('http://', 'https://'))
        if self.remote:
            log(f'ipsw: remote {self.spec}')
            self.stream = RangeFile(self.spec)
        else:
            path = expand(self.spec)
            if not path.is_file():
                raise Error(f'IPSW not found: {path}')
            log(f'ipsw: local {path}')
            self.stream = path.open('rb')
        self.zip = zipfile.ZipFile(self.stream)
        self.manifest = plistlib.loads(self.zip.read('BuildManifest.plist'))
        self.identity = self._identity()
        self.cache = ctx.out_root / 'ipsw' / self.config['build']

    def _identity(self):
        c, m = self.config, self.manifest
        if (m['ProductVersion'], m['ProductBuildVersion']) != (c['version'], c['build']) or c['product'] not in m['SupportedProductTypes']:
            raise Error(f'this IPSW is {m["ProductVersion"]} ({m["ProductBuildVersion"]}) for {", ".join(m["SupportedProductTypes"])}; '
                        f'the board expects {c["version"]} ({c["build"]}) for {c["product"]}')
        for item in m['BuildIdentities']:
            if (item['Info']['DeviceClass'] == c['device_class'] and item['Info']['RestoreBehavior'] == 'Erase'
                    and int(item['ApChipID'], 16) == c['chip_id'] and int(item['ApBoardID'], 16) == c['board_id']):
                return item
        raise Error(f'no Erase build identity for {c["device_class"]} in this IPSW')

    def member(self, component):
        try:
            return self.identity['Manifest'][component]['Info']['Path']
        except KeyError:
            raise Error(f'component {component} is not in this IPSW') from None

    def info(self, component):
        name = self.member(component)
        entry = self.zip.getinfo(name)
        return name, entry.file_size, entry.compress_size

    def extract(self, component):
        """Extract a component into the cache (skipped when it is there and matches its pin); returns its path."""
        name, size, _ = self.info(component)
        destination = self.cache / Path(name).name
        pin = self.config.get('sha256', {}).get(Path(name).name)
        record = destination.with_suffix(destination.suffix + '.json')
        if destination.is_file() and destination.stat().st_size == size:
            if pin is None or (record.is_file() and json.loads(record.read_text()).get('sha256') == pin) or sha256_file(destination) == pin:
                log(f'ipsw: cache hit {component} ({size >> 20} MiB)')
                return destination
            destination.unlink()
        self.cache.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + '.partial')
        digest = hashlib.sha256()
        log(f'ipsw: extracting {component} ({name}, {size >> 20} MiB)')
        with Progress(f'ipsw {component}', size) as progress, self.zip.open(name) as source, partial.open('wb') as target:
            window = self.stream.prefetch(self.zip.getinfo(name).compress_size) if self.remote else nullcontext()
            with window:
                while chunk := source.read(CHUNK):
                    target.write(chunk)
                    digest.update(chunk)
                    progress.advance(len(chunk))
        if partial.stat().st_size != size:
            partial.unlink()
            raise Error(f'incomplete extraction of {name}')
        if pin and digest.hexdigest() != pin:
            partial.unlink()
            raise Error(f'{name} differs from the pinned sha256 of board.toml [ipsw.sha256]: {digest.hexdigest()}')
        partial.rename(destination)
        if self.remote:
            log(f'ipsw transfer totals: {self.stream.downloaded / 2**20:.1f} MiB downloaded'
                f' - {self.stream.reused / 2**20:.1f} MiB reused from range cache')
        record.write_text(json.dumps({'component': component, 'member': name, 'size': size, 'sha256': digest.hexdigest(),
                                      'source': 'remote' if self.remote else 'local'}, indent=2) + '\n')
        return destination

    def read(self, component):
        return self.zip.read(self.member(component))

    def disk_image(self, component):
        """A component as a plain disk image: extracted, then decrypted when the IPSW ships it as AEA."""
        # AEA inputs may have been removed after decryption. Validate the plain
        # image and both recorded identities before downloading the archive again.
        name = self.member(component)
        if name.endswith('.aea'):
            archive = self.cache / Path(name).name
            plain = archive.with_suffix('')
            record = plain.with_suffix(plain.suffix + '.json')
            origin = archive.with_suffix(archive.suffix + '.json')
            if plain.is_file() and record.is_file() and origin.is_file():
                data, source = json.loads(record.read_text()), json.loads(origin.read_text())
                pin = self.config.get('sha256', {}).get(archive.name)
                if source.get('member') == name and data.get('member') == name \
                        and data.get('aea_sha256') == source.get('sha256') \
                        and (pin is None or pin == source.get('sha256')) \
                        and data.get('size') == plain.stat().st_size \
                        and sha256_file(plain) == data.get('sha256'):
                    prune_aea(archive)
                    return plain
        path = self.extract(component)
        if path.suffix != '.aea':
            return path
        return decrypt_aea(path)


def prune_aea(path):
    """Drop only a downloaded encrypted copy after its plain image and records exist."""
    if os.environ.get('UNIXNU_PRUNE_AEA') != '1':
        return
    out = path.with_suffix('')
    if (out.is_file() and out.with_suffix(out.suffix + '.json').is_file()
            and path.with_suffix(path.suffix + '.json').is_file() and path.is_file()):
        size = path.stat().st_size
        path.unlink()
        log(f'ipsw: released encrypted copy {path.name} ({size / 2**20:.0f} MiB)')


def decrypt_aea(path):
    """Decrypt an AEA archive beside it (name without .aea), cached with the sha256 of both files.

    The key is fetched by `ipsw fw aea --key` from the key URL in the archive header and passed to macOS aea;
    it is not written to disk or the log.
    """
    out = path.with_suffix('')
    record = out.with_suffix(out.suffix + '.json')
    if out.is_file() and record.is_file():
        data = json.loads(record.read_text())
        if data.get('aea_sha256') == json.loads(path.with_suffix(path.suffix + '.json').read_text()).get('sha256') \
                and data.get('size') == out.stat().st_size:
            prune_aea(path)
            return out
    if platform.system() != 'Darwin':
        raise Error(f'{path.name} is AEA encrypted; decrypting it needs macOS /usr/bin/aea')
    tool('aea', 'macOS /usr/bin/aea decrypts AEA disk images')
    ipsw = tool('ipsw', 'the ipsw tool fetches the AEA key (brew install blacktop/tap/ipsw)')
    log(f'ipsw: fetching the AEA key of {path.name}')
    result = subprocess.run([ipsw, 'fw', 'aea', '--key', str(path)], capture_output=True, text=True)
    keys = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith('base64:')]
    if result.returncode or len(keys) != 1:
        raise Error(f'could not fetch the AEA key of {path.name}: {result.stderr.strip()[-300:]}')
    partial = out.with_suffix(out.suffix + '.partial')
    log(f'ipsw: decrypting {path.name}')
    result = subprocess.run(['/usr/bin/aea', 'decrypt', '-i', str(path), '-o', str(partial), '-key-value', keys[0]],
                            capture_output=True, text=True)
    if result.returncode:
        partial.unlink(missing_ok=True)
        raise Error(f'aea decrypt of {path.name} failed: {result.stderr.strip()[-300:]}')
    partial.rename(out)
    source = json.loads(path.with_suffix(path.suffix + '.json').read_text())
    record.write_text(json.dumps({'member': source.get('member'), 'aea_sha256': source.get('sha256'),
                                  'size': out.stat().st_size, 'sha256': sha256_file(out)}, indent=2) + '\n')
    prune_aea(path)
    return out


def _der(data, at):
    tag, length = data[at], data[at + 1]
    at += 2
    if length & 0x80:
        count = length & 0x7f
        length = int.from_bytes(data[at:at + count], 'big')
        at += count
    return tag, at, length


def im4p_payload(data):
    """The payload of an IM4P (DER: IM4P, type, version, data, ...)."""
    tag, start, length = _der(data, 0)
    if tag != 0x30:
        raise Error('not an IM4P')
    at, items = start, []
    while at < start + length:
        _, body, size = _der(data, at)
        items.append(data[body:body + size])
        at = body + size
    if len(items) < 4 or items[0] != b'IM4P':
        raise Error('not an IM4P')
    return items[3]


def kernelcache(ctx, source):
    """The decompressed arm64 kernelcache Mach-O of the IPSW, in out/ipsw (checked against the board pin)."""
    spec = ctx.board.get('kernel', {}).get('prelink', {})
    out = source.cache / 'kernelcache.decompressed'
    pin = spec.get('kernelcache_sha256')
    if out.is_file() and (pin is None or sha256_file(out) == pin):
        return out
    if platform.system() == 'Linux':
        decode = [lzfse(), '-decode']
    else:
        tool('compression_tool', 'macOS /usr/bin/compression_tool decodes the LZFSE kernelcache')
        decode = ['/usr/bin/compression_tool', '-decode', '-a', 'lzfse']
    source.cache.mkdir(parents=True, exist_ok=True)
    payload = im4p_payload(source.read('KernelCache'))
    packed, raw = source.cache / 'kernelcache.lzfse', source.cache / 'kernelcache.fat'
    packed.write_bytes(payload)
    result = subprocess.run([*decode, '-i', packed, '-o', raw], capture_output=True, text=True)
    if result.returncode:
        raise Error(f'{Path(decode[0]).name} failed: {result.stderr.strip()}')
    fat = raw.read_bytes()
    if fat[:4] == b'\xca\xfe\xba\xbe':
        count = struct.unpack('>I', fat[4:8])[0]
        slices = [struct.unpack('>5I', fat[8 + 20 * i:28 + 20 * i]) for i in range(count)]
        arm64 = [s for s in slices if s[0] == 0x0100000c]
        if len(arm64) != 1:
            raise Error('expected exactly one arm64 slice in the kernelcache')
        _, _, offset, size, _ = arm64[0]
        fat = fat[offset:offset + size]
    packed.unlink()
    raw.unlink()
    if fat[:4] != b'\xcf\xfa\xed\xfe':
        raise Error('the decompressed kernelcache is not a 64-bit Mach-O')
    out.write_bytes(fat)
    if pin and sha256_file(out) != pin:
        out.unlink()
        raise Error(f'the kernelcache of this IPSW does not match the pin of board.toml (sha256 {pin})')
    return out
