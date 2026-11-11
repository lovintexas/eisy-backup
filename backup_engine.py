#!/usr/bin/env python3

import http.client
import os
import ssl
import json
import zipfile
import logging
import subprocess
import threading
import shutil

from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("eisy-backup")
EISY_UI_HOST = "127.0.0.1"
EISY_UI_PORT = 443
BACKUP_LOCK = threading.Lock()
DEFAULT_BACKUP_DIRECTORY = str(Path(__file__).resolve().parent / "persist" / "backups")


def grant_eisyui_backup_access():
    """Allow eisy-ui to read persistent plugin files during native backup."""
    root = Path(__file__).resolve().parent / "persist"

    if not root.exists():
        return

    paths = [root, *root.rglob("*")]

    for path in paths:
        if path.is_symlink():
            continue

        permission = "rx" if path.is_dir() else "r"

        subprocess.run(
            ["setfacl", "-m", f"u:eisyui:{permission}:allow", str(path)],
            check=True,
        )


def api_request(path, token, stream=False):
    if not token:
        raise RuntimeError("eisy-ui authentication token unavailable")

    context = ssl._create_unverified_context()
    conn = http.client.HTTPSConnection(
        EISY_UI_HOST,
        EISY_UI_PORT,
        timeout=300,
        context=context
    )

    try:
        conn.request(
            "GET",
            path,
            headers={"Authorization": f"Bearer {token}"}
        )
        response = conn.getresponse()

        if response.status != 200:
            detail = response.read(2000).decode(errors="replace")
            conn.close()
            raise RuntimeError(
                f"eisy-ui API error {response.status}: {detail}"
            )

        if stream:
            return conn, response

        try:
            result = json.loads(response.read())
        finally:
            conn.close()

        if not result.get("successful"):
            raise RuntimeError(f"eisy-ui returned failure: {result}")

        return result["data"]

    except Exception:
        conn.close()
        raise


HISTORY_FILE = Path(__file__).resolve().parent / "persist" / "backup_history.json"


def save_history(history, directory):
    path = HISTORY_FILE
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(history, indent=2))
    temp.chmod(0o600)
    subprocess.run(["setfacl", "-m", "u:polyglot:r:allow", str(temp)], check=True)
    subprocess.run(["setfacl", "-m", "u:eisyui:rw:allow", str(temp)], check=True)
    temp.replace(path)


def create_backup(directory=DEFAULT_BACKUP_DIRECTORY, retention=7, token=None,
                  google_auth_directory=None, google_folder="eisy Backups",
                  oauth_provider=None, google_enabled=False):
    """Create and verify a local backup, optionally uploading to Google Drive."""
    import google_drive
    if not BACKUP_LOCK.acquire(blocking=False):
        raise RuntimeError("A backup is already running")
    destination = Path(DEFAULT_BACKUP_DIRECTORY)  # Never accept user-controlled backup locations.
    timestamp = datetime.now().astimezone()
    filename = "eisy-backup-" + timestamp.strftime("%Y-%m-%d-%H%M%S") + ".zip"
    record = {"filename": filename, "date": timestamp.isoformat(),
              "started": timestamp.isoformat(), "completed": None,
              "size": 0, "path": None, "status": "running",
              "successful": False, "error": None,
              "google_drive": "Pending" if google_enabled else "Disabled"}
    history = []
    partial = destination / (filename + ".part")
    archive_path = destination / filename
    conn = None
    uploaded = False
    try:
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination.chmod(0o700)
        if HISTORY_FILE.exists():
            try:
                history = json.loads(HISTORY_FILE.read_text())
            except Exception:
                LOGGER.exception("Unable to read backup history")
        history.insert(0, record.copy())
        save_history(history, destination)
        grant_eisyui_backup_access()

        # Remove previous local backups before creating the native backup.
        # Otherwise they become embedded in the new backup ZIP.
        for old_backup in destination.glob("eisy-backup-*.zip"):
            LOGGER.info("Removing previous local backup: %s", old_backup.name)
            old_backup.unlink()

        result = api_request("/api/system/backup/create", token)
        metadata = result["meta"]
        conn, response = api_request(result["downloadPath"], token, stream=True)
        with partial.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        conn.close()
        conn = None
        if partial.stat().st_size == 0:
            raise RuntimeError("Downloaded backup is empty")
        with zipfile.ZipFile(partial, "r") as archive:
            bad_file = archive.testzip()
            if bad_file:
                raise RuntimeError(f"ZIP integrity failure: {bad_file}")
        partial.replace(archive_path)
        record["size"] = archive_path.stat().st_size
        record.update({
            "status": "successful",
            "successful": True,
            "completed": datetime.now().astimezone().isoformat(),
            "metadata": metadata,
            "error": None,
        })

        if google_enabled:
            access_token = None
            try:
                if oauth_provider is not None:
                    access_token = oauth_provider.getAccessToken()

                if not access_token:
                    record["google_drive"] = "Skipped"
                    LOGGER.warning(
                        "Google Drive upload skipped: PG3x authentication unavailable"
                    )
                else:
                    folder_id = google_drive.ensure_backup_folder(
                        access_token, google_folder
                    )
                    cloud_file = google_drive.upload_backup(
                        archive_path, access_token, folder_id
                    )
                    uploaded = True
                    record["google_drive"] = "Uploaded"
                    record["google_file_id"] = cloud_file["id"]

                    try:
                        google_drive.enforce_retention(
                            access_token, folder_id, retention
                        )
                    except Exception as exc:
                        LOGGER.exception("Google Drive retention failed")
                        record["retention_warning"] = str(exc)

            except Exception as exc:
                LOGGER.exception("Google Drive upload failed")
                record["google_drive"] = "Failed"
                record["google_error"] = str(exc)

        history[0] = record.copy()
        save_history(history[:100], destination)
        return record
    except Exception as exc:
        LOGGER.exception("Backup/upload failed")
        record.update({"status": "failed", "successful": False,
                       "completed": datetime.now().astimezone().isoformat(),
                       "error": str(exc),
                       "google_drive": record.get("google_drive", "Disabled")})
        if history:
            history[0] = record.copy()
        else:
            history.insert(0, record.copy())
        try:
            save_history(history[:100], destination)
        except Exception:
            LOGGER.exception("Unable to save failure history")
        raise
    finally:
        if conn:
            conn.close()
        for item in (partial,):
            try:
                item.unlink(missing_ok=True)
            except Exception:
                LOGGER.exception("Unable to remove temporary backup %s", item)
        BACKUP_LOCK.release()


def get_history(directory=DEFAULT_BACKUP_DIRECTORY):
    history_file = HISTORY_FILE

    if not history_file.exists():
        return []

    try:
        return json.loads(history_file.read_text())
    except Exception:
        LOGGER.exception("Unable to read backup history")
        return []
