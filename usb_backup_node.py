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
            result = subprocess.run(
                ["/sbin/mount", "-p"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )

            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 2:
                    device = fields[0]
                    mountpoint = fields[1]
                    if (
                        mountpoint == str(MOUNT_POINT)
                        and device == f"/dev/{selected}"
                    ):
                        mounted = True
                        break

            if mounted:
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=MOUNT_POINT,
                        prefix=".eisy-usb-test-",
                    ):
                        pass
                    writable = True
                except OSError:
                    LOGGER.exception("USB write test failed")

                free_mb = os.statvfs(MOUNT_POINT)
                free_mb = int(
                    free_mb.f_bavail * free_mb.f_frsize / 1048576
                )

        except Exception:
            LOGGER.exception("USB status check failed")

        self.setDriver("GV1", int(mounted))
        self.setDriver("GV2", int(writable))
        self.setDriver("GV3", free_mb)
        self.setDriver("ST", 1 if mounted and writable else 2)

    def query(self, command=None):
        self.check_usb()
        self.reportDrivers()

    def manual_check(self, command=None):
        self.check_usb()
        self.reportDrivers()

    commands = {
        "QUERY": query,
        "CHECK_USB": manual_check,
    }
