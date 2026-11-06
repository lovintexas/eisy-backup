#!/usr/bin/env python3

import time
import json
from pathlib import Path
import udi_interface
from backup import Controller

LOGGER = udi_interface.LOGGER

if __name__ == "__main__":
    try:
        polyglot = udi_interface.Interface([])
        polyglot.start("0.1.0")

        isy = udi_interface.ISY(polyglot)

        profile_path = Path(__file__).resolve().parent / "profile.json"
        with profile_path.open(encoding="utf-8") as f:
            polyglot.updateJsonProfile(json.load(f), {"waitResponse": True})
        polyglot.updateProfile()

        Controller(polyglot)
        polyglot.ready()

        LOGGER.info("eisy Backup plugin initialized")

        # Wait for credentials asynchronously.
        for _ in range(30):
            if isy.valid:
                LOGGER.info("eisy Backup: IoX credentials received")
                break
            if isy.unauthorized:
                LOGGER.warning("eisy Backup: IoX access not authorized")
                break
            time.sleep(1)
        else:
            LOGGER.warning("eisy Backup: timed out waiting for IoX credentials")

        polyglot.runForever()

    except Exception:
        LOGGER.exception("eisy Backup startup failed")
        raise
