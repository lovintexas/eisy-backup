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

            try:
                volume_id = get_fat_volume_id(partition)
            except (ValueError, OSError, subprocess.SubprocessError):
                volume_id = None

            devices.append({
                "disk": disk,
                "partition": partition,
                "size_mb": size_mb,
                "volume_id": volume_id,
            })

    return devices


if __name__ == "__main__":
    print(discover_usb_partitions())


def get_fat_volume_id(partition):
    """Read a FAT16/FAT32 volume serial number without mounting the drive."""
    if not re.fullmatch(r"da\d+(?:s\d+|p\d+)", partition or ""):
        raise ValueError("Invalid USB partition")

    result = subprocess.run(
        ["dd", f"if=/dev/{partition}", "bs=512", "count=1"],
        capture_output=True,
        check=True,
        timeout=10,
    )

    boot = result.stdout
    if len(boot) != 512 or boot[510:512] != b"\x55\xaa":
        raise ValueError("Invalid FAT boot sector")

    if boot[54:62] == b"FAT12   " or boot[54:62] == b"FAT16   ":
        serial = boot[39:43]
    elif boot[82:90] == b"FAT32   ":
        serial = boot[67:71]
    else:
        raise ValueError("Not a supported FAT filesystem")

    if len(serial) != 4:
        raise ValueError("Invalid FAT volume serial number")

    value = int.from_bytes(serial, "little")
    return f"{value >> 16:04X}-{value & 0xFFFF:04X}"
