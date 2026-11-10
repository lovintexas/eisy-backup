import os
import subprocess
import tempfile
from pathlib import Path

import udi_interface

LOGGER = udi_interface.LOGGER
MOUNT_POINT = Path("/mnt/usbbackup")


class USBBackup(udi_interface.Node):
    id = "usbbackup"

    drivers = [
        {"driver": "ST", "value": 0, "uom": 25, "name": "USB Status"},
        {"driver": "GV1", "value": 0, "uom": 2, "name": "USB Mounted"},
        {"driver": "GV2", "value": 0, "uom": 2, "name": "USB Writable"},
        {"driver": "GV3", "value": 0, "uom": 56, "name": "USB Free Space MB"},
        {"driver": "GV4", "value": 0, "uom": 25, "name": "Last USB Backup Result"},
    ]

    def __init__(self, polyglot):
        super().__init__(
            polyglot, "controller", "usbbackup", "USB Backup"
        )
        self.poly = polyglot
        self.poly.addNode(self)

    def check_usb(self, command=None):
        from web_server import load_config

        config = load_config()

        if not config.get("usb_enabled"):
            self.setDriver("ST", 0)
            self.setDriver("GV1", 0)
            self.setDriver("GV2", 0)
            self.setDriver("GV3", 0)
            return

        selected = config.get("usb_device", "")
        if not selected:
            self.setDriver("ST", 0)
            self.setDriver("GV1", 0)
            self.setDriver("GV2", 0)
            self.setDriver("GV3", 0)
            return

        mounted = False
        writable = False
        free_mb = 0

        try:
            from usb_backup import verify_mount

            verify_mount(
                selected,
                config.get("usb_volume_id", ""),
            )
            mounted = True

            try:
                with tempfile.NamedTemporaryFile(
                    dir=MOUNT_POINT,
                    prefix=".eisy-usb-test-",
                ):
                    pass
                writable = True
            except OSError:
                LOGGER.exception("USB write test failed")

            stat = os.statvfs(MOUNT_POINT)
            free_mb = int(
                stat.f_bavail * stat.f_frsize / 1048576
            )

        except Exception as exc:
            LOGGER.warning("USB drive verification failed: %s", exc)

        self.setDriver("GV1", int(mounted))
        self.setDriver("GV2", int(writable))
        self.setDriver("GV3", free_mb)
        self.setDriver("ST", 1 if mounted and writable else 2)

    def update_backup_result(self):
        """Restore the most recent USB backup result from history."""
        from backup_engine import get_history

        status_codes = {
            "Success": 1,
            "Failed": 2,
            "Disabled": 3,
        }

        try:
            history = get_history()
            record = next(
                (item for item in history if "usb_status" in item),
                None,
            )
            status = record.get("usb_status") if record else None
            self.setDriver("GV4", status_codes.get(status, 0))
        except Exception:
            LOGGER.exception("Unable to read USB backup result")
            self.setDriver("GV4", 0)

    def query(self, command=None):
        self.check_usb()
        self.update_backup_result()
        self.reportDrivers()

    def manual_check(self, command=None):
        self.check_usb()
        self.reportDrivers()

    commands = {
        "QUERY": query,
        "CHECK_USB": manual_check,
    }
