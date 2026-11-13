"""Validate the installed guest namespace without following links into the build host."""

import os
from pathlib import Path, PurePosixPath
import plistlib
import posixpath
import stat
import time


class Namespace:
    def __init__(self, system, data, preboot):
        self.mounts = {'/': system, '/System/Volumes/Data': data,
                       '/System/Volumes/Preboot': preboot}
        for line in (system / 'usr/share/firmlinks').read_text().splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            source, target = line.split()
            if '..' in PurePosixPath(source).parts or '..' in PurePosixPath(target).parts:
                raise ValueError('Invalid firmlink path')
            source = '/' + source.lstrip('/')
            if source in self.mounts:
                raise ValueError(f'Duplicate firmlink or mount: {source}')
            self.mounts[source] = data / target.lstrip('/')

    def physical(self, path):
        prefix = max((name for name in self.mounts if name == '/' or path == name or
                      path.startswith(name + '/')), key=len)
        relative = path[len(prefix):].lstrip('/')
        for root in getattr(self, 'overlays', {}).get(prefix, (self.mounts[prefix],)):
            candidate = root / relative
            if candidate.exists() or candidate.is_symlink():
                return candidate
        return self.mounts[prefix] / relative

    def resolve(self, path, *, follow_final=True):
        pending = list(PurePosixPath(path).parts[1:])
        if not path.startswith('/'):
            raise ValueError('Expected an absolute guest path')
        current, links = '/', 0
        while pending:
            component = pending.pop(0)
            current = posixpath.normpath(posixpath.join(current, component))
            physical = self.physical(current)
            info = physical.lstat()
            if stat.S_ISLNK(info.st_mode) and (pending or follow_final):
                links += 1
                if links > 40:
                    raise ValueError(f'Guest symlink loop: {path}')
                target = os.readlink(physical)
                logical = target if target.startswith('/') else posixpath.join(posixpath.dirname(current), target)
                pending = list(PurePosixPath(posixpath.normpath(logical)).parts[1:]) + pending
                current = '/'
        return self.physical(current)


class ReferenceNamespace(Namespace):
    def __init__(self, system, sources):
        original = sources.get('system', system)
        template = original / 'System/Library/Templates/Data'
        super().__init__(system, template, original / 'System/Volumes/Preboot')
        roots = [sources[name] for name in ('system_os', 'system') if name in sources]
        self.overlays = {}
        if roots:
            for prefix in ('/', '/System/Cryptexes/OS', '/System/Volumes/Preboot/Cryptexes/OS'):
                self.mounts[prefix] = roots[0]
                self.overlays[prefix] = roots
        if 'app_os' in sources:
            for prefix in ('/System/Cryptexes/App', '/System/Volumes/Preboot/Cryptexes/App'):
                self.mounts[prefix] = sources['app_os']


BUNDLE_SUFFIXES = ('.app', '.xpc', '.appex', '.framework', '.prefPane', '.plugin', '.bundle')


def bundle_inventory(source, prefix='/'):
    bundles = set()
    for directory in ('Applications', 'System/Applications', 'System/Library', 'Library', 'usr/libexec'):
        root = source / directory
        if not root.is_dir() or root.is_symlink():
            continue
        def failed(error):
            raise error
        for current, directories, files in os.walk(root, followlinks=False, onerror=failed):
            for name in directories + [name for name in files if (Path(current) / name).is_symlink()]:
                if name.endswith(BUNDLE_SUFFIXES):
                    path = Path(current) / name
                    bundles.add(posixpath.join(prefix, path.relative_to(source).as_posix()))
    return bundles


def reference_inventory(sources):
    bundles, files = {}, {}
    ordered = [(source, '/System/Volumes/Preboot/Cryptexes/App' if name == 'app_os' else '/')
               for name, source in sources.items()]
    if 'system' in sources:
        ordered.append((sources['system'] / 'System/Library/Templates/Data', '/'))
    for source, prefix in ordered:
        for bundle in sorted(bundle_inventory(source, prefix)):
            relative = bundle[len(prefix):].lstrip('/') if prefix != '/' else bundle.lstrip('/')
            origin = source / relative
            bundles[bundle] = origin
            if origin.is_symlink():
                continue
            def failed(error):
                raise error
            for current, directories, names in os.walk(origin, followlinks=False, onerror=failed):
                for name in names + [name for name in directories if (Path(current) / name).is_symlink()]:
                    file = Path(current) / name
                    logical = posixpath.join(bundle, file.relative_to(origin).as_posix())
                    files[logical] = file
    return bundles, files


