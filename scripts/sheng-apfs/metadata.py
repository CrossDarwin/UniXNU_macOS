"""Restore BSD metadata after merging the restore-image volumes."""

import ctypes
import errno
import os
import stat
import subprocess
import tempfile
import time
from pathlib import Path


_libc = ctypes.CDLL(None, use_errno=True)
_rootless = b'com.apple.rootless'
_nofollow = 1
# Darwin sys/stat.h constants are absent from older Python releases.
_restricted = getattr(stat, 'SF_RESTRICTED', 0x00080000)
_firmlink = getattr(stat, 'SF_FIRMLINK', 0x00800000)
_dataless = getattr(stat, 'SF_DATALESS', 0x40000000)
_compressed = getattr(stat, 'UF_COMPRESSED', 0x00000020)


def rootless_attribute(path):
    # Darwin's six-argument xattr API is not exposed by Python's os module.
    get = _libc.getxattr
    get.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                   ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    get.restype = ctypes.c_ssize_t
    encoded = os.fsencode(path)
    size = get(encoded, _rootless, None, 0, 0, _nofollow)
    if size < 0:
        error = ctypes.get_errno()
        if error == errno.ENOATTR:
            return None
        raise OSError(error, os.strerror(error), str(path))
    value = ctypes.create_string_buffer(size)
    count = get(encoded, _rootless, value, size, 0, _nofollow)
    if count < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(path))
    return value.raw[:count]


def set_rootless_attribute(path, value):
    if value is None:
        call = _libc.removexattr
        call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        result = call(os.fsencode(path), _rootless, _nofollow)
    else:
        call = _libc.setxattr
        call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                         ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        result = call(os.fsencode(path), _rootless, value, len(value), 0, _nofollow)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(path))


def check_file_flags(directory):
    """Fail before copying if the host cannot preserve restricted file flags."""
    descriptor, name = tempfile.mkstemp(prefix='.apfs-flags-', dir=directory)
    os.close(descriptor)
    path = Path(name)
    try:
        os.chflags(path, _restricted)
        if not path.stat().st_flags & _restricted:
            raise OSError('The filesystem did not retain SF_RESTRICTED')
    except PermissionError as error:
        raise ValueError('Cannot preserve System file flags. Enable unrestricted filesystem '
                         'access in the macOS build VM, restart it, and retry as root') from error
    finally:
        os.chflags(path, 0)
        path.unlink()


def restore_protected_directory(source, target):
    # ditto carries Apple's installer entitlement; the BOM excludes every child.
    if not source.is_dir() or source.is_symlink() or source.name != target.name:
        raise ValueError(f'Cannot restore protected metadata without replacing content: {target}')
    if any(character in source.name for character in ('\n', '\r')):
        raise ValueError(f'Unsupported BOM directory name: {source}')
    with tempfile.TemporaryDirectory(prefix='apfs-metadata-') as directory:
        paths, bom = Path(directory) / 'paths', Path(directory) / 'metadata.bom'
        paths.write_text('.\n./' + source.name + '\n')
        subprocess.run(['/usr/bin/mkbom', '-s', '-i', str(paths), str(bom)], check=True)
        print(f'Restoring protected directory metadata - {target}', flush=True)
        subprocess.run(['/usr/bin/ditto', '-X', '--bom', str(bom),
                        str(source.parent), str(target.parent)], check=True)


def restore_metadata(source, target, overlay=None):
    """Preserve ownership, modes, rootless xattrs and logical BSD file flags."""
    checked, changed = 0, 0
    next_update = time.monotonic() + 10

    def copy_one(relative):
        nonlocal checked, changed, next_update
        if overlay is not None:
            # Later images replace entries but merge directories; metadata follows the final owner.
            if os.path.lexists(overlay / relative):
                return
            for parent in relative.parents:
                entry = overlay / parent
                if entry.is_symlink() or (entry.exists() and not entry.is_dir()):
                    return
        src, dst = source / relative, target / relative
        before, after = src.lstat(), dst.lstat()
        if stat.S_IFMT(before.st_mode) != stat.S_IFMT(after.st_mode):
            raise ValueError(f'Metadata target has a different file type: {dst}')
        if before.st_flags & _dataless:
            raise ValueError(f'Restore source contains a dataless placeholder: {src}')
        value = rootless_attribute(src)
        # rsync may expand compressed data; UF_COMPRESSED belongs to its storage representation.
        logical_flags = before.st_flags
        # Firmlink flags belong to the original volume pair; the kernel rebuilds those links.
        if logical_flags & _firmlink:
            logical_flags &= ~(_firmlink | stat.SF_NOUNLINK)
        flags = (logical_flags & ~_compressed) | (after.st_flags & _compressed)
        modified = False
        try:
            if (before.st_uid, before.st_gid) != (after.st_uid, after.st_gid):
                os.chown(dst, before.st_uid, before.st_gid, follow_symlinks=False)
                modified = True
            if not stat.S_ISLNK(before.st_mode) and stat.S_IMODE(before.st_mode) != stat.S_IMODE(after.st_mode):
                os.chmod(dst, stat.S_IMODE(before.st_mode))
                modified = True
            if value != rootless_attribute(dst):
                set_rootless_attribute(dst, value)
                modified = True
            if after.st_flags != flags:
                os.chflags(dst, flags, follow_symlinks=False)
                modified = True
        except PermissionError:
            restore_protected_directory(src, dst)
            modified = True
        actual = dst.lstat()
        if (actual.st_flags != flags or (actual.st_uid, actual.st_gid) != (before.st_uid, before.st_gid)
                or (not stat.S_ISLNK(before.st_mode) and stat.S_IMODE(actual.st_mode) != stat.S_IMODE(before.st_mode))
                or rootless_attribute(dst) != value):
            raise ValueError(f'Metadata verification failed: {dst} - '
                             f'flags {actual.st_flags:#x}/{flags:#x}, '
                             f'owner {actual.st_uid}:{actual.st_gid}/{before.st_uid}:{before.st_gid}, '
                             f'mode {stat.S_IMODE(actual.st_mode):o}/{stat.S_IMODE(before.st_mode):o}, '
                             f'rootless attribute matches: {rootless_attribute(dst) == value}')
        checked += 1
        changed += modified
        if time.monotonic() >= next_update:
            print(f'System metadata - {checked} checked - {changed} changed - {relative}', flush=True)
            next_update = time.monotonic() + 10

    # Apply directory flags after their children so immutable directories remain resumable.
    def walk_error(error):
        raise error

    for root, directories, files in os.walk(source, topdown=False, followlinks=False, onerror=walk_error):
        relative = Path(root).relative_to(source)
        for name in files + directories:
            copy_one(relative / name)
    copy_one(Path('.'))
    print(f'System metadata complete - {checked} checked - {changed} changed', flush=True)
    return {'checked': checked, 'changed': changed}
