# Changelog

## 1.1.1 — Backup History Permissions Fix

- Fixed HTTP 500 errors when creating native eisy-ui backups.
- Granted the eisyui service read/write access to backup history.
- Preserved FreeBSD ACL permissions when replacing backup_history.json.
- Maintained restricted access to backup history files.

## 1.1.0 — NAS REST Backup Access

- Added REST endpoint `/api/backup/latest` to download the newest successful backup available locally.
- Added dedicated bearer-token authentication for automated NAS downloads.
- Added NAS Backup Access section to the HTML interface.
- Added Show Token, Copy Token, and Regenerate Token controls.
- Added secure persistent API token storage.
- Added NAS setup instructions to README and Plugin Guide.
- Preserved existing browser authentication and backup functionality.

## 1.0.0 — Initial Release

- On-demand native eisy backups.
- Google Drive integration.
- Backup history and local downloads.
- Google Drive folder configuration.
- Backup retention management.
- IoX backup status reporting.
