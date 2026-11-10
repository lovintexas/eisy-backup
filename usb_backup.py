"""Copy verified eisy backup archives to configured USB storage."""

import os
import re
import shutil
import subprocess
import zipfile
import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)
MOUNT_POINT = Path("/mnt/usbbackup")
BACKUP_PATTERN = re.compile(r"eisy-backup-\d{4}-\d{2}-\d{2}-\d{6}\.zip")


def verify_mount(device):
    """Confirm the selected device is mounted at the expected location."""
    if not re.fullmatch(r"da\d+s\d+", device or ""):
        raise RuntimeError("Invalid USB device selection")

    result = subprocess.run(
        ["/sbin/mount", "-p"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )

    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4 and (
            fields[0] == f"/dev/{device}"
            and fields[1] == str(MOUNT_POINT)
            and fields[2] == "msdosfs"
            and "rw" in fields[3].split(",")
        ):
            return

    raise RuntimeError("Selected USB device is not mounted read/write")


def copy_backup_to_usb(source, device, retention=3):
    """Copy a completed backup and retain the newest USB archives."""
    source = Path(source)
    retention = int(retention)

    if retention < 1 or retention > 100:
        raise ValueError("Invalid USB retention")

    if not source.is_file() or not BACKUP_PATTERN.fullmatch(source.name):
        raise RuntimeError("Invalid backup archive")

    with zipfile.ZipFile(source) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Source backup ZIP failed verification")

    verify_mount(device)

    destination = MOUNT_POINT / source.name
    partial = MOUNT_POINT / (source.name + ".part")

    if source.stat().st_size > shutil.disk_usage(MOUNT_POINT).free:
        raise RuntimeError("Insufficient USB free space")

    try:
        verify_mount(device)

        # Remove an incomplete copy left by a previous interrupted backup.
        if partial.exists():
            if partial.is_symlink() or not partial.is_file():
                raise RuntimeError("Unsafe USB temporary file")
            partial.unlink()

        verify_mount(device)

        with source.open("rb") as src, partial.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())

        if source.stat().st_size != partial.stat().st_size:
            raise RuntimeError("USB backup size mismatch")

        with zipfile.ZipFile(partial) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("USB backup ZIP failed verification")

        verify_mount(device)
        partial.replace(destination)

        archives = sorted(
            (
                p for p in MOUNT_POINT.iterdir()
                if p.is_file() and BACKUP_PATTERN.fullmatch(p.name)
            ),
            key=lambda p: p.name,
            reverse=True,
        )

        for old in archives[retention:]:
            verify_mount(device)
            old.unlink()
            LOGGER.info("Removed expired USB backup: %s", old.name)

        LOGGER.info("USB backup completed: %s", destination.name)
        return destination.name

    finally:
        try:
            verify_mount(device)
            partial.unlink(missing_ok=True)
        except Exception:
            LOGGER.warning("Unable to clean up USB temporary file", exc_info=True)
