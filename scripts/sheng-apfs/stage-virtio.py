#!/usr/bin/env python3
"""Stage the bounded APFS VirtIO backend for full-image QEMU tests."""

import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def stage(source, output, receipt):
    spec = importlib.util.spec_from_file_location('apfs_driver_stage', ROOT / 'stage-driver.py')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    output.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix='virtio-', dir=output))
    shutil.copytree(source, destination, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('.git', '.codegraph', 'build'))
    subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(ROOT / 'native-virtio.patch')],
                   cwd=destination, check=True)
    shutil.copy2(ROOT / 'ContainerLayout.h', destination)
    adapter.write_config(destination, receipt)
    return destination / 'NativeVirtioBlock.cpp'
