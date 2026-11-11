"""PG3x-managed Google OAuth for eisy Backup."""

import logging
from udi_interface import OAuth, LOGGER


class OAuthSecretFilter(logging.Filter):
    """Prevent sensitive OAuth library messages from reaching logs."""

    def filter(self, record):
        # Suppress all messages originating from the OAuth library.
        # Its debug and info messages may contain credentials or tokens.
        return not (
            record.name == "udi_interface"
            and record.module == "oauth"
        )


def install_oauth_log_filter():
    """Filter sensitive messages at the logging handler."""
    for handler in LOGGER.handlers:
        if not any(
            isinstance(f, OAuthSecretFilter)
            for f in handler.filters
        ):
            handler.addFilter(OAuthSecretFilter())


class BackupOAuth(OAuth):
    """Google OAuth adapter using PG3x-managed credentials."""

    def shutdown(self):
        timer = getattr(self, "_refreshTimer", None)
        if timer is not None:
            timer.cancel()
            self._refreshTimer = None
