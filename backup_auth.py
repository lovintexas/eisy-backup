#!/usr/bin/env python3

import hashlib
import hmac
import os
import secrets
import subprocess
import tempfile
import time

from pathlib import Path

ITERATIONS = 300000
RECOVERY_SECONDS = 900
MAX_RECOVERY_ATTEMPTS = 5


def auth_directory(base_dir):
    directory = Path(base_dir) / "persist"
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o750)
    subprocess.run(["setfacl", "-m", "u:polyglot:x:allow", str(directory)], check=True)
    return directory


def credential_path(base_dir):
    return auth_directory(base_dir) / "web_auth.txt"


def recovery_path(base_dir):
    return auth_directory(base_dir) / "web_recovery.txt"


def ensure_backup_access(base_dir):
    """Allow eisy-ui backups to read an existing credential file."""
    path = credential_path(base_dir)
    if not path.is_file():
        return
    subprocess.run(
        ["setfacl", "-m", "u:eisyui:r:allow", str(path)],
        check=True,
    )



def atomic_write(path, content):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".auth-", dir=path.parent)

    try:
        with os.fdopen(fd, "w") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())

        os.chmod(temporary, 0o600)
        subprocess.run(["setfacl", "-m", "u:polyglot:r:allow", temporary], check=True)
        if path.name == "web_auth.txt":
            subprocess.run(
                ["setfacl", "-m", "u:eisyui:r:allow", temporary],
                check=True,
            )

        os.replace(temporary, path)

    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def hash_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain 12-1024 characters")

    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, ITERATIONS
    )

    return salt, digest


def read_credentials(base_dir):
    path = credential_path(base_dir)

    if not path.is_file():
        return None

    parts = path.read_text().strip().split(":")

    if len(parts) != 2:
        raise ValueError("Invalid administrator credential file")

    salt_hex, digest_hex = parts
    return bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)


def save_password(base_dir, password):
    salt, digest = hash_password(password)

    atomic_write(
        credential_path(base_dir),
        f"{salt.hex()}:{digest.hex()}\n"
    )

    return salt, digest


def check_password(password, salt, expected):
    if not isinstance(password, str):
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, ITERATIONS
    )

    return hmac.compare_digest(actual, expected)


def create_initial_password(base_dir, password):
    salt, digest = hash_password(password)
    path = credential_path(base_dir)

    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600
        )
    except FileExistsError:
        raise ValueError("Administrator password already configured")

    with os.fdopen(fd, "w") as output:
        output.write(f"{salt.hex()}:{digest.hex()}\n")
        output.flush()
        os.fsync(output.fileno())

    os.chmod(path, 0o600)
    subprocess.run(["setfacl", "-m", "u:polyglot:r:allow", str(path)], check=True)

    return salt, digest


def change_password(base_dir, current_password, new_password):
    credentials = read_credentials(base_dir)

    if credentials is None:
        raise ValueError("Administrator password not configured")

    if not check_password(current_password, *credentials):
        raise ValueError("Current password is incorrect")

    return save_password(base_dir, new_password)


def create_recovery_code(base_dir):
    if read_credentials(base_dir) is None:
        raise ValueError("No administrator password to recover")

    code = secrets.token_urlsafe(24)
    salt = os.urandom(16)

    digest = hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), salt, ITERATIONS
    )

    expires = int(time.time()) + RECOVERY_SECONDS

    atomic_write(
        recovery_path(base_dir),
        f"{expires}:{salt.hex()}:{digest.hex()}:0\n"
    )

    return code


def complete_recovery(base_dir, code, new_password):
    path = recovery_path(base_dir)

    if not path.is_file():
        raise ValueError("No recovery request is pending")

    parts = path.read_text().strip().split(":")

    if len(parts) != 4:
        raise ValueError("Invalid recovery record")

    expires_text, salt_hex, digest_hex, attempts_text = parts

    expires = int(expires_text)
    attempts = int(attempts_text)

    if time.time() > expires:
        raise ValueError("Recovery code has expired")

    if attempts >= MAX_RECOVERY_ATTEMPTS:
        raise ValueError("Recovery attempt limit reached")

    valid = isinstance(code, str) and check_password(
        code,
        bytes.fromhex(salt_hex),
        bytes.fromhex(digest_hex)
    )

    if not valid:
        attempts += 1

        atomic_write(
            path,
            f"{expires_text}:{salt_hex}:{digest_hex}:{attempts}\n"
        )

        raise ValueError("Invalid recovery code")

    # Validate the new password before changing credentials.
    hash_password(new_password)

    result = save_password(base_dir, new_password)
    path.unlink()

    return result


def main():
    import sys

    if len(sys.argv) != 2 or sys.argv[1] != "reset":
        print("Usage: python3 backup_auth.py reset")
        return 1

    code = create_recovery_code(Path(__file__).resolve().parent)

    print()
    print("eisy Backup password recovery")
    print("-----------------------------")
    print("Recovery code:", code)
    print("Expires in: 15 minutes")
    print()
    print("Enter this code in the web interface.")
    print("The existing password remains valid until recovery completes.")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
