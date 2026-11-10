#!/bin/sh
# eisy Backup USB mount setup
set -eu

MODE="${1:-}"

case "$MODE" in
    --check|--generate|--install) ;;
    *)
        echo "Usage: sudo sh setup_usb.sh --check|--generate|--install"
        exit 1
        ;;
esac

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run with sudo."
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

case "$SCRIPT_DIR" in
    /var/eisyui/ns/plugin_[0-9]*) ;;
    *)
        echo "ERROR: Run from the installed eisy Backup plugin directory."
        exit 1
        ;;
esac

/usr/local/bin/python3 - "$SCRIPT_DIR" "$MODE" <<'PY'
import json
import os
import re
import subprocess
import tempfile
import sys
from pathlib import Path

base = Path(sys.argv[1]).resolve()
mode = sys.argv[2]

if not re.fullmatch(r"/var/eisyui/ns/plugin_[0-9]+", str(base)):
    sys.exit("ERROR: Invalid installed plugin directory")

config_path = base / "persist" / "config.json"

if not config_path.is_file():
    sys.exit("ERROR: USB configuration not found")

config = json.loads(config_path.read_text())
device = config.get("usb_device", "")
registered_id = config.get("usb_volume_id", "")

if not isinstance(device, str) or not re.fullmatch(
    r"da[0-9]+(?:s[0-9]+|p[0-9]+)", device
):
    sys.exit("ERROR: Invalid USB partition")

if registered_id and (
    not isinstance(registered_id, str)
    or not re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}", registered_id)
):
    sys.exit("ERROR: Invalid registered volume ID")

partition = Path("/dev") / device

if not partition.exists():
    sys.exit(f"ERROR: {partition} not found")

with partition.open("rb") as f:
    boot = f.read(512)

if len(boot) != 512 or boot[510:512] != b"\x55\xaa":
    sys.exit("ERROR: Invalid FAT boot sector")

if boot[54:62] in (b"FAT12   ", b"FAT16   "):
    serial = boot[39:43]
elif boot[82:90] == b"FAT32   ":
    serial = boot[67:71]
else:
    sys.exit("ERROR: Unsupported filesystem")

value = int.from_bytes(serial, "little")
actual_id = f"{value >> 16:04X}-{value & 0xFFFF:04X}"

if registered_id and registered_id != actual_id:
    sys.exit(
        f"ERROR: Wrong USB drive. Expected {registered_id}, "
        f"found {actual_id}"
    )

print(f"USB partition: {device}")
print(f"FAT volume ID: {actual_id}")

if mode == "--check":
    print("Validation passed. No changes made.")
    sys.exit(0)

slot = base.name.removeprefix("plugin_")
service_path = Path("/usr/local/etc/rc.d/eisy_usbbackup")
generated_path = None

