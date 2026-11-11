#!/usr/bin/env python3
"""Google Drive OAuth device authorization and backup uploads."""

import requests

DEVICE_AUTH_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


def start_device_authorization(client_id):
    """Request a Google verification URL and user code."""
    response = requests.post(
        DEVICE_AUTH_URL,
        data={
            "client_id": client_id,
            "scope": DRIVE_SCOPE,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def poll_device_authorization(client_id, client_secret, device_code):
    """Exchange an approved device code for Google OAuth tokens."""
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
        timeout=30,
    )

    result = response.json()

    if response.ok:
        return {"status": "authorized", "tokens": result}

    error = result.get("error", "unknown_error")

    if error in ("authorization_pending", "slow_down"):
        return {"status": error}

    if error in ("access_denied", "expired_token"):
        return {"status": error}

    raise RuntimeError(f"Google OAuth error: {error}")


def save_tokens(tokens, persist_directory):
    """Save Google OAuth tokens for subsequent backups."""
    import json
    import os
    from pathlib import Path

    directory = Path(persist_directory)
    directory.mkdir(parents=True, exist_ok=True)

    path = directory / "google_tokens.json"
    temp = directory / "google_tokens.tmp"

    existing = {}
    if path.exists():
        existing = json.loads(path.read_text())

    # Google may omit the refresh token when refreshing access.
    if not tokens.get("refresh_token") and existing.get("refresh_token"):
        tokens = {**tokens, "refresh_token": existing["refresh_token"]}

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(temp, flags, 0o600)

    with os.fdopen(fd, "w") as output:
        json.dump(tokens, output)

    os.chmod(temp, 0o600)
    os.replace(temp, path)


def load_tokens(persist_directory):
    """Read previously saved Google OAuth tokens."""
    import json
    from pathlib import Path

    path = Path(persist_directory) / "google_tokens.json"

    if not path.exists():
        return None

    return json.loads(path.read_text())


def refresh_access_token(client_id, client_secret, persist_directory):
    """Refresh Google access token and preserve the refresh token."""
    tokens = load_tokens(persist_directory)

    if not tokens or not tokens.get("refresh_token"):
        raise RuntimeError("Google Drive authorization required")

    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": tokens["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )

    if not response.ok:
        error = response.json().get("error", "unknown_error")
        raise RuntimeError(f"Google token refresh failed: {error}")

    updated = response.json()

    # Preserve existing credentials and refresh token.
    tokens.update(updated)
    save_tokens(tokens, persist_directory)

    return tokens["access_token"]


def load_client_credentials(persist_directory=None):
    """Load the shared eisy Backup Google OAuth application credentials."""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent / "google_oauth_client.json"

    if not path.is_file():
        raise RuntimeError("Shared Google OAuth configuration missing")

    data = json.loads(path.read_text())

    client_id = data.get("client_id")
    client_secret = data.get("client_secret")

    if not client_id or not client_secret:
        raise RuntimeError("Incomplete shared Google OAuth configuration")

    return client_id, client_secret


# Drive API operations use only the app-created files permitted by drive.file.
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"


def _drive_request(method, url, access_token, **kwargs):
    headers = {"Authorization": f"Bearer {access_token}", **kwargs.pop("headers", {})}
    response = requests.request(method, url, headers=headers, timeout=90, **kwargs)
    if not response.ok:
        raise RuntimeError(f"Google Drive HTTP {response.status_code}: {response.text[:500]}")
    return response


