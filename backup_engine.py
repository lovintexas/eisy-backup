#!/usr/bin/env python3

import http.client
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


def save_history(history, directory):
    path = Path(directory) / "history.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(history, indent=2))
    temp.chmod(0o600)
    subprocess.run(["setfacl", "-m", "u:polyglot:r:allow", str(temp)], check=True)
    temp.replace(path)


def create_backup(directory=DEFAULT_BACKUP_DIRECTORY, retention=7, token=None):
    if not BACKUP_LOCK.acquire(blocking=False):
        raise RuntimeError("A backup is already running")

    destination = Path(directory)
    temp_path = None
    conn = None

    timestamp = datetime.now(ZoneInfo("America/Chicago"))
    filename = "eisy-backup-" + timestamp.strftime("%Y-%m-%d-%H%M%S") + ".zip"

    record = {
        "filename": filename,
        "date": timestamp.isoformat(),
        "started": timestamp.isoformat(),
        "completed": None,
        "size": 0,
        "path": str(destination / filename),
        "status": "running",
        "successful": False,
        "error": None,
        "google_drive": "Pending"
    }

    history = []
    history_file = destination / "history.json"

    try:
        destination.mkdir(parents=True, exist_ok=True)
        destination.chmod(0o750)
        subprocess.run(["setfacl", "-m", "u:polyglot:x:allow", str(destination)], check=True)

        if history_file.exists():
            try:
                history = json.loads(history_file.read_text())
            except Exception:
                LOGGER.exception("Unable to read previous history")
                history = []

        # Record the attempt before doing any backup work.
        history.insert(0, record.copy())
        save_history(history, destination)

        LOGGER.info("Granting eisy-ui access to persistent plugin files")
        grant_eisyui_backup_access()

        LOGGER.info("Requesting native eisy backup")

        result = api_request("/api/system/backup/create", token)
        meta = result["meta"]
        download_path = result["downloadPath"]

        temp_path = destination / (filename + ".part")
        final_path = destination / filename

        LOGGER.info("Downloading native backup: %s", filename)

        conn, response = api_request(download_path, token, stream=True)

        try:
            if response.status != 200:
                raise RuntimeError(f"Download failed: HTTP {response.status}")

            with temp_path.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
        finally:
            conn.close()
            conn = None

        if not temp_path.exists() or temp_path.stat().st_size == 0:
            raise RuntimeError("Downloaded backup is empty")

        LOGGER.info("Verifying backup archive")

        with zipfile.ZipFile(temp_path, "r") as archive:
            bad_file = archive.testzip()
            if bad_file:
                raise RuntimeError(f"ZIP integrity failure: {bad_file}")

        temp_path.replace(final_path)

        record.update({
            "status": "successful",
            "successful": True,
            "completed": datetime.now(ZoneInfo("America/Chicago")).isoformat(),
            "size": final_path.stat().st_size,
            "metadata": meta,
            "error": None
        })

        # Update the existing attempt record.
        history[0] = record.copy()
        save_history(history, destination)

        # Retain the configured number of successful ZIP files.
        keep = max(1, int(retention))
        local_backups = sorted(
            destination.glob("eisy-backup-*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )

        for old_backup in local_backups[keep:]:
            LOGGER.info("Removing expired backup: %s", old_backup.name)
            old_backup.unlink()

        retained = {p.name for p in local_backups[:keep]}

        # Preserve failed attempts; remove only history for expired ZIPs.
        history = [
            item for item in history
            if item.get("status") != "successful"
            or item.get("filename") in retained
        ]

        # Keep the most recent 100 attempts.
        history = history[:100]
        save_history(history, destination)

        LOGGER.info("Backup completed successfully: %s", filename)
        return record

    except Exception as exc:
        LOGGER.exception("Backup creation failed")

        record.update({
            "status": "failed",
            "successful": False,
            "completed": datetime.now(ZoneInfo("America/Chicago")).isoformat(),
            "error": str(exc),
            "google_drive": "Not uploaded"
        })

        # Preserve the failed attempt even when creation or download fails.
        try:
            if history:
                history[0] = record.copy()
            else:
                history.insert(0, record.copy())

            history = history[:100]
            save_history(history, destination)
        except Exception:
            LOGGER.exception("Unable to save failed backup history")

        if temp_path and temp_path.exists():
            temp_path.unlink()

        raise

    finally:
        if conn:
            conn.close()
        BACKUP_LOCK.release()


def get_history(directory=DEFAULT_BACKUP_DIRECTORY):
    history_file = Path(directory) / "history.json"

    if not history_file.exists():
        return []

    try:
        return json.loads(history_file.read_text())
    except Exception:
        LOGGER.exception("Unable to read backup history")
        return []
