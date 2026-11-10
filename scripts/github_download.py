"""Bounded downloads and Git-backed reads of pinned public GitHub sources."""
from __future__ import annotations

from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

RETRIES = 4
MAX_WAIT = 120


@lru_cache(maxsize=1)
def token() -> str | None:
    value = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if not value and shutil.which('gh'):
        try:
            result = subprocess.run(['gh', 'auth', 'token', '--hostname', 'github.com'],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                value = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return value or None


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            old, new = urlsplit(req.full_url), urlsplit(newurl)
            if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
                redirected.remove_header('Authorization')
        return redirected


def retry_delay(error: HTTPError, attempt: int, now: float | None = None) -> float | None:
    now = time.time() if now is None else now
    headers = error.headers or {}
    retry = headers.get('Retry-After')
    limited = headers.get('X-RateLimit-Remaining') == '0' or retry is not None
    if error.code == 403 and not limited:
        body = error.read(8192).decode(errors='replace').lower()
        limited = any(word in body for word in ('rate limit', 'secondary rate', 'abuse detection'))
    if error.code not in (429, 500, 502, 503, 504) and not (error.code == 403 and limited):
        return None
    delay = float(2 ** attempt)
    if retry:
        try:
            delay = max(delay, float(retry))
        except ValueError:
            try:
                delay = max(delay, parsedate_to_datetime(retry).timestamp() - now)
            except (ValueError, TypeError, OverflowError):
                pass
    reset = headers.get('X-RateLimit-Reset')
    if error.code in (403, 429) and reset:
        try:
            delay = max(delay, float(reset) - now + 1)
        except ValueError:
            pass
    return max(0, delay)


def git(directory: Path, *args: str) -> bytes:
    command = ['git', '-C', str(directory), *args]
    retryable = args[0] in ('fetch', 'show', 'cat-file')
    for attempt in range(RETRIES):
        try:
            result = subprocess.run(command, capture_output=True, timeout=180,
                                    env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
            if result.returncode == 0:
                return result.stdout
            if not retryable:
                raise ValueError(f'Git source read failed: {args[0]}')
        except subprocess.TimeoutExpired:
            if not retryable:
                raise
        if attempt == RETRIES - 1:
            raise ValueError('Pinned Git source fetch failed after four attempts')
        print(f'Git source fetch - retry {attempt + 1}/{RETRIES - 1} - wait {2 ** attempt}s', flush=True)
        time.sleep(2 ** attempt)
    raise AssertionError('Unreachable')


@contextmanager
def source_repository(repository: str, revision: str, local: Path | None = None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid GitHub repository')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', revision) or '..' in revision:
        raise ValueError('Invalid pinned source revision')
    if local and local.is_dir():
        try:
            git(local, 'rev-parse', '--verify', revision + '^{tree}')
        except (ValueError, OSError):
            pass
        else:
            yield local, revision
            return
    cache = Path(os.environ.get('UNIXNU_GITHUB_CACHE', '.cache/github-sources'))
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f'{repository}\n{revision}'.encode()).hexdigest()
    folder = cache / key
    # Different build processes can fetch the same reference concurrently.
    with (cache / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (folder / 'config').is_file():
            folder.mkdir(exist_ok=True)
            git(folder, 'init', '--bare', '-q')
        try:
            origin = git(folder, 'remote', 'get-url', 'origin').decode().strip()
        except ValueError:
            git(folder, 'remote', 'add', 'origin', f'https://github.com/{repository}.git')
        else:
            if origin != f'https://github.com/{repository}.git':
                raise ValueError('Cached Git source has an unexpected remote')
        try:
            git(folder, 'rev-parse', '--verify', 'FETCH_HEAD^{tree}')
        except ValueError:
            print(f'GitHub source - {repository} {revision} - fetching Git objects', flush=True)
            git(folder, 'fetch', '--quiet', '--depth', '1', '--filter=blob:none', 'origin', revision)
        git(folder, 'rev-parse', '--verify', 'FETCH_HEAD^{tree}')
        yield folder, 'FETCH_HEAD'


def source_file(repository: str, revision: str, name: str, max_bytes: int) -> bytes:
    if name.startswith('/') or '..' in Path(name).parts:
        raise ValueError('Invalid source path')
    with source_repository(repository, revision) as (folder, ref):
        size = int(git(folder, 'cat-file', '-s', f'{ref}:{name}'))
        if size > max_bytes:
            raise ValueError('Pinned Git source exceeds its size bound')
        return git(folder, 'show', f'{ref}:{name}')


def directory_entries(repository: str, revision: str, name: str, local: Path | None = None) -> list[dict]:
    if name.startswith('/') or '..' in Path(name).parts:
        raise ValueError('Invalid source directory')
    with source_repository(repository, revision, local) as (folder, ref):
        listing = git(folder, 'ls-tree', '-z', f'{ref}:{name}')
    entries = []
    for row in listing.split(b'\0'):
        if not row:
            continue
        fields, filename = row.split(b'\t', 1)
        _, kind, sha = fields.decode().split()
        filename = filename.decode()
        entries.append({'name': filename, 'sha': sha, 'type': 'file' if kind == 'blob' else 'dir',
                        'download_url': f'https://raw.githubusercontent.com/{repository}/'
                                        f'{quote(revision, safe="")}/{quote(name + "/" + filename, safe="/")}'})
    return entries


def release_asset(url: str, timeout: float, max_bytes: int) -> bytes | None:
    location = urlsplit(url)
    match = re.fullmatch(r'/([^/]+/[^/]+)/releases/download/([^/]+)/([^/]+)', location.path)
    if location.hostname != 'github.com' or match is None:
        return None
    repository, tag, name = match.groups()
    metadata = json.loads(fetch_bytes(f'https://api.github.com/repos/{repository}/releases/tags/{tag}',
                                     timeout=timeout, max_bytes=4 * 1024 * 1024))
    for asset in metadata.get('assets', []):
        if asset['name'] == unquote(name):
            print('GitHub release download limited - Trying the official asset API', flush=True)
            return fetch_bytes(f'https://api.github.com/repos/{repository}/releases/assets/{int(asset["id"])}',
                               timeout=timeout, max_bytes=max_bytes, accept='application/octet-stream')
    return None


def fetch_bytes(url: str, *, timeout: float = 30, max_bytes: int = 64 * 1024 * 1024,
                accept: str | None = None) -> bytes:
    location = urlsplit(url)
    headers = {'User-Agent': 'UniXNU-build-download'}
    if accept:
        headers['Accept'] = accept
    # Public raw files need no bearer token; credentials stay on the API host.
    if location.scheme == 'https' and location.hostname == 'api.github.com' and token():
        headers['Authorization'] = 'Bearer ' + token()
    opener = build_opener(SafeRedirect())
    waited = 0.0
    for attempt in range(RETRIES):
        try:
            with opener.open(Request(url, headers=headers), timeout=timeout) as response:
                body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError(f'Download exceeds its size bound: {location.hostname}')
            return body
        except HTTPError as error:
            if error.code in (403, 429) and location.hostname == 'raw.githubusercontent.com':
                parts = location.path.lstrip('/').split('/', 3)
                if len(parts) == 4:
                    error.close()
                    print('GitHub raw download limited - Reusing the pinned Git object transport', flush=True)
                    return source_file('/'.join(parts[:2]), unquote(parts[2]), unquote(parts[3]), max_bytes)
            if error.code in (403, 429) and location.hostname == 'github.com':
                try:
                    alternate = release_asset(url, timeout, max_bytes)
                except (ValueError, OSError, RuntimeError):
                    alternate = None
                if alternate is not None:
                    error.close()
                    return alternate
            delay = retry_delay(error, attempt)
            error.close()
            if delay is None or attempt == RETRIES - 1 or waited + delay > MAX_WAIT:
                raise RuntimeError(f'Download HTTP {error.code} from {location.hostname}; '
                                   'retry budget exhausted or access denied') from error
            print(f'Download HTTP {error.code} - retry {attempt + 1}/{RETRIES - 1} - wait {delay:.0f}s', flush=True)
        except (URLError, TimeoutError, ConnectionError) as error:
            if attempt == RETRIES - 1:
                raise
            delay = float(2 ** attempt)
            print(f'Download {type(error).__name__} - retry {attempt + 1}/{RETRIES - 1} - wait {delay:.0f}s', flush=True)
        time.sleep(delay)
        waited += delay
    raise AssertionError('Unreachable')


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + '.', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