# Generate the service with fixed, validated values.
service = r'''#!/bin/sh
# PROVIDE: eisy_usbbackup
# REQUIRE: FILESYSTEMS
# KEYWORD: shutdown
# EISY_USB_DEVICE=__DEVICE__ VOLUME_ID=__VOLUME_ID__

. /etc/rc.subr

name="eisy_usbbackup"
rcvar="eisy_usbbackup_enable"
start_cmd="eisy_usbbackup_start"
stop_cmd="eisy_usbbackup_stop"

usb_device="__DEVICE__"
usb_volume_id="__VOLUME_ID__"
usb_plugin_user="plugin___SLOT__"
usb_mount_point="/mnt/usbbackup"

eisy_usbbackup_verify()
{
    /usr/local/bin/python3 - "$usb_device" "$usb_volume_id" <<'VERIFY'
import re
import sys
from pathlib import Path

device, expected = sys.argv[1:]

if not re.fullmatch(r"da[0-9]+(?:s[0-9]+|p[0-9]+)", device):
    sys.exit(1)

if not re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}", expected):
    sys.exit(1)

try:
    with (Path("/dev") / device).open("rb") as f:
        boot = f.read(512)

    if len(boot) != 512 or boot[510:512] != b"\x55\xaa":
        sys.exit(1)

    if boot[54:62] in (b"FAT12   ", b"FAT16   "):
        serial = boot[39:43]
    elif boot[82:90] == b"FAT32   ":
        serial = boot[67:71]
    else:
        sys.exit(1)

    value = int.from_bytes(serial, "little")
    actual = f"{value >> 16:04X}-{value & 0xFFFF:04X}"
    sys.exit(0 if actual == expected else 1)
except OSError:
    sys.exit(1)
VERIFY
}

eisy_usbbackup_mounted()
{
    /sbin/mount -p | /usr/bin/awk -v mp="$usb_mount_point" \
        '$2 == mp { print $1, $3, $4 }'
}

eisy_usbbackup_start()
{
    if ! eisy_usbbackup_verify; then
        echo "ERROR: Registered USB volume ID does not match."
        return 1
    fi

    mounted="$(eisy_usbbackup_mounted)"

    if [ -n "$mounted" ]; then
        case "$mounted" in
            "/dev/$usb_device msdosfs rw"*)
                echo "Registered USB drive already mounted."
                return 0
                ;;
            *)
                echo "ERROR: Mount point occupied by another filesystem."
                return 1
                ;;
        esac
    fi

    uid=$(/usr/bin/id -u "$usb_plugin_user") || return 1
    gid=$(/usr/bin/id -g "$usb_plugin_user") || return 1

    /bin/mkdir -p "$usb_mount_point"

    /sbin/mount_msdosfs -u "$uid" -g "$gid" -m 700 \
        "/dev/$usb_device" "$usb_mount_point"
}

eisy_usbbackup_stop()
{
    mounted="$(eisy_usbbackup_mounted)"

    case "$mounted" in
        "/dev/$usb_device msdosfs "*)
            if ! eisy_usbbackup_verify; then
                echo "ERROR: Drive identity mismatch; refusing to unmount."
                return 1
            fi
            /sbin/umount "$usb_mount_point"
            ;;
        "")
            return 0
            ;;
        *)
            echo "ERROR: Another filesystem occupies the mount point."
            return 1
            ;;
    esac
}

load_rc_config "$name"
: ${eisy_usbbackup_enable:="NO"}

run_rc_command "$1"
'''

service = (
    service.replace("__DEVICE__", device)
    .replace("__VOLUME_ID__", actual_id)
    .replace("__SLOT__", slot)
)

# Securely create a unique root-owned temporary file.
fd, filename = tempfile.mkstemp(
    prefix=f"eisy_usbbackup_slot{slot}_",
    suffix=".generated",
    dir="/tmp",
)
generated_path = Path(filename)

with os.fdopen(fd, "w") as f:
    f.write(service)

os.chmod(generated_path, 0o600)

subprocess.run(
    ["/bin/sh", "-n", str(generated_path)],
    check=True,
)

print(f"Generated service: {generated_path}")

if mode == "--generate":
    print("No installed service or mount was changed.")
    sys.exit(0)

# Install or upgrade without stopping or unmounting the USB drive.
import shutil

if service_path.is_symlink():
    sys.exit("ERROR: Service path is a symlink")

# Allow first-time installation and replacement of an existing drive.
# Never overwrite the user's existing .previous service backup.
backup_path = None

mount_result = subprocess.run(
    ["/sbin/mount", "-p"],
    capture_output=True,
    text=True,
    check=True,
)

mounted = [
    line.split()
    for line in mount_result.stdout.splitlines()
    if len(line.split()) >= 4
    and line.split()[1] == "/mnt/usbbackup"
]

if len(mounted) > 1:
    sys.exit("ERROR: Multiple filesystems found at USB mount point")

if mounted:
    entry = mounted[0]
    if (
        entry[0] != f"/dev/{device}"
        or entry[2] != "msdosfs"
        or "rw" not in entry[3].split(",")
    ):
        sys.exit(
            "ERROR: Another drive is mounted. "
            "Safely unmount it before registering a replacement."
        )

if service_path.exists():
    fd, backup_name = tempfile.mkstemp(
        prefix=".eisy_usbbackup_rollback_",
        dir=str(service_path.parent),
    )
    os.close(fd)
    backup_path = Path(backup_name)
    shutil.copy2(service_path, backup_path)
    os.chmod(backup_path, 0o600)