def audit_apps(system, data, preboot, sources):
    namespace = Namespace(system, data, preboot)
    original_namespace = ReferenceNamespace(system, sources)
    references, files = reference_inventory(sources)
    bundles = set(references) | bundle_inventory(data) | {'/Applications/Safari.app'}
    executables, omitted, invalid_metadata, missing_source_bundles = 0, [], [], []
    reference_roots = [path.resolve() for path in sources.values()]
    for bundle in sorted(bundles):
        try:
            directory = namespace.resolve(bundle)
        except (FileNotFoundError, NotADirectoryError) as error:
            origin = references.get(bundle)
            if origin is not None and origin.is_symlink():
                try:
                    original_namespace.resolve(bundle)
                except (FileNotFoundError, NotADirectoryError):
                    link = namespace.resolve(bundle, follow_final=False)
                    if link.is_symlink() and os.readlink(link) == os.readlink(origin):
                        missing_source_bundles.append(bundle)
                        continue
            raise ValueError(f'Installed bundle directory unavailable: {bundle}') from error
        if not directory.is_dir():
            if bundle in references and not original_namespace.resolve(bundle).is_dir():
                continue
            raise ValueError(f'Installed bundle directory unavailable: {bundle}')
        found = None
        for relative in ('Contents/Info.plist', 'Resources/Info.plist', 'Info.plist'):
            try:
                found = namespace.resolve(bundle + '/' + relative)
            except (FileNotFoundError, NotADirectoryError):
                continue
            break
        if found is None:
            # Some resource and cache bundles have no executable metadata in the restore image.
            origin = references.get(bundle)
            if origin is not None and not origin.is_symlink() and not any(
                    (origin / path).is_file() for path in ('Contents/Info.plist', 'Resources/Info.plist', 'Info.plist')):
                continue
            raise ValueError(f'Installed bundle metadata missing: {bundle}')
        try:
            metadata = plistlib.loads(found.read_bytes())
        except Exception as error:
            origin = references.get(bundle)
            reference = origin / relative if origin is not None else None
            if (reference is not None and any(reference.resolve().is_relative_to(root) for root in reference_roots)
                    and reference.is_file() and reference.read_bytes() == found.read_bytes()):
                invalid_metadata.append({'bundle': bundle, 'error': str(error)})
                continue
            raise ValueError(f'Invalid installed bundle metadata: {bundle}') from error
        executable = metadata.get('CFBundleExecutable')
        if executable and not bundle.endswith('.framework'):
            if not isinstance(executable, str) or '/' in executable or executable in ('.', '..'):
                raise ValueError(f'Invalid bundle executable: {bundle}')
            executable_path = ('Contents/MacOS/' if relative.startswith('Contents/') else '') + executable
            try:
                path = namespace.resolve(bundle + '/' + executable_path)
            except FileNotFoundError:
                origin = references.get(bundle)
                if origin is not None:
                    try:
                        original_namespace.resolve(bundle + '/' + executable_path)
                    except FileNotFoundError:
                        omitted.append(bundle + '/' + executable_path)
                        continue
                raise ValueError(f'Installed executable missing: {bundle}/{executable_path} - source {origin}')
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
                raise ValueError(f'Installed bundle executable unavailable: {bundle}')
            executables += 1
    updated = time.monotonic()
    for checked, (logical, origin) in enumerate(sorted(files.items()), 1):
        if time.monotonic() - updated >= 10:
            print(f'Application namespace - checking resources {checked}/{len(files)}', flush=True)
            updated = time.monotonic()
        if origin.is_symlink():
            target = namespace.resolve(logical, follow_final=False)
            if not target.is_symlink() or os.readlink(origin) != os.readlink(target):
                raise ValueError(f'Installed bundle shortcut differs: {logical}')
            continue
        target = namespace.resolve(logical)
        expected, actual = origin.stat(), target.stat()
        if not stat.S_ISREG(actual.st_mode) or expected.st_size != actual.st_size:
            raise ValueError(f'Installed bundle resource differs: {logical}')
        if logical.endswith('/Info.plist') and origin.read_bytes() != target.read_bytes():
            raise ValueError(f'Installed bundle metadata differs: {logical}')
    for path in ('/sbin/launchd', '/usr/share/firmlinks',
                 '/System/Cryptexes/OS/System/Library/Frameworks/WebKit.framework',
                 '/System/Cryptexes/App/System/Applications/Safari.app/Contents/Info.plist',
                 '/Applications/Safari.app/Contents/MacOS/Safari'):
        namespace.resolve(path)
    print(f'Application namespace - {len(bundles)} bundles - {executables} executable paths - '
          f'{len(files)} resources verified - {len(omitted)} source-omitted executables', flush=True)
    return {'bundles': len(bundles), 'executables': executables, 'resources': len(files),
            'source_omitted_executables': omitted, 'source_unavailable_bundles': missing_source_bundles,
            'source_invalid_metadata': invalid_metadata, 'safari_entry': '/Applications/Safari.app'}
