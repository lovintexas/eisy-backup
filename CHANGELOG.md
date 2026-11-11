# Changelog

## 1.2.0 — USB Backup Support

- Added USB Backup Storage listing and direct ZIP downloads in the web interface.
- Added safe USB unmount and unregister operations.
- Added restricted privileged helper for USB mount management.
- Added automatic mounting of the registered USB drive at system startup.
- Added documentation for USB registration and reinstall requirements.

- Added automatic USB backup copying after successful native eisy backups.
- Added USB device selection, enable/disable control, and independent USB retention settings.
- Added ZIP integrity verification before and after copying to USB storage.
- Added temporary-file handling for interrupted USB copies.
- Added USB Backup child node in IoX with mount, write-access, and free-space status.
- Added Check USB command and periodic USB status polling.
- Added Last USB Backup Result status with Success, Failed, Disabled, and Never Run states.
- Persisted USB backup results in backup history and restored them after plugin restart.
- Kept USB copy failures independent of the primary backup success status.

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
