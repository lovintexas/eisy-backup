#!/usr/bin/env python3

import logging
import subprocess
import secrets
import threading
import time

from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field

from backup_engine import create_backup, get_history, DEFAULT_BACKUP_DIRECTORY
import backup_auth
import backup_sessions
import google_drive

BASE = Path(__file__).resolve().parent
PERSIST = BASE / "persist"
GOOGLE_AUTH_DIRECTORY = BASE / "google_auth"
GOOGLE_AUTH_DIRECTORY.mkdir(mode=0o700, exist_ok=True)
GOOGLE_AUTH_DIRECTORY.chmod(0o700)
PERSIST.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger("eisy-backup-web")

app = FastAPI(title="eisy Backup")

EISY_UI_TOKEN = None
GOOGLE_OAUTH = None


def set_google_oauth(oauth):
    global GOOGLE_OAUTH
    GOOGLE_OAUTH = oauth


def google_oauth_available():
    """Check whether PG3x has supplied valid Google OAuth credentials."""
    if GOOGLE_OAUTH is None:
        return False
    try:
        return bool(GOOGLE_OAUTH.getAccessToken())
    except (ValueError, KeyError):
        return False


def set_eisy_ui_token(token):
    global EISY_UI_TOKEN
    EISY_UI_TOKEN = token



BACKUP_DIRECTORY = DEFAULT_BACKUP_DIRECTORY
COOKIE_NAME = "eisy_backup_session"
COOKIE_MAX_AGE = 3600

backup_state = {
    "running": False,
    "status": "Ready",
    "last_backup": None,
    "error": None
}

state_lock = threading.Lock()
login_attempts = {}
login_lock = threading.Lock()
recovery_attempts = {}
recovery_lock = threading.Lock()

DEFAULT_CONFIG = {
    "retention": 7,
    "google_drive_folder": "eisy Backups",
    "google_auth_mode": "pg3x",
    "usb_enabled": False,
    "usb_device": "",
    "usb_volume_id": "",
    "usb_retention": 7
}


def load_config():
    path = PERSIST / "config.json"
    if path.exists():
        try:
            import json
            return {**DEFAULT_CONFIG, **{k: v for k, v in json.loads(path.read_text()).items() if k in DEFAULT_CONFIG}}
        except Exception:
            LOGGER.exception("Configuration read failed")
    return DEFAULT_CONFIG.copy()


def save_config(data):
    import json
    path = PERSIST / "config.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2))
    temp.chmod(0o600)
    subprocess.run(["setfacl", "-m", "u:polyglot:r:allow", str(temp)], check=True)
    temp.replace(path)


def get_credentials():
    return backup_auth.read_credentials(BASE)


