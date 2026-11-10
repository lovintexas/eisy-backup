"""USB storage discovery for FreeBSD eisy."""

import re
import subprocess


def run_command(*args):
    result = subprocess.run(
        args, capture_output=True, text=True, check=True, timeout=10
    )
    return result.stdout


def discover_usb_disks():
    """Return USB-backed disk names using current FreeBSD device data."""
    output = run_command("sysctl", "-a")
    usb_buses = set()
    disks = set()

    for line in output.splitlines():
        match = re.search(r"umass(\d+):\d+:\d+: Attached to scbus(\d+)", line)
        if match:
            usb_buses.add(match.group(2))

    for line in output.splitlines():
        match = re.search(r"\b(da\d+) at umass-sim\d+.*scbus(\d+)", line)
        if match and match.group(2) in usb_buses:
            disks.add(match.group(1))

    return sorted(disks)


if __name__ == "__main__":
    print(discover_usb_disks())


def discover_usb_partitions():
    """List partitions on currently detected USB disks."""
    devices = []

    for disk in discover_usb_disks():
        output = run_command("gpart", "show", "-p", disk)

        for line in output.splitlines():
            parts = line.split()

            if len(parts) < 5:
                continue

            partition = parts[2]

            if not re.fullmatch(rf"{re.escape(disk)}(?:s\d+|p\d+)", partition):
                continue

            sectors = int(parts[1])
            size_mb = round(sectors * 512 / (1024 * 1024))

            devices.append({
                "disk": disk,
                "partition": partition,
                "size_mb": size_mb,
            })

    return devices


if __name__ == "__main__":
    print(discover_usb_partitions())
