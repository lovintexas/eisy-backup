#!/usr/bin/env python3

import atexit
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import udi_interface
from backup import Controller

LOGGER = udi_interface.LOGGER
BASE = Path(__file__).resolve().parent

web_process = None
web_stopping = threading.Event()


def start_web_server():
    global web_process

    if web_process is not None and web_process.poll() is None:
        return

    try:
        web_process = subprocess.Popen(
            [sys.executable, str(BASE / "web_server.py")],
            cwd=str(BASE),
            stdin=subprocess.DEVNULL,
            stdout=open("/var/eisy-backups/web_server.log", "a"),
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )
        LOGGER.info(
            "eisy Backup HTTPS server started (PID %s)",
            web_process.pid
        )
    except Exception:
        LOGGER.exception("Unable to start eisy Backup HTTPS server")
        web_process = None


def stop_web_server():
    global web_process

    web_stopping.set()

    if web_process is None:
        return

    if web_process.poll() is None:
        web_process.terminate()
        try:
            web_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            web_process.kill()
            web_process.wait()

    web_process = None


def monitor_web_server():
    while not web_stopping.wait(15):
        if web_process is None or web_process.poll() is not None:
            LOGGER.warning("eisy Backup HTTPS server stopped; restarting")
            start_web_server()


atexit.register(stop_web_server)


if __name__ == "__main__":
    try:
        polyglot = udi_interface.Interface([])
        polyglot.start("0.1.0")

        isy = udi_interface.ISY(polyglot)

        profile_path = BASE / "profile.json"
        with profile_path.open(encoding="utf-8") as f:
            polyglot.updateJsonProfile(
                json.load(f),
                {"waitResponse": True}
            )

        polyglot.updateProfile()

        Controller(polyglot)
        polyglot.ready()

        LOGGER.info("eisy Backup plugin initialized")

        for _ in range(30):
            if isy.valid:
                LOGGER.info("eisy Backup: IoX credentials received")
                break

            if isy.unauthorized:
                LOGGER.warning("eisy Backup: IoX access not authorized")
                break

            time.sleep(1)
        else:
            LOGGER.warning(
                "eisy Backup: timed out waiting for IoX credentials"
            )

        start_web_server()

        threading.Thread(
            target=monitor_web_server,
            name="BackupWebMonitor",
            daemon=True
        ).start()

        polyglot.runForever()

    except Exception:
        LOGGER.exception("eisy Backup startup failed")
        raise

    finally:
        stop_web_server()