def authenticated(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    # Background polling must not extend the session. Only explicit user
    # activity, marked by the browser, refreshes the inactivity timeout.
    refresh = request.headers.get("X-User-Activity") == "1"
    if not backup_sessions.validate_session(token, refresh=refresh):
        raise HTTPException(401, "Login required")



NAS_TOKEN_FILE = PERSIST / "nas_api_token"


def get_nas_token():
    """Read the preconfigured NAS download token."""
    if NAS_TOKEN_FILE.is_symlink() or not NAS_TOKEN_FILE.is_file():
        raise HTTPException(503, "NAS API token not configured")

    token = NAS_TOKEN_FILE.read_text().strip()
    if not token:
        raise HTTPException(503, "NAS API token not configured")

    return token


def authenticated_nas(request: Request):
    authorization = request.headers.get("Authorization", "")
    scheme, separator, supplied = authorization.partition(" ")

    if not separator or scheme.lower() != "bearer":
        raise HTTPException(401, "Bearer token required")

    expected = get_nas_token()

    if not secrets.compare_digest(supplied.strip(), expected):
        raise HTTPException(401, "Invalid API token")


def client_key(request):
    return request.client.host if request.client else "unknown"


def check_rate_limit(table, key, maximum, interval):
    now = time.monotonic()
    with login_lock:
        attempts = [t for t in table.get(key, []) if now - t < interval]
        if len(attempts) >= maximum:
            table[key] = attempts
            return False
        attempts.append(now)
        table[key] = attempts
        return True


def clear_rate_limit(table, key):
    with login_lock:
        table.pop(key, None)



_backup_status_callback = None


def register_backup_status_callback(callback):
    global _backup_status_callback
    _backup_status_callback = callback


def update_iox_backup_status(value):
    if _backup_status_callback is not None:
        try:
            _backup_status_callback(value)
        except Exception:
            LOGGER.exception("Failed to update IoX backup status")



_usb_backup_result_callback = None


def register_usb_backup_result_callback(callback):
    global _usb_backup_result_callback
    _usb_backup_result_callback = callback


def update_iox_usb_backup_result(value):
    if _usb_backup_result_callback is not None:
        try:
            _usb_backup_result_callback(value)
        except Exception:
            LOGGER.exception("Failed to update IoX USB backup result")


def backup_worker():
    from backup_engine import save_history, DEFAULT_BACKUP_DIRECTORY

    previous_history = get_history()
    previous_start = (
        previous_history[0].get("started") if previous_history else None
    )

    update_iox_backup_status(2)
    try:
        config = load_config()

        with state_lock:
            backup_state["status"] = "Creating native eisy backup"
            backup_state["error"] = None

        if not EISY_UI_TOKEN:
            raise RuntimeError("eisy-ui authentication token unavailable")

        result = create_backup(
            retention=config["retention"],
            token=EISY_UI_TOKEN,
            google_auth_directory=GOOGLE_AUTH_DIRECTORY,
            google_folder=config["google_drive_folder"],
            oauth_provider=(
                GOOGLE_OAUTH
                if config["google_auth_mode"] == "pg3x"
                else None
            )
        )

        # USB copying is optional and independent of Google Drive success.
        if config.get("usb_enabled"):
            try:
                from usb_backup import copy_backup_to_usb

                archive = Path(DEFAULT_BACKUP_DIRECTORY) / result["filename"]
                result["usb_backup"] = copy_backup_to_usb(
                    archive,
                    config.get("usb_device", ""),
                    config.get("usb_retention", 7),
                    volume_id=config.get("usb_volume_id", ""),
                )
                result["usb_status"] = "Success"
            except Exception as exc:
                LOGGER.exception("USB backup copy failed")
                result["usb_status"] = "Failed"
                result["usb_error"] = str(exc)
        else:
            result["usb_status"] = "Disabled"

        # Persist USB results independently of Google Drive status.
        from backup_engine import save_history
        try:
            history = get_history()
            if history and history[0].get("filename") == result["filename"]:
                history[0].update({
                    k: result[k] for k in
                    ("usb_status", "usb_backup", "usb_error")
                    if k in result
                })
                save_history(history[:100], DEFAULT_BACKUP_DIRECTORY)
        except Exception:
            LOGGER.exception("Unable to persist USB backup result")

        with state_lock:
            backup_state["status"] = "Backup completed successfully"
            backup_state["last_backup"] = result
            backup_state["error"] = None

        update_iox_backup_status(1)
        update_iox_usb_backup_result({
            "Success": 1,
            "Failed": 2,
            "Disabled": 3,
        }.get(result["usb_status"], 0))
        LOGGER.info("Backup completed: %s", result["filename"])

    except Exception as exc:
        LOGGER.exception("Backup failed")

        # The engine normally records failures itself. Record failures
        # that occur before the engine starts writing history.
        try:
            history = get_history()
            latest_start = history[0].get("started") if history else None
            if latest_start == previous_start:
                now = datetime.now().astimezone()
                record = {
                    "filename": None,
                    "date": now.isoformat(),
                    "started": now.isoformat(),
                    "completed": now.isoformat(),
                    "size": 0,
                    "path": None,
                    "status": "failed",
                    "successful": False,
                    "error": str(exc),
                    "google_drive": "Not uploaded"
                }
                history.insert(0, record)
                save_history(history[:100], DEFAULT_BACKUP_DIRECTORY)
        except Exception:
            LOGGER.exception("Unable to record early backup failure")

        update_iox_backup_status(3)
        with state_lock:
            backup_state["status"] = "Backup failed"
            backup_state["error"] = str(exc)

    finally:
        with state_lock:
            backup_state["running"] = False


class PasswordSetup(BaseModel):
    password: str
    confirm: str


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class PasswordRecovery(BaseModel):
    code: str
    new_password: str
    confirm_password: str


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTML


@app.get("/api/auth/status")
def auth_status(request: Request):
    credentials = get_credentials()
    token = request.cookies.get(COOKIE_NAME)

    return {
        "setup_required": credentials is None,
        "authenticated": backup_sessions.validate_session(token)
    }


@app.post("/api/auth/setup")
def setup_password(data: PasswordSetup, request: Request, response: Response):
    if get_credentials() is not None:
        raise HTTPException(409, "Administrator password is already configured")

    if data.password != data.confirm:
        raise HTTPException(400, "Passwords do not match")

    try:
        backup_auth.create_initial_password(BASE, data.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    LOGGER.info("Initial administrator password created")
    return {"ok": True}


@app.post("/api/auth/login")
def login(data: LoginRequest, request: Request, response: Response):
    if get_credentials() is None:
        raise HTTPException(409, "Initial password setup is required")

    key = client_key(request)

    if not check_rate_limit(login_attempts, key, 5, 300):
        raise HTTPException(429, "Too many login attempts. Try again later.")

    credentials = get_credentials()

    if (
        credentials is None
        or not secrets.compare_digest(data.username, "admin")
        or not backup_auth.check_password(data.password, *credentials)
    ):
        raise HTTPException(401, "Invalid username or password")

    clear_rate_limit(login_attempts, key)

    token = backup_sessions.create_session()
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME)
    backup_sessions.revoke_session(token)
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return {"ok": True}


@app.post("/api/auth/heartbeat", dependencies=[Depends(authenticated)])
def heartbeat(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not backup_sessions.validate_session(token, refresh=True):
        raise HTTPException(401, "Session expired")
    return {"ok": True}


@app.post("/api/auth/change", dependencies=[Depends(authenticated)])
def change_password(data: PasswordChange):
    if data.new_password != data.confirm_password:
        raise HTTPException(400, "New passwords do not match")

    try:
        backup_auth.change_password(BASE, data.current_password, data.new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    backup_sessions.revoke_all_sessions()

    return {"ok": True}


@app.post("/api/auth/recover")
def recover_password(data: PasswordRecovery, request: Request, response: Response):
    key = client_key(request)

    if not check_rate_limit(recovery_attempts, key, 5, 900):
        raise HTTPException(429, "Too many recovery attempts")

    if data.new_password != data.confirm_password:
        raise HTTPException(400, "New passwords do not match")

    try:
        backup_auth.complete_recovery(BASE, data.code, data.new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    clear_rate_limit(recovery_attempts, key)
    backup_sessions.revoke_all_sessions()

    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    LOGGER.warning("Administrator password recovered")
    return {"ok": True}


class ConfigUpdate(BaseModel):
    google_drive_folder: str = "eisy Backups"
    usb_enabled: bool = False
    usb_retention: int = Field(default=7, ge=1, le=100)


@app.get("/api/usb/devices")
def get_usb_devices(auth=Depends(authenticated)):
    from usb_storage import discover_usb_partitions
    return {"devices": discover_usb_partitions()}



class UsbRegistration(BaseModel):
    partition: str


@app.post("/api/usb/register")
def register_usb_device(data: UsbRegistration, auth=Depends(authenticated)):
    from usb_storage import discover_usb_partitions

    with state_lock:
        if backup_state["running"]:
            raise HTTPException(409, "Backup is running")

        devices = discover_usb_partitions()
        selected = next(
            (d for d in devices if d["partition"] == data.partition),
            None,
        )

        if selected is None:
            raise HTTPException(400, "Selected USB drive was not detected")

        config = load_config()
        config["usb_device"] = data.partition
        config["usb_volume_id"] = ""
        config["usb_enabled"] = False
        save_config(config)

    return {
        "success": True,
        "partition": data.partition,
        "volume_id": "",
        "size_mb": selected["size_mb"],
        "message": "Drive selected. Run USB setup over SSH to verify its identity.",
    }


@app.get("/api/usb/setup-command")
def usb_setup_command(auth=Depends(authenticated)):
    import re

    installed = Path(__file__).resolve().parent

    if not re.fullmatch(
        r"/var/eisyui/ns/plugin_[0-9]+", str(installed)
    ):
        raise HTTPException(
            400, "USB setup command available after plugin installation"
        )

    return {
        "command": f"sudo sh {installed}/setup_usb.sh --install"
    }


@app.post("/api/usb/complete")
def complete_usb_registration(auth=Depends(authenticated)):
    import re
    from usb_backup import verify_mount

    config = load_config()
    device = config.get("usb_device", "")

    if not re.fullmatch(r"da[0-9]+(?:s[0-9]+|p[0-9]+)", device):
        raise HTTPException(400, "Select and register a USB drive first")

    service_path = Path("/usr/local/etc/rc.d/eisy_usbbackup")

    try:
        if service_path.is_symlink():
            raise ValueError("Unexpected service symlink")

        metadata = service_path.read_text()
    except (OSError, ValueError):
        raise HTTPException(400, "USB mount service is not installed")

    match = re.search(
        r"^# EISY_USB_DEVICE=" + re.escape(device) +
        r" VOLUME_ID=([0-9A-F]{4}-[0-9A-F]{4})$",
        metadata,
        re.MULTILINE,
    )

    if not match:
        raise HTTPException(
            400,
            "USB mount service does not match the selected drive"
        )

    volume_id = match.group(1)

    try:
        verify_mount(device, volume_id)
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    config["usb_volume_id"] = volume_id
    config["usb_enabled"] = False
    save_config(config)

    return {
        "success": True,
        "partition": device,
        "volume_id": volume_id,
        "message": "USB registration complete. Backups can now be enabled.",
    }


@app.get("/api/config")
def get_config(auth=Depends(authenticated)):
    return load_config()


@app.post("/api/config")
def update_config(data: ConfigUpdate, auth=Depends(authenticated)):
    config = load_config()
    updated = {**config, **data.model_dump()}

    if updated.get("usb_enabled"):
        from usb_backup import verify_mount

        try:
            verify_mount(
                updated.get("usb_device", ""),
                updated.get("usb_volume_id", ""),
            )
        except (RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    save_config(updated)
    return {"success": True, "config": updated}


def start_backup_job():
    """Start a backup from either IoX or the web interface."""
    if not (
        google_oauth_available()
        if load_config().get("google_auth_mode") == "pg3x"
        else bool((google_drive.load_tokens(GOOGLE_AUTH_DIRECTORY) or {}).get("refresh_token"))
    ):
        raise RuntimeError("Connect Google Drive before backing up")

    with state_lock:
        if backup_state["running"]:
            raise RuntimeError("Backup already running")
        backup_state["running"] = True
        backup_state["status"] = "Starting backup"
        backup_state["error"] = None

    try:
        threading.Thread(target=backup_worker, daemon=True).start()
    except Exception:
        with state_lock:
            backup_state["running"] = False
        raise


@app.post("/api/backup")
def start_backup(auth=Depends(authenticated)):
    if not (
        google_oauth_available()
        if load_config().get("google_auth_mode") == "pg3x"
        else bool((google_drive.load_tokens(GOOGLE_AUTH_DIRECTORY) or {}).get("refresh_token"))
    ):
        raise HTTPException(409, "Connect Google Drive before backing up")
    try:
        start_backup_job()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"success": True, "message": "Backup started"}


google_auth_lock = threading.Lock()
google_auth_state = {}


@app.post("/api/google/start")
def google_start(auth=Depends(authenticated)):
    try:
        client_id, client_secret = google_drive.load_client_credentials(GOOGLE_AUTH_DIRECTORY)
        result = google_drive.start_device_authorization(client_id)

        import time
        with google_auth_lock:
            google_auth_state.clear()
            google_auth_state.update({
                "device_code": result["device_code"],
                "client_id": client_id,
                "client_secret": client_secret,
                "expires_at": time.monotonic() + result["expires_in"],
                "interval": result.get("interval", 5),
                "next_poll": time.monotonic() + result.get("interval", 5),
                "status": "pending",
            })

        return {
            "user_code": result["user_code"],
            "verification_url": result["verification_url"],
            "expires_in": result["expires_in"],
            "interval": result.get("interval", 5),
        }
    except Exception as exc:
        LOGGER.exception("Google authorization start failed")
        raise HTTPException(502, str(exc))


@app.get("/api/google/connection")
def google_connection(auth=Depends(authenticated)):
    tokens = google_drive.load_tokens(GOOGLE_AUTH_DIRECTORY)
    return {
        "connected": bool(tokens and tokens.get("refresh_token"))
    }


@app.get("/api/google/status")
def google_status(auth=Depends(authenticated)):
    import time

    with google_auth_lock:
        if not google_auth_state:
            return {"status": "idle"}

        if google_auth_state["status"] == "authorized":
            return {"status": "authorized"}

        if time.monotonic() >= google_auth_state["expires_at"]:
            google_auth_state.clear()
            return {"status": "expired"}

        if time.monotonic() < google_auth_state["next_poll"]:
            return {"status": "pending"}

        google_auth_state["next_poll"] = (
            time.monotonic() + google_auth_state["interval"]
        )

        try:
            result = google_drive.poll_device_authorization(
                google_auth_state["client_id"],
                google_auth_state["client_secret"],
                google_auth_state["device_code"],
            )

            status = result["status"]

            if status == "authorized":
                google_drive.save_tokens(result["tokens"], GOOGLE_AUTH_DIRECTORY)
                google_auth_state.clear()
                google_auth_state["status"] = "authorized"
                return {"status": "authorized"}

            if status == "slow_down":
                google_auth_state["interval"] += 5

            if status in ("access_denied", "expired_token"):
                google_auth_state.clear()

            return {"status": status}

        except Exception:
            LOGGER.exception("Google authorization polling failed")
            return {"status": "error"}




@app.get("/api/nas/token")
def nas_token_info(auth=Depends(authenticated)):
    token = get_nas_token()
    return Response(
        content=__import__("json").dumps({"token": token}),
        media_type="application/json",
        headers={"Cache-Control": "no-store"}
    )



@app.post("/api/nas/token/regenerate")
def regenerate_nas_token(auth=Depends(authenticated)):
    import os

    token = secrets.token_hex(32)
    temporary = NAS_TOKEN_FILE.with_name("nas_api_token.tmp")

    if NAS_TOKEN_FILE.is_symlink() or temporary.is_symlink():
        raise HTTPException(500, "Unsafe NAS token path")

    fd = os.open(
        str(temporary),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600
    )

    try:
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        temporary.replace(NAS_TOKEN_FILE)
    finally:
        temporary.unlink(missing_ok=True)

    return Response(
        content=__import__("json").dumps({"token": token}),
        media_type="application/json",
        headers={"Cache-Control": "no-store"}
    )


def usb_control_busy():
    with state_lock:
        return backup_state["running"]


@app.post("/api/usb/mount")
def mount_usb_drive(auth=Depends(authenticated)):
    from usb_backup import verify_mount

    with state_lock:
        if backup_state["running"]:
            raise HTTPException(409, "Backup is running")

        config = load_config()
        if not config.get("usb_device") or not config.get("usb_volume_id"):
            raise HTTPException(400, "Complete USB registration first")

        try:
            result = subprocess.run(
                ["/usr/local/bin/sudo", "-n", "/usr/local/sbin/eisy-usb-control", "mount"],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "USB mount operation timed out")

        if result.returncode:
            raise HTTPException(
                400, result.stderr.strip() or result.stdout.strip()
            )

        try:
            verify_mount(config["usb_device"], config["usb_volume_id"])
        except (RuntimeError, OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    return {"message": "Registered USB drive mounted successfully."}


@app.post("/api/usb/unmount")
def unmount_usb_drive(auth=Depends(authenticated)):
    with state_lock:
        if backup_state["running"]:
            raise HTTPException(409, "Backup is running")

        result = subprocess.run(
            ["/usr/local/bin/sudo", "-n", "/usr/local/sbin/eisy-usb-control", "unmount"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise HTTPException(
                400, result.stderr.strip() or result.stdout.strip()
            )

        config = load_config()
        config["usb_enabled"] = False
        save_config(config)

    return {"message": "USB drive safely unmounted. You may remove it."}


@app.post("/api/usb/unregister")
def unregister_usb_drive(auth=Depends(authenticated)):
    with state_lock:
        if backup_state["running"]:
            raise HTTPException(409, "Backup is running")

        result = subprocess.run(
            ["/usr/local/bin/sudo", "-n", "/usr/local/sbin/eisy-usb-control", "unregister"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode:
            raise HTTPException(
                400, result.stderr.strip() or result.stdout.strip()
            )

        config = load_config()
        config["usb_enabled"] = False
        config["usb_device"] = ""
        config["usb_volume_id"] = ""
        save_config(config)

    return {
        "message": "USB drive unregistered. Existing backup files were preserved."
    }


@app.get("/api/usb/backups")
def list_usb_backups(auth=Depends(authenticated)):
    import os
    import re
    from usb_backup import verify_mount

    config = load_config()

    try:
        verify_mount(
            config.get("usb_device", ""),
            config.get("usb_volume_id", ""),
        )
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    directory = Path("/mnt/usbbackup/eisy-backups")

    if not directory.is_dir() or directory.is_symlink():
        raise HTTPException(404, "USB backup directory not found")

    archives = []

    for entry in directory.iterdir():
        if not re.fullmatch(
            r"eisy-backup-\d{4}-\d{2}-\d{2}-\d{6}\.zip",
            entry.name,
        ):
            continue

        if entry.is_symlink() or not entry.is_file():
            continue

        stat = entry.stat()
        archives.append({
            "filename": entry.name,
            "size": stat.st_size,
            "modified": stat.st_mtime,
        })

    archives.sort(key=lambda item: item["filename"], reverse=True)

    usage = os.statvfs(directory)

    return {
        "device": config["usb_device"],
        "volume_id": config["usb_volume_id"],
        "total_bytes": usage.f_blocks * usage.f_frsize,
        "free_bytes": usage.f_bavail * usage.f_frsize,
        "backups": archives,
    }


@app.get("/api/usb/backups/{filename}")
def download_usb_backup(filename: str, auth=Depends(authenticated)):
    import re
    from usb_backup import verify_mount

    if not re.fullmatch(
        r"eisy-backup-\d{4}-\d{2}-\d{2}-\d{6}\.zip",
        filename,
    ):
        raise HTTPException(400, "Invalid backup filename")

    config = load_config()

    try:
        verify_mount(
            config.get("usb_device", ""),
            config.get("usb_volume_id", ""),
        )
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    directory = Path("/mnt/usbbackup/eisy-backups")

    if directory.is_symlink() or not directory.is_dir():
        raise HTTPException(404, "USB backup directory not found")

    path = directory / filename

    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, "USB backup not found")

    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/backup/latest")
def download_latest_backup(auth=Depends(authenticated_nas)):
    import re

    directory = Path(BACKUP_DIRECTORY).resolve()
    candidates = []

    for item in get_history():
        filename = item.get("filename")
        if (
            item.get("status") != "successful"
            or not isinstance(filename, str)
            or not re.fullmatch(
                r"eisy-backup-\d{4}-\d{2}-\d{2}-\d{6}\.zip",
                filename
            )
        ):
            continue

        path = directory / filename
        if path.is_file() and not path.is_symlink():
            candidates.append((filename, path))

    if not candidates:
        raise HTTPException(404, "No successful local backup available")

    filename, path = max(candidates, key=lambda item: item[0])

    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        headers={"Cache-Control": "no-store"}
    )


@app.get("/api/backup/download/{filename}")
def download_backup(filename: str, auth=Depends(authenticated)):
    import re

    if not re.fullmatch(r"eisy-backup-\d{4}-\d{2}-\d{2}-\d{6}\.zip", filename):
        raise HTTPException(404, "Backup not found")

    records = get_history()
    if not any(
        item.get("filename") == filename
        and item.get("status") == "successful"
        for item in records
    ):
        raise HTTPException(404, "Backup not found in successful history")

    directory = Path(BACKUP_DIRECTORY).resolve()
    path = directory / filename

    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "Backup not available locally")

    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        headers={"Cache-Control": "no-store"}
    )


@app.get("/api/history")
def history(auth=Depends(authenticated)):
    import re

    directory = Path(BACKUP_DIRECTORY).resolve()
    records = get_history()

    for record in records:
        filename = record.get("filename")
        available = False

        if isinstance(filename, str) and re.fullmatch(
            r"eisy-backup-\d{4}-\d{2}-\d{2}-\d{6}\.zip", filename
        ):
            path = directory / filename
            available = (
                record.get("status") == "successful"
                and path.is_file()
                and not path.is_symlink()
            )

        record["local_available"] = available

    return records


@app.get("/api/status")
def status(auth=Depends(authenticated)):
    with state_lock:
        return dict(backup_state)


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>eisy Backup</title>
<style>
body{font-family:Arial,sans-serif;background:#f3f5f8;color:#263244;margin:0;padding:24px}
main{max-width:950px;margin:auto}
header{margin-bottom:24px}
h1{margin-bottom:5px}
h2{font-size:18px;margin-top:0}
.subtitle,.note{color:#687586}
.card{background:white;border-radius:10px;padding:22px;margin-bottom:18px;box-shadow:0 2px 8px #0000000b}
label{display:block;font-weight:600;margin:14px 0 6px}
input,select{box-sizing:border-box;width:100%;padding:10px;border:1px solid #ccd3dc;border-radius:6px}
button{background:#2463a6;color:white;border:0;border-radius:6px;padding:11px 18px;cursor:pointer;font-weight:600;margin-top:18px}
button:hover{background:#194d85}
button:disabled{opacity:.5;cursor:not-allowed}
.status{font-weight:600}
.success{color:#25804b}
.error{color:#b42318}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
table{width:100%;border-collapse:collapse;margin-top:12px}
th,td{text-align:left;padding:11px 8px;border-bottom:1px solid #e5e9ef;font-size:14px}
th{color:#687586}
.note{font-size:13px}
.auth{max-width:420px;margin:9vh auto}
.hidden{display:none!important}
.top-actions{float:right}
@media(max-width:600px){.grid{grid-template-columns:1fr}body{padding:12px}.card{padding:16px}table{font-size:12px}}
</style>
</head>
<body>

<div id="authScreen" class="auth card">
<h1>eisy Backup</h1>
<h2 id="authTitle">Loading...</h2>
<p id="authDescription" class="note"></p>

<div id="setupPanel" class="hidden">
<label>Administrator password</label>
<input id="setupPassword" type="password" autocomplete="new-password">
<label>Confirm password</label>
<input id="setupConfirm" type="password" autocomplete="new-password">
<button onclick="setupPassword()">Create password</button>
</div>

<div id="loginPanel" class="hidden">
<label>Username</label>
<input id="loginUsername" value="admin" autocomplete="username">
<label>Password</label>
<input id="loginPassword" type="password" autocomplete="current-password">
<button onclick="login()">Login</button>
<p><a href="#" onclick="showRecovery();return false">Recover password</a></p>
</div>

<div id="recoveryPanel" class="hidden">
<label>Recovery code from SSH</label>
<input id="recoveryCode" autocomplete="off">
<label>New password</label>
<input id="recoveryPassword" type="password" autocomplete="new-password">
<label>Confirm new password</label>
<input id="recoveryConfirm" type="password" autocomplete="new-password">
<button onclick="recoverPassword()">Set new password</button>
<button onclick="showLogin()">Back to login</button>
</div>
<p id="authError" class="error"></p>
</div>

<main id="dashboard" class="hidden">
<header>
<button class="top-actions" onclick="logout()">Logout</button>
<h1>eisy Backup</h1>
<div class="subtitle">Native eisy backups and Google Drive replication</div>
</header>

<div class="card">
<h2>Backup status</h2>
<div id="status" class="status">Checking...</div>
<p id="last" class="note"></p>
<button id="backupButton" onclick="startBackup()">Backup Now</button>
<div id="message" class="note"></div>
</div>

<div class="card">
<div class="card">
<h2>Google Drive Backup</h2>
<label>Enter a Name for Your Google Drive Folder</label>
<input id="google_drive_folder" type="text" value="eisy Backups">
</div>

<div class="card">
<h2>USB Backup Settings</h2>
<div class="grid">

<div style="grid-column:1/-1">
<label style="display:flex;align-items:center;gap:10px">
<input id="usb_enabled" type="checkbox"
       style="width:auto;margin:0">
Enable USB backups
</label>
</div>

<div>
<label>USB backup drive</label>
<select id="usb_device">
<option value="">Loading USB drives...</option>
</select>
<button type="button" onclick="loadUsbDevices()">Refresh USB drives</button>
<button type="button" onclick="registerUsbDrive()">Register USB Drive</button>
<p id="usbRegistered" class="note" style="font-size:17px;font-weight:700;color:inherit;margin:12px 0;">Registered drive: Loading...</p>
<p id="usbRegistrationMessage" class="note" style="font-size:15px;font-weight:600;color:inherit;"></p>
<button type="button" onclick="showUsbSetup()">USB Setup Instructions</button>
<button type="button" onclick="completeUsbRegistration()">Complete Registration</button>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
<button type="button" onclick="mountUsbDrive()">Mount USB Drive</button>
<button type="button" onclick="unmountUsbDrive()">Safely Remove USB Drive</button>
<button type="button" onclick="unregisterUsbDrive()">Unregister USB Drive</button>
</div>
<div>
<label>USB backups to retain</label>
<input id="usb_retention" type="number" min="1" max="100" value="7">
</div>
</div>

</div>
<button onclick="saveConfig()">Save settings</button>
<div id="configMessage" class="note"></div>
</div>

<div class="card">
<h2>USB Backup Storage</h2>
<p class="note">Backups currently stored on the registered USB drive.</p>
<div id="usbStorageStatus" class="note">Loading USB storage...</div>
<button type="button" onclick="loadUsbBackups()">Refresh USB backups</button>
<div id="usbBackupList"></div>
</div>

<div class="card">
<h2>NAS Backup Access</h2>
<p class="note">Allow a NAS or another computer to download the latest successful backup automatically.</p>

<label>Download URL</label>
<input id="nasUrl" type="text" readonly>

<label>API token</label>
<input id="nasToken" type="password" readonly
       placeholder="Click Show Token to display">

<div>
<button type="button" onclick="showNasToken()">Show Token</button>
<button type="button" onclick="copyNasToken()">Copy Token</button>
<button type="button" onclick="regenerateNasToken()">Regenerate Token</button>
</div>

<p id="nasMessage" class="note"></p>
<p class="note">Regenerating the token immediately invalidates the previous token. Update your NAS configuration afterward.</p>
</div>

<div class="card">
<h2>Backup history</h2>
<div id="history">Loading...</div>
</div>

<div class="card">
<h2>Google Drive</h2>
<div id="googleStatus" class="status">Not connected</div>

<button id="googleConnectButton" onclick="connectGoogle()">Connect Google Drive</button>

<div id="googleAuthorization" class="hidden">
<p>Open <a id="googleVerificationLink" href="https://www.google.com/device"
target="_blank" rel="noopener noreferrer">Google device authorization</a>
and enter this code:</p>
<h2 id="googleUserCode"></h2>
</div>

<p id="googleMessage" class="note"></p>
</div>

<div class="card">
<h2>Administrator</h2>
<button onclick="showChangePassword()">Change password</button>
<div id="changePanel" class="hidden">
<label>Current password</label><input id="currentPassword" type="password">
<label>New password</label><input id="newPassword" type="password">
<label>Confirm new password</label><input id="newConfirm" type="password">
<button onclick="changePassword()">Update password</button>
<p id="changeMessage" class="note"></p>
</div>
</div>
</main>

<script>
let heartbeatTimer=null;
let backupPollTimer=null;
let lastObservedBackupRunning=false;
let authenticatedSession=false;
let lastActivity=0;
const IDLE_LIMIT=3600000;

["pointerdown","keydown"].forEach(eventName=>{
 document.addEventListener(eventName,()=>{
  if(authenticatedSession)lastActivity=Date.now();
 },{passive:true});
});

function showAuth(title,description){
 clearNasToken();
 authenticatedSession=false;
 if(backupPollTimer){clearInterval(backupPollTimer);backupPollTimer=null;}
 document.getElementById('authScreen').classList.remove('hidden');
 document.getElementById('dashboard').classList.add('hidden');
 document.getElementById('authTitle').textContent=title;
 document.getElementById('authDescription').textContent=description||'';
 document.getElementById('authError').textContent='';
}

function showPanel(name){
 for(const id of ['setupPanel','loginPanel','recoveryPanel'])
  document.getElementById(id).classList.add('hidden');
 document.getElementById(name).classList.remove('hidden');
}

async function authStatus(){
 try{
  const r=await fetch('/api/auth/status',{credentials:'same-origin',cache:'no-store'});
  const s=await r.json();
  if(s.authenticated){showDashboard();return;}
  if(s.setup_required){
   showAuth('Create administrator password','Choose a password of at least 12 characters.');
   showPanel('setupPanel');
  }else{
   showAuth('Administrator login','Sign in to manage your backups.');
   showPanel('loginPanel');
  }
 }catch(e){
  showAuth('Connection error','Unable to contact the backup server.');
 }
}

function authError(message){
 document.getElementById('authError').textContent=message;
}

async function setupPassword(){
 const password=document.getElementById('setupPassword').value;
 const confirm=document.getElementById('setupConfirm').value;
 if(password.length<12){authError('Password must contain at least 12 characters.');return;}
 if(password!==confirm){authError('Passwords do not match.');return;}
 try{
  const r=await fetch('/api/auth/setup',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({password,confirm})});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Setup failed');
  document.getElementById('setupPassword').value='';
  document.getElementById('setupConfirm').value='';
  showLogin();
  authError('Administrator password created. Please log in.');
 }catch(e){authError(e.message);}
}

async function login(){
 try{
  const r=await fetch('/api/auth/login',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.getElementById('loginUsername').value,password:document.getElementById('loginPassword').value})});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Login failed');
  document.getElementById('loginPassword').value='';
  showDashboard();
 }catch(e){authError(e.message);}
}

function showRecovery(){
 showPanel('recoveryPanel');
 document.getElementById('authTitle').textContent='Password recovery';
 document.getElementById('authDescription').textContent='Enter the temporary recovery code generated over SSH.';
}

function showLogin(){
 showPanel('loginPanel');
 document.getElementById('authTitle').textContent='Administrator login';
 document.getElementById('authDescription').textContent='Sign in to manage your backups.';
 authError('');
}

async function recoverPassword(){
 const code=document.getElementById('recoveryCode').value.trim();
 const password=document.getElementById('recoveryPassword').value;
 const confirm=document.getElementById('recoveryConfirm').value;
 if(password.length<12){authError('Password must contain at least 12 characters.');return;}
 if(password!==confirm){authError('Passwords do not match.');return;}
 try{
  const r=await fetch('/api/auth/recover',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,new_password:password,confirm_password:confirm})});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Recovery failed');
  showLogin();
  authError('Password changed. Please log in.');
 }catch(e){authError(e.message);}
}

async function logout(){
 if(heartbeatTimer)clearInterval(heartbeatTimer);
 await fetch('/api/auth/logout',{method:'POST',credentials:'same-origin',cache:'no-store'});
 authenticatedSession=false;
 authStatus();
}

function showDashboard(){
 authenticatedSession=true;
 lastActivity=Date.now();
 document.getElementById('authScreen').classList.add('hidden');
 document.getElementById('dashboard').classList.remove('hidden');
 loadConfig();
 loadGoogleConnection();
 refreshAll();
 if(backupPollTimer)clearInterval(backupPollTimer);
 lastObservedBackupRunning=false;
 backupPollTimer=setInterval(()=>{
  if(authenticatedSession)refreshStatus();
 },3000);
 if(heartbeatTimer)clearInterval(heartbeatTimer);
 heartbeatTimer=setInterval(async()=>{
  if(!authenticatedSession || Date.now()-lastActivity>=60000)return;
  const r=await fetch('/api/auth/heartbeat',{method:'POST',credentials:'same-origin',cache:'no-store'});
  if(r.status===401){authenticatedSession=false;clearInterval(heartbeatTimer);authStatus();}
 },30000);
}

setInterval(()=>{
 if(authenticatedSession && Date.now()-lastActivity>=IDLE_LIMIT)logout();
},30000);


function showChangePassword(){
 document.getElementById('changePanel').classList.toggle('hidden');
}

async function changePassword(){
 const current_password=document.getElementById('currentPassword').value;
 const new_password=document.getElementById('newPassword').value;
 const confirm=document.getElementById('newConfirm').value;
 if(new_password.length<12){document.getElementById('changeMessage').textContent='Password must contain at least 12 characters.';return;}
 try{
  const r=await fetch('/api/auth/change',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password,new_password,confirm_password:confirm})});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Password change failed');
  if(heartbeatTimer)clearInterval(heartbeatTimer);
  authStatus();
  authError('Password changed. Please log in again.');
 }catch(e){document.getElementById('changeMessage').textContent=e.message;}
}

async function apiFetch(url,options={}){
 const headers={...(options.headers||{})};
 if(authenticatedSession && Date.now()-lastActivity<60000){
  headers['X-User-Activity']='1';
 }
 const r=await fetch(url,{...options,headers,credentials:'same-origin',cache:'no-store'});
 if(r.status===401){authStatus();throw new Error('Session expired. Please log in again.');}
 return r;
}


let googlePollTimer=null;

async function connectGoogle(){
 const message=document.getElementById('googleMessage');
 const button=document.getElementById('googleConnectButton');
 button.disabled=true;
 message.textContent='Requesting Google authorization...';

 try{
  const r=await apiFetch('/api/google/start',{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Unable to start authorization');

  document.getElementById('googleVerificationLink').href=d.verification_url;
  document.getElementById('googleUserCode').textContent=d.user_code;
  document.getElementById('googleAuthorization').classList.remove('hidden');
  document.getElementById('googleStatus').textContent='Waiting for authorization';
  message.textContent='Enter the code at Google, then return here.';

  if(googlePollTimer)clearInterval(googlePollTimer);
  googlePollTimer=setInterval(checkGoogleAuthorization,Math.max(5,d.interval)*1000);
 }catch(e){
  message.textContent=e.message;
  button.disabled=false;
 }
}

async function checkGoogleAuthorization(){
 if(!authenticatedSession){
  clearInterval(googlePollTimer);
  googlePollTimer=null;
  return;
 }

 try{
  const r=await apiFetch('/api/google/status');
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Authorization check failed');

  if(d.status==='authorized'){
   clearInterval(googlePollTimer);
   googlePollTimer=null;
   document.getElementById('googleStatus').textContent='Connected';
   document.getElementById('googleAuthorization').classList.add('hidden');
   document.getElementById('googleMessage').textContent='Google Drive connected successfully.';
   document.getElementById('googleConnectButton').disabled=false;
  }else if(['expired','expired_token','access_denied','idle'].includes(d.status)){
   clearInterval(googlePollTimer);
   googlePollTimer=null;
   document.getElementById('googleMessage').textContent='Authorization ended: '+d.status;
   document.getElementById('googleConnectButton').disabled=false;
  }else if(d.status==='error'){
   document.getElementById('googleMessage').textContent='Google authorization check failed. Retrying...';
  }
 }catch(e){
  document.getElementById('googleMessage').textContent=e.message;
 }
}

async function loadGoogleConnection(){
 try{
  const r=await apiFetch('/api/google/connection');
  if(!r.ok)return;
  const d=await r.json();
  document.getElementById('googleStatus').textContent=
   d.connected?'Connected':'Not connected';
 }catch(e){
  document.getElementById('googleStatus').textContent='Connection status unavailable';
 }
}

async function registerUsbDrive(){
 const partition=document.getElementById('usb_device').value;
 const message=document.getElementById('usbRegistrationMessage');

 if(!partition){
  message.textContent='Select a USB drive first.';
  return;
 }

 if(!confirm(
  `Register ${partition} as the USB backup drive? `+
  'This will replace any previous registration and disable USB backups until setup is completed.'
 ))return;

 try{
  const r=await apiFetch('/api/usb/register',{
   method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({partition})
  });
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Registration failed');

  document.getElementById('usbRegistered').textContent=
   `Selected drive: ${d.partition} — setup pending`;
  document.getElementById('usb_enabled').checked=false;
  message.textContent=d.message;

  // Immediately display the SSH command after registration.
  await showUsbSetup();

 }catch(e){
  message.textContent=e.message;
 }
}

async function mountUsbDrive(){
 if(!confirm('Mount the registered USB backup drive?'))return;

 const message=document.getElementById('usbRegistrationMessage');

 try{
  const r=await apiFetch('/api/usb/mount',{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Mount failed');

  message.textContent=d.message;
  await loadConfig();
  await loadUsbBackups();
 }catch(e){
  message.textContent=e.message;
 }
}

async function unmountUsbDrive(){
 if(!confirm(
  'Safely unmount the USB drive? USB backups will be disabled until it is mounted again.'
 ))return;

 const message=document.getElementById('usbRegistrationMessage');

 try{
  const r=await apiFetch('/api/usb/unmount',{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Unmount failed');

  document.getElementById('usb_enabled').checked=false;
  message.textContent=d.message;
  document.getElementById('usbStorageStatus').textContent=
   'USB drive safely unmounted.';
  document.getElementById('usbBackupList').replaceChildren();
 }catch(e){
  message.textContent=e.message;
 }
}

async function unregisterUsbDrive(){
 if(!confirm(
  'Unregister this USB drive? It must be safely unmounted first. '+
  'Existing backup archives will NOT be deleted.'
 ))return;

 const message=document.getElementById('usbRegistrationMessage');

 try{
  const r=await apiFetch('/api/usb/unregister',{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Unregister failed');

  document.getElementById('usb_enabled').checked=false;
  document.getElementById('usbRegistered').textContent=
   'No USB drive registered';
  document.getElementById('usbStorageStatus').textContent=
   'No USB drive registered.';
  document.getElementById('usbBackupList').replaceChildren();

  message.textContent=d.message;
 }catch(e){
  message.textContent=e.message;
 }
}

async function showUsbSetup(){
 try{
  const r=await apiFetch('/api/usb/setup-command');
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Unable to get setup command');
  window.prompt(
   'STEP 1: Copy and run this command over SSH.\n\n' +
   'STEP 2: After the command completes successfully, return to this USB Backup page and click Complete Registration.\n\n' +
   'Your USB drive will then be verified and ready to use.',
   d.command
  );
 }catch(e){
  document.getElementById('usbRegistrationMessage').textContent=e.message;
 }
}

async function completeUsbRegistration(){
 const message=document.getElementById('usbRegistrationMessage');
 try{
  const r=await apiFetch('/api/usb/complete',{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Registration incomplete');
  document.getElementById('usbRegistered').textContent=
   `Registered drive: ${d.partition} — ${d.volume_id}`;
  document.getElementById('usb_enabled').checked=false;
  message.textContent=d.message;
 }catch(e){
  message.textContent=e.message;
 }
}

async function loadUsbBackups(){
 const status=document.getElementById('usbStorageStatus');
 const list=document.getElementById('usbBackupList');
 if(!status||!list)return;

 status.textContent='Loading USB backups...';
 list.replaceChildren();

 try{
  const r=await apiFetch('/api/usb/backups');
  const d=await r.json();

  if(!r.ok)throw new Error(d.detail||'Unable to read USB storage');

  const mb=n=>(n/1048576).toFixed(1)+' MB';

  status.textContent=
   `${d.device} — ${d.volume_id} | `+
   `${d.backups.length} backups | `+
   `${mb(d.free_bytes)} free of ${mb(d.total_bytes)}`;

  if(!d.backups.length){
   list.textContent='No USB backups found.';
   return;
  }

  const table=document.createElement('table');
  const header=document.createElement('tr');

  for(const name of ['Backup file','Size','Download']){
   const th=document.createElement('th');
   th.textContent=name;
   header.appendChild(th);
  }
  table.appendChild(header);

  for(const backup of d.backups){
   const tr=document.createElement('tr');

   const filename=document.createElement('td');
   filename.textContent=backup.filename;

   const size=document.createElement('td');
   size.textContent=mb(backup.size);

   const action=document.createElement('td');
   const button=document.createElement('button');
   button.type='button';
   button.textContent='Download';
   button.onclick=()=>{
    window.location.href=
     '/api/usb/backups/'+encodeURIComponent(backup.filename);
   };

   action.appendChild(button);
   tr.append(filename,size,action);
   table.appendChild(tr);
  }

  list.appendChild(table);

 }catch(e){
  status.textContent=e.message;
 }
}

async function loadUsbDevices(){
 const select=document.getElementById('usb_device');
 if(!select)return;
 const previous=select.value;
 select.replaceChildren(new Option('Loading USB drives...', ''));
 try{
  const r=await apiFetch('/api/usb/devices');
  if(!r.ok)throw new Error('USB discovery failed');
  const data=await r.json();
  select.replaceChildren(new Option('Select USB drive', ''));
  for(const device of data.devices){
   const label=`${device.partition} — ${device.size_mb} MB`;
   select.add(new Option(label,device.partition));
  }
  if([...select.options].some(o=>o.value===previous))
   select.value=previous;
  if(!data.devices.length)
   select.options[0].text='No USB drives detected';
 }catch(e){
  select.replaceChildren(new Option('Unable to detect USB drives', ''));
 }
}

async function loadConfig(){
 await loadUsbDevices();
 try{
  const r=await apiFetch('/api/config');if(!r.ok)return;
  const c=await r.json();
  document.getElementById('google_drive_folder').value=c.google_drive_folder;
  document.getElementById('usb_enabled').checked=!!c.usb_enabled;
  document.getElementById('usb_retention').value=c.usb_retention;
  document.getElementById('usbRegistered').textContent=
   c.usb_volume_id
    ? `Registered drive: ${c.usb_device} — ${c.usb_volume_id}`
    : (c.usb_device
       ? `Selected drive: ${c.usb_device} — setup pending`
       : 'No USB drive registered');
  const usbSelect=document.getElementById('usb_device');
  if([...usbSelect.options].some(o=>o.value===c.usb_device))
   usbSelect.value=c.usb_device;
 }catch(e){}
 await loadUsbBackups();
}

async function saveConfig(){
 const c={
  google_drive_folder:document.getElementById('google_drive_folder').value.trim()||'eisy Backups',
  usb_enabled:document.getElementById('usb_enabled').checked,
  usb_retention:Number(document.getElementById('usb_retention').value)
 };
 try{
  const r=await apiFetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(c)});
  document.getElementById('configMessage').textContent=r.ok?'Settings saved.':'Unable to save settings.';
 }catch(e){document.getElementById('configMessage').textContent=e.message;}
}

async function startBackup(){
 const button=document.getElementById('backupButton');
 button.disabled=true;
 document.getElementById('message').textContent='Starting backup...';
 try{
  const r=await apiFetch('/api/backup',{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Unable to start backup');
  document.getElementById('message').textContent=d.message;
 }catch(e){document.getElementById('message').textContent=e.message;}
 await refreshStatus();
}

async function refreshStatus(){
 try{
  const r=await apiFetch('/api/status');const s=await r.json();
  const el=document.getElementById('status');
  if(lastObservedBackupRunning && !s.running){
   await refreshHistory();
  }
  lastObservedBackupRunning=s.running;
  el.textContent=s.status;
  el.className='status '+(s.error?'error':(s.running?'':'success'));
  document.getElementById('last').textContent=s.error?s.error:(s.last_backup?'Last backup: '+s.last_backup.filename:'');
  document.getElementById('backupButton').disabled=s.running;
 }catch(e){}
}

function escapeHtml(value){
 return String(value??'').replace(/[&<>"']/g,c=>({
  '&':'&amp;',
  '<':'&lt;',
  '>':'&gt;',
  '"':'&quot;',
  "'":'&#39;'
 })[c]);
}

async function refreshHistory(){
 try{
  const r=await apiFetch('/api/history');const items=await r.json();
  if(!items.length){document.getElementById('history').textContent='No backups yet.';return;}
  let html='<table><thead><tr><th>Date</th><th>Result</th><th>Filename</th><th>Size</th><th>Cloud</th><th>Download</th></tr></thead><tbody>';
  for(const item of items){
   const date=new Date(item.date).toLocaleString();
   const size=item.size?(item.size/1048576).toFixed(1)+' MB':'—';
   const success=item.status==='successful'||(item.status===undefined&&item.successful);
   const result=success?'<span style="color:#25804b">Success</span>':item.status==='running'?'<span>Running</span>':'<span style="color:#b42318">Failed</span>';
   const filename=escapeHtml(item.filename||'—');
   const download=item.local_available
    ? '<button type="button" class="download-backup" data-filename="'+filename+'">Download</button>'
    : '—';
   html+='<tr><td>'+date+'</td><td>'+result+'</td><td>'+filename+'</td><td>'+size+'</td><td>'+escapeHtml(item.google_drive||'—')+'</td><td>'+download+'</td></tr>';
   if(item.error)html+='<tr><td colspan="6" style="color:#b42318">Error: '+item.error+'</td></tr>';
  }
  html+='</tbody></table>';
  document.getElementById('history').innerHTML=html;
 }catch(e){document.getElementById('history').textContent='Unable to load history.';}
}

async function downloadBackup(filename){
 try{
  const r=await apiFetch('/api/backup/download/'+encodeURIComponent(filename));
  if(!r.ok)throw new Error('Backup download unavailable');
  const blob=await r.blob();
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;
  a.download=filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),60000);
 }catch(e){
  alert(e.message);
 }
}

document.getElementById('history').addEventListener('click',function(event){
 const button=event.target.closest('button.download-backup');
 if(button)downloadBackup(button.dataset.filename);
});


function clearNasToken(){
 const field=document.getElementById('nasToken');
 if(field){
  field.value='';
  field.type='password';
 }
}

async function showNasToken(){
 const field=document.getElementById('nasToken');
 const message=document.getElementById('nasMessage');
 try{
  if(field.value && field.type==='text'){
   clearNasToken();
   message.textContent='Token hidden.';
   return;
  }
  const r=await apiFetch('/api/nas/token',{cache:'no-store'});
  if(!r.ok)throw new Error('Unable to retrieve NAS token');
  const d=await r.json();
  field.value=d.token;
  field.type='text';
  message.textContent='Token displayed. Keep it private.';
 }catch(e){message.textContent=e.message;}
}

async function copyNasToken(){
 const message=document.getElementById('nasMessage');
 try{
  const r=await apiFetch('/api/nas/token',{cache:'no-store'});
  if(!r.ok)throw new Error('Unable to retrieve NAS token');
  const d=await r.json();
  await navigator.clipboard.writeText(d.token);
  message.textContent='Token copied to clipboard.';
 }catch(e){message.textContent=e.message;}
}

async function regenerateNasToken(){
 const message=document.getElementById('nasMessage');
 if(!confirm('Regenerate NAS token? The existing NAS token will immediately stop working.'))return;
 try{
  const r=await apiFetch('/api/nas/token/regenerate',{method:'POST'});
  if(!r.ok)throw new Error('Unable to regenerate NAS token');
  clearNasToken();
  message.textContent='New token generated. Copy it to your NAS configuration.';
 }catch(e){message.textContent=e.message;}
}

async function refreshAll(){await refreshStatus();await refreshHistory();}

document.getElementById('nasUrl').value=location.origin+'/api/backup/latest';

authStatus();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn

    certificates = list(BASE.glob("plugin_*.cert"))

    if len(certificates) != 1:
        raise RuntimeError(
            f"Expected one PG3x certificate, found {len(certificates)}"
        )

    cert = certificates[0]
    key = cert.with_suffix(".key")

    if not key.is_file():
        raise RuntimeError(f"HTTPS key missing: {key}")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8769,
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
        access_log=False
    )
