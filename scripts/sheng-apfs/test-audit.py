#!/usr/bin/env python3
"""Exercise guest namespace resolution across firmlinks and Cryptex shortcuts."""

import os
import shutil
from pathlib import Path
import plistlib
import tempfile
import unittest

from audit import Namespace, audit_apps


class NamespaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = self.root = Path(self.temporary.name)
        self.system, self.data, self.preboot = [root / name for name in ('System', 'Data', 'Preboot')]
        for path in (self.system, self.data, self.preboot):
            path.mkdir()
        file = self.system / 'usr/share/firmlinks'
        file.parent.mkdir(parents=True)
        file.write_text('Applications\tApplications\nSystem/Cryptexes\tSystem/Cryptexes\nprivate\tprivate\n')
        for path in (self.data / 'Applications', self.data / 'System/Cryptexes', self.data / 'private',
                     self.system / 'sbin', self.system / 'System/Volumes', self.system / 'System/Library/Frameworks/WebKit.framework',
                     self.preboot / 'Cryptexes/App/System/Applications/Safari.app/Contents/MacOS'):
            path.mkdir(parents=True)
        (self.system / 'sbin/launchd').touch()
        (self.data / 'System/Cryptexes/OS').symlink_to('/')
        (self.data / 'System/Cryptexes/App').symlink_to('/System/Volumes/Preboot/Cryptexes/App')
        self.shortcut = self.data / 'Applications/Safari.app'
        self.shortcut.symlink_to('../System/Cryptexes/App/System/Applications/Safari.app')
        self.bundle = self.preboot / 'Cryptexes/App/System/Applications/Safari.app'
        (self.bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'Safari'}))
        self.binary = self.bundle / 'Contents/MacOS/Safari'
        self.binary.touch(mode=0o755)

    def test_original_apple_shortcut_resolves_through_data_and_preboot(self):
        namespace = Namespace(self.system, self.data, self.preboot)
        self.assertEqual(namespace.resolve('/Applications/Safari.app/Contents/MacOS/Safari'), self.binary)
        self.assertEqual(namespace.resolve('/System/Volumes/Data/Applications/Safari.app/Contents/MacOS/Safari'), self.binary)
        self.assertEqual(audit_apps(self.system, self.data, self.preboot, {})['executables'], 1)

    def test_missing_executable_and_broken_app_mount_are_rejected(self):
        self.binary.unlink()
        with self.assertRaisesRegex(ValueError, 'Installed executable missing'):
            audit_apps(self.system, self.data, self.preboot, {})
        self.shortcut.unlink()
        self.shortcut.symlink_to('/System/Volumes/Unbound/Safari.app')
        with self.assertRaisesRegex(ValueError, 'directory unavailable'):
            audit_apps(self.system, self.data, self.preboot, {})

    def test_resource_loss_is_rejected_against_restore_source(self):
        source = self.root / 'AppOS'
        shutil.copytree(self.preboot / 'Cryptexes/App', source)
        resource = self.bundle / 'Contents/Resources/Assets.car'
        resource.parent.mkdir()
        resource.write_bytes(b'assets')
        reference = source / 'System/Applications/Safari.app/Contents/Resources'
        reference.mkdir()
        (reference / 'Assets.car').write_bytes(b'assets')
        result = audit_apps(self.system, self.data, self.preboot, {'app_os': source})
        self.assertGreaterEqual(result['resources'], 3)
        resource.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'resource differs'):
            audit_apps(self.system, self.data, self.preboot, {'app_os': source})

    def test_source_omissions_are_distinct_from_packaging_loss(self):
        origin = self.root / 'OS'
        bundle = Path('System/Library/CoreServices/Test.app/Contents/XPCServices/Intel.xpc')
        metadata = bundle / 'Contents/Info.plist'
        for base in (origin, self.system):
            path = base / metadata
            path.parent.mkdir(parents=True)
            path.write_bytes(plistlib.dumps({'CFBundleExecutable': 'Intel'}))
        result = audit_apps(self.system, self.data, self.preboot, {'system': origin})
        self.assertEqual(result['source_omitted_executables'], ['/' + str(bundle / 'Contents/MacOS/Intel')])
        executable = origin / bundle / 'Contents/MacOS/Intel'
        executable.parent.mkdir()
        executable.touch(mode=0o755)
        with self.assertRaisesRegex(ValueError, 'executable missing'):
            audit_apps(self.system, self.data, self.preboot, {'system': origin})

    def test_original_absolute_framework_alias_uses_the_guest_sources(self):
        source = self.root / 'OS'
        bundle = 'System/Library/PrivateFrameworks/Phone.framework'
        for base in (source, self.system):
            framework = base / bundle
            framework.mkdir(parents=True)
            (framework / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'Phone'}))
            alias = base / 'System/Library/LocationBundles/Phone.bundle'
            alias.parent.mkdir(parents=True)
            alias.symlink_to('/' + bundle)
        result = audit_apps(self.system, self.data, self.preboot, {'system': source})
        self.assertIn('/System/Library/LocationBundles/Phone.bundle/Phone', result['source_omitted_executables'])

    def test_executable_shortcut_with_framework_suffix_is_not_a_bundle(self):
        source = self.root / 'OS'
        for base in (source, self.system):
            root = base / 'System/Library/Accessibility/AX.bundle'
            root.mkdir(parents=True)
            (root / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'AX'}))
            (root / 'AX').touch(mode=0o755)
            (root / 'AX.framework').symlink_to('AX')
        result = audit_apps(self.system, self.data, self.preboot, {'system': source})
        self.assertEqual(result['source_omitted_executables'], [])

    def test_unshipped_appleinternal_shortcuts_are_preserved_and_reported(self):
        source = self.root / 'OS'
        relative = 'System/Library/CoreServices/Types.bundle/Contents/_.bundle'
        for base in (source, self.system):
            path = base / relative
            path.parent.mkdir(parents=True)
            path.symlink_to('/AppleInternal/Missing.bundle')
        result = audit_apps(self.system, self.data, self.preboot, {'system': source})
        self.assertEqual(result['source_unavailable_bundles'], ['/' + relative])
        target = self.system / relative
        target.unlink()
        target.symlink_to('/AppleInternal/Changed.bundle')
        with self.assertRaisesRegex(ValueError, 'directory unavailable'):
            audit_apps(self.system, self.data, self.preboot, {'system': source})

    def test_absolute_link_does_not_follow_build_host(self):
        (self.data / 'Applications/Host').symlink_to('/etc/passwd')
        with self.assertRaises(FileNotFoundError):
            Namespace(self.system, self.data, self.preboot).resolve('/Applications/Host')

    def test_symlink_cycle_and_invalid_firmlink_are_rejected(self):
        (self.data / 'Applications/Loop').symlink_to('Loop')
        with self.assertRaisesRegex(ValueError, 'loop'):
            Namespace(self.system, self.data, self.preboot).resolve('/Applications/Loop')
        (self.system / 'usr/share/firmlinks').write_text('Applications\t../host\n')
        with self.assertRaisesRegex(ValueError, 'Invalid firmlink'):
            Namespace(self.system, self.data, self.preboot)


if __name__ == '__main__':
    unittest.main()