def ensure_backup_folder(access_token, folder_name="eisy Backups"):
    """Find an app-accessible folder or create it automatically."""
    query = "name = '" + folder_name.replace("\\", "\\\\").replace("'", "\\'") + "' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    params = {"q": query, "fields": "nextPageToken,files(id,name)", "page_size": 100}
    response = _drive_request("GET", DRIVE_API, access_token, params=params).json()
    folders = response.get("files", [])
    if folders:
        return folders[0]["id"]
    response = _drive_request("POST", DRIVE_API, access_token,
                              json={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
                              params={"fields": "id"})
    return response.json()["id"]


def upload_backup(path, access_token, folder_id):
    """Upload a ZIP in resumable chunks and verify its size and MD5."""
    import hashlib
    import logging
    import time
    from pathlib import Path

    from requests.exceptions import ConnectionError, Timeout, SSLError

    log = logging.getLogger("eisy-backup-google")
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.md5()

    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)

    metadata = {
        "name": path.name,
        "parents": [folder_id],
        "mimeType": "application/zip",
    }
    response = _drive_request(
        "POST", DRIVE_UPLOAD_API, access_token,
        params={"uploadType": "resumable",
                "fields": "id,name,size,md5Checksum"},
        headers={
            "X-Upload-Content-Type": "application/zip",
            "X-Upload-Content-Length": str(size),
        },
        json=metadata,
    )
    upload_url = response.headers.get("Location")
    if not upload_url or not upload_url.startswith(
            "https://www.googleapis.com/"):
        raise RuntimeError("Google did not return a valid upload URL")

    headers = {"Authorization": f"Bearer {access_token}"}
    chunk_size = 8 * 1024 * 1024
    retry_codes = {408, 429, 500, 502, 503, 504}

    def query_progress():
        response = requests.put(
            upload_url,
            headers={**headers,
                     "Content-Length": "0",
                     "Content-Range": f"bytes */{size}"},
            timeout=90,
        )
        return response

    offset = 0
    failures = 0
    result = None

    with path.open("rb") as source:
        while offset < size:
            source.seek(offset)
            data = source.read(min(chunk_size, size - offset))
            end_byte = offset + len(data) - 1

            try:
                response = requests.put(
                    upload_url,
                    data=data,
                    headers={
                        **headers,
                        "Content-Type": "application/zip",
                        "Content-Length": str(len(data)),
                        "Content-Range":
                            f"bytes {offset}-{end_byte}/{size}",
                    },
                    timeout=180,
                )
            except (SSLError, ConnectionError, Timeout):
                response = None

            if response is not None and response.status_code in (200, 201):
                result = response.json()
                offset = size
                break

            if response is not None and response.status_code == 308:
                acknowledged = response.headers.get("Range")
                new_offset = (
                    int(acknowledged.rsplit("-", 1)[1]) + 1
                    if acknowledged else 0
                )
                if new_offset > offset:
                    failures = 0
                else:
                    failures += 1
                    if failures > 6:
                        raise RuntimeError("Google upload stalled")
                offset = new_offset
                log.info("Google upload progress: %d/%d bytes",
                         offset, size)
                continue

            if (response is not None
                    and response.status_code not in retry_codes):
                raise RuntimeError(
                    f"Google upload failed (HTTP {response.status_code})")

            failures += 1
            if failures > 6:
                raise RuntimeError(
                    "Google upload failed after six recovery attempts")

            time.sleep(min(2 ** failures, 30))

            try:
                status = query_progress()
            except (SSLError, ConnectionError, Timeout):
                continue

            if status.status_code in (200, 201):
                result = status.json()
                offset = size
                break

            if status.status_code == 308:
                previous_offset = offset
                acknowledged = status.headers.get("Range")
                offset = (
                    int(acknowledged.rsplit("-", 1)[1]) + 1
                    if acknowledged else 0
                )
                log.info("Google upload resumed at byte %d", offset)
                if offset > previous_offset:
                    failures = 0
                continue

            if status.status_code not in retry_codes:
                raise RuntimeError(
                    f"Google upload recovery failed (HTTP {status.status_code})")

    if not result:
        raise RuntimeError("Google upload did not return a completed file")

    file_id = result.get("id")
    if not file_id:
        raise RuntimeError("Google upload response omitted file ID")

    remote = _drive_request(
        "GET", f"{DRIVE_API}/{file_id}", access_token,
        params={"fields": "id,name,size,md5Checksum,trashed"},
    ).json()

    if (remote.get("trashed")
            or remote.get("name") != path.name
            or int(remote.get("size", -1)) != size
            or remote.get("md5Checksum", "").lower() != digest.hexdigest()):
        raise RuntimeError("Google Drive upload verification failed")

    log.info("Google upload verified: %d bytes", size)
    return {"id": file_id, "size": size, "md5": digest.hexdigest()}


def enforce_retention(access_token, folder_id, keep):
    """Retain the newest N app-accessible eisy backup ZIPs in Drive."""
    files = []
    page_token = None
    while True:
        params = {"q": f"'{folder_id}' in parents and trashed = false",
                  "fields": "nextPageToken,files(id,name,createdTime)", "page_size": 1000}
        if page_token:
            params["pageToken"] = page_token
        data = _drive_request("GET", DRIVE_API, access_token, params=params).json()
        files.extend(f for f in data.get("files", []) if
                     f.get("name", "").startswith("eisy-backup-") and f.get("name", "").endswith(".zip"))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    files.sort(key=lambda f: f.get("createdTime", ""), reverse=True)
    for item in files[max(1, int(keep)):]:
        _drive_request("DELETE", f"{DRIVE_API}/{item['id']}", access_token)
    return len(files[:max(1, int(keep))])