# Stage the replacement on the destination filesystem.
fd, staged_name = tempfile.mkstemp(
    prefix=".eisy_usbbackup_",
    dir=str(service_path.parent),
)
staged_path = Path(staged_name)

try:
    with os.fdopen(fd, "w") as f:
        f.write(service)
    os.chmod(staged_path, 0o755)
    subprocess.run(
        ["/bin/sh", "-n", str(staged_path)],
        check=True,
    )
except Exception:
    staged_path.unlink(missing_ok=True)
    raise

try:
    os.replace(staged_path, service_path)

    result = subprocess.run(
        ["/bin/sh", str(service_path), "start"],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or "Mount service validation failed"
        )

    # Enable automatic mounting at boot after successful validation.
    subprocess.run(
        ["/usr/sbin/sysrc", "eisy_usbbackup_enable=YES"],
        check=True,
    )

except Exception as exc:
    if backup_path is not None and backup_path.exists():
        os.replace(backup_path, service_path)
        sys.exit(f"ERROR: Installation failed; previous service restored: {exc}")

    if service_path.exists():
        service_path.unlink()

    sys.exit(f"ERROR: First-time installation failed: {exc}")

if backup_path is not None:
    backup_path.unlink(missing_ok=True)

print("USB mount service installed and validated.")
print(f"Registered USB drive: {device} ({actual_id})")
print("Existing USB mount and backup files were preserved.")

PY

# Install USB control helper and restricted sudo permissions.
if [ "$MODE" = "--install" ]; then

    SLOT="${SCRIPT_DIR##*_}"
    PLUGIN_USER="plugin_${SLOT}"

    if ! id "$PLUGIN_USER" >/dev/null 2>&1; then
        echo "ERROR: Plugin user $PLUGIN_USER not found."
        exit 1
    fi

    HELPER="/usr/local/sbin/eisy-usb-control"
    SUDOERS="/usr/local/etc/sudoers.d/eisy-usb-control"

    TMP_HELPER=$(mktemp)
    TMP_SUDOERS=$(mktemp)

    trap 'rm -f "$TMP_HELPER" "$TMP_SUDOERS"' EXIT

    cat > "$TMP_HELPER" <<'HELPER'
#!/bin/sh
set -eu

SERVICE=/usr/local/etc/rc.d/eisy_usbbackup
MOUNT_POINT=/mnt/usbbackup

[ -f "$SERVICE" ] && [ ! -L "$SERVICE" ] || {
    echo "USB mount service not found." >&2
    exit 1
}

case "${1:-}" in
    mount)
        exec /bin/sh "$SERVICE" start
        ;;
    unmount)
        exec /bin/sh "$SERVICE" stop
        ;;
    unregister)
        if /sbin/mount -p | /usr/bin/awk \
            -v mp="$MOUNT_POINT" '$2 == mp { found=1 } END { exit !found }'
        then
            echo "ERROR: Unmount USB drive before unregistering." >&2
            exit 1
        fi
        /usr/sbin/sysrc eisy_usbbackup_enable=NO
        echo "USB mount service disabled."
        ;;
    *)
        echo "Usage: eisy-usb-control mount|unmount|unregister" >&2
        exit 1
        ;;
esac
HELPER

    printf '%s ALL=(root) NOPASSWD: /usr/local/sbin/eisy-usb-control mount, /usr/local/sbin/eisy-usb-control unmount, /usr/local/sbin/eisy-usb-control unregister\n' \
        "$PLUGIN_USER" > "$TMP_SUDOERS"

    chmod 755 "$TMP_HELPER"
    chmod 440 "$TMP_SUDOERS"

    sh -n "$TMP_HELPER"
    /usr/local/sbin/visudo -c -f "$TMP_SUDOERS"

    install -o root -g wheel -m 755 \
        "$TMP_HELPER" "$HELPER"

    install -o root -g wheel -m 440 \
        "$TMP_SUDOERS" "$SUDOERS"

    /usr/local/sbin/visudo -c -f "$SUDOERS"

    echo "USB control helper installed."
    echo "Restricted sudo permissions installed for $PLUGIN_USER."

fi
