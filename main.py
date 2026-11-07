#!/usr/bin/env python3

import atexit
import json
import os
import signal
import threading
import time
from pathlib import Path

import udi_interface
from backup import Controller

LOGGER = udi_interface.LOGGER
BASE = Path(__file__).resolve().parent

web_server = None
web_thread = None


def start_web_server(plugin_token=None):
    global web_server, web_thread

    if web_thread is not None and web_thread.is_alive():
        return

    import uvicorn
    from web_server import app, set_eisy_ui_token
    set_eisy_ui_token(plugin_token)

    certificates = list(BASE.glob("plugin_*.cert"))
    if len(certificates) != 1:
        raise RuntimeError(
            f"Expected one PG3x certificate, found {len(certificates)}"
        )

    cert = certificates[0]
    key = cert.with_suffix(".key")

    if not key.is_file():
        raise RuntimeError(f"HTTPS key missing: {key}")

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8768,
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
        access_log=False,
        log_config=None,
    )

    web_server = uvicorn.Server(config)
    web_thread = threading.Thread(
        target=web_server.run,
        name="EisyBackupWeb",
        daemon=True,
    )
    web_thread.start()
    LOGGER.info("eisy Backup HTTPS server thread started")


def stop_web_server():
    global web_server, web_thread

    if web_server is not None:
        web_server.should_exit = True

    if web_thread is not None and web_thread.is_alive():
        web_thread.join(timeout=5)

    web_server = None
    web_thread = None


def handle_sigterm(signum, frame):
    LOGGER.info("eisy Backup: SIGTERM received, shutting down")
    try:
        stop_web_server()
    finally:
        os._exit(0)


signal.signal(signal.SIGTERM, handle_sigterm)
atexit.register(stop_web_server)


if __name__ == "__main__":
    try:
        polyglot = udi_interface.Interface([])
        polyglot.start("1.0.0")

        isy = udi_interface.ISY(polyglot)

        polyglot.updateProfile()

        polyglot.ready()
        Controller(polyglot)

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

        start_web_server(isy._isy_pass if isy.valid else None)

        polyglot.runForever()

    except Exception:
        LOGGER.exception("eisy Backup startup failed")
        raise

    finally:
        stop_web_server()
