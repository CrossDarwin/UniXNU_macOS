"""GitHub limits must preserve pinned data, retry status and credential boundaries."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'scripts'))

import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

import github_download as download

ERRORS = []

def error(code, body=b'', headers=None):
    result = HTTPError('https://api.github.com/test', code, 'test', headers or {}, io.BytesIO(body))
    ERRORS.append(result)
    return result


class Downloads(unittest.TestCase):
    def tearDown(self):
        while ERRORS:
            ERRORS.pop().close()

    def response(self, body):
        result = Mock()
        result.__enter__ = Mock(return_value=io.BytesIO(body))
        result.__exit__ = Mock(return_value=False)
        return result

    def test_primary_limit_uses_reset(self):
        self.assertEqual(download.retry_delay(error(403, headers={
            'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '125'}), 0, now=100), 26)

    def test_secondary_limit_uses_retry_after(self):
        self.assertEqual(download.retry_delay(error(403, b'secondary rate limit', {'Retry-After': '35'}), 0), 35)

    def test_http_date_retry_after(self):
        self.assertEqual(download.retry_delay(error(429, headers={
            'Retry-After': 'Thu, 01 Jan 1970 00:02:00 GMT'}), 0, now=100), 20)

    def test_permission_denial_is_not_a_rate_limit(self):
        self.assertIsNone(download.retry_delay(error(403, b'Resource not accessible'), 0))

    def test_cross_host_redirect_drops_bearer(self):
        request = Request('https://api.github.com/asset', headers={'Authorization': 'Bearer private'})
        redirected = download.SafeRedirect().redirect_request(request, None, 302, '', {}, 'https://assets.example/file')
        self.assertFalse(redirected.has_header('Authorization'))
        same = download.SafeRedirect().redirect_request(request, None, 302, '', {}, 'https://api.github.com/other')
        self.assertEqual(same.get_header('Authorization'), 'Bearer private')
        downgrade = download.SafeRedirect().redirect_request(request, None, 302, '', {}, 'http://api.github.com/other')
        self.assertFalse(downgrade.has_header('Authorization'))

    def test_limit_retries_then_succeeds(self):
        opener = Mock()
        opener.open.side_effect = [error(403, b'API rate limit exceeded', {'Retry-After': '4'}), self.response(b'ok')]
        with patch.object(download, 'build_opener', return_value=opener), \
                patch.object(download, 'token', return_value='private'), patch.object(download.time, 'sleep') as sleep:
            self.assertEqual(download.fetch_bytes('https://api.github.com/test'), b'ok')
        sleep.assert_called_once_with(4)
        self.assertEqual(opener.open.call_args.args[0].get_header('Authorization'), 'Bearer private')

    def test_long_reset_has_a_bounded_wait(self):
        opener = Mock()
        opener.open.side_effect = error(403, headers={'Retry-After': '3600'})
        with patch.object(download, 'build_opener', return_value=opener), \
                patch.object(download, 'token', return_value=None), patch.object(download.time, 'sleep') as sleep:
            with self.assertRaises(RuntimeError):
                download.fetch_bytes('https://api.github.com/test')
        sleep.assert_not_called()

    def test_raw_403_uses_git_without_bearer(self):
        opener = Mock()
        opener.open.side_effect = error(403)
        with patch.object(download, 'build_opener', return_value=opener), \
                patch.object(download, 'source_file', return_value=b'pinned') as git:
            self.assertEqual(download.fetch_bytes('https://raw.githubusercontent.com/owner/repo/abc/path.h'), b'pinned')
        git.assert_called_once_with('owner/repo', 'abc', 'path.h', 64 * 1024 * 1024)
        self.assertFalse(opener.open.call_args.args[0].has_header('Authorization'))

    def test_network_error_retries(self):
        opener = Mock()
        opener.open.side_effect = [URLError('timeout'), self.response(b'ok')]
        with patch.object(download, 'build_opener', return_value=opener), patch.object(download.time, 'sleep'):
            self.assertEqual(download.fetch_bytes('https://codeload.github.com/repo/archive'), b'ok')

    def test_server_failure_has_four_attempts(self):
        opener = Mock()
        opener.open.side_effect = lambda *a, **k: (_ for _ in ()).throw(error(503))
        with patch.object(download, 'build_opener', return_value=opener), patch.object(download.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                download.fetch_bytes('https://codeload.github.com/repo/archive')
        self.assertEqual(opener.open.call_count, 4)

    def test_size_bound(self):
        opener = Mock()
        opener.open.return_value = self.response(b'too long')
        with patch.object(download, 'build_opener', return_value=opener):
            with self.assertRaises(ValueError):
                download.fetch_bytes('https://codeload.github.com/repo/archive', max_bytes=3)

    def test_release_fallback_selects_exact_asset(self):
        metadata = json.dumps({'assets': [{'name': 'tool.zip', 'id': 12}]}).encode()
        with patch.object(download, 'fetch_bytes', side_effect=[metadata, b'zip']) as fetch:
            self.assertEqual(download.release_asset('https://github.com/o/r/releases/download/v1/tool.zip', 30, 100), b'zip')
        self.assertEqual(fetch.call_args.kwargs['accept'], 'application/octet-stream')
        self.assertTrue(fetch.call_args.args[0].endswith('/releases/assets/12'))

    def test_git_header_list_needs_no_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            header = root / 'iokit/DriverKit/Example.h'
            header.parent.mkdir(parents=True)
            header.write_text('example\n')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'Test'], check=True)
            subprocess.run(['git', '-C', str(root), 'tag', 'xnu-test'], check=True)
            with patch.object(download, 'fetch_bytes', side_effect=AssertionError('API used')):
                entries = download.directory_entries('apple-oss-distributions/xnu', 'xnu-test', 'iokit/DriverKit', root)
            self.assertEqual(entries[0]['name'], 'Example.h')
            expected = subprocess.check_output(['git', '-C', str(root), 'hash-object', str(header)], text=True).strip()
            self.assertEqual(entries[0]['sha'], expected)

    def test_incomplete_git_fetch_is_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / 'origin'
            subprocess.run(['git', 'init', '-q', str(origin)], check=True)
            (origin / 'headers').mkdir()
            (origin / 'headers/Test.h').write_text('test\n')
            subprocess.run(['git', '-C', str(origin), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(origin), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'Test'], check=True)
            repository, revision = 'owner/repo', 'fixed-ref'
            key = hashlib.sha256(f'{repository}\n{revision}'.encode()).hexdigest()
            cached = root / 'cache' / key
            cached.parent.mkdir()
            subprocess.run(['git', 'init', '--bare', '-q', str(cached)], check=True)
            subprocess.run(['git', '-C', str(cached), 'remote', 'add', 'origin', 'https://github.com/owner/repo.git'], check=True)
            (cached / 'FETCH_HEAD').write_bytes(b'')
            actual_git = download.git
            fetches = []

            def git(folder, *args):
                if args[0] == 'fetch':
                    fetches.append(args)
                    subprocess.run(['git', '-C', str(folder), 'fetch', '-q', str(origin), 'HEAD'], check=True)
                    return b''
                return actual_git(folder, *args)

            with patch.dict(os.environ, {'UNIXNU_GITHUB_CACHE': str(cached.parent)}), patch.object(download, 'git', side_effect=git):
                self.assertEqual(download.directory_entries(repository, revision, 'headers')[0]['name'], 'Test.h')
                self.assertEqual(download.directory_entries(repository, revision, 'headers')[0]['name'], 'Test.h')
            self.assertEqual(len(fetches), 1)

    def test_atomic_failure_keeps_existing_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'cached'
            target.write_bytes(b'old')
            with patch.object(Path, 'replace', side_effect=OSError('full')):
                with self.assertRaises(OSError):
                    download.atomic_write(target, b'new')
            self.assertEqual(target.read_bytes(), b'old')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_bad_firmware_does_not_replace_valid_cache(self):
        spec = importlib.util.spec_from_file_location('adreno', (Path(__file__).resolve().parents[1] / 'scripts/fetch-adreno-inputs.py'))
        adreno = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adreno)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'fw'
            target.write_bytes(b'old')
            with patch.object(adreno, 'fetch_bytes', return_value=b'bad'):
                with self.assertRaises(ValueError):
                    adreno.fetch((target, 'https://raw.githubusercontent.com/o/r/rev/fw',
                                  {'sha256': '0' * 64, 'bytes': 3}, None, None))
            self.assertEqual(target.read_bytes(), b'old')

    def test_verified_firmware_reuses_cache_without_network(self):
        spec = importlib.util.spec_from_file_location('adreno', (Path(__file__).resolve().parents[1] / 'scripts/fetch-adreno-inputs.py'))
        adreno = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adreno)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'fw'
            body = b'cached'
            target.write_bytes(body)
            expected = {'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}
            blob = hashlib.sha1(b'blob 6\0' + body).hexdigest()
            with patch.object(adreno, 'fetch_bytes', side_effect=AssertionError('cache fetched again')):
                self.assertEqual(adreno.fetch((target, 'https://raw.githubusercontent.com/o/r/rev/fw', expected, blob, None)), 6)


if __name__ == '__main__':
    unittest.main()
