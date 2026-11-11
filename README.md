# eisy Backup

**[Configuration Guide — setup, Google Drive authorization, and required API access](https://lovintexas.github.io/eisy-backup/)**


**Version 1.2.0**

## Overview

eisy Backup is a Universal Devices plugin for creating native eisy system backups and uploading them directly to your own Google Drive account.

eisy Backup provides on-demand and scheduled native backups, Google Drive integration, optional USB storage, backup history, retention management, local downloads, and IoX status reporting.

## Requirements

- Universal Devices eisy with a supported plugin environment.
- Internet access for Google Drive uploads and authorization.
- An existing Google account.
- Allow Unrestricted ISY Access enabled for the plugin.

## Installation

**Important:** After a fresh installation or uninstall/reinstall, enable
**Allow Unrestricted ISY Access by Node Server** in the plugin's PG3x
configuration and restart the plugin. This authorization is required to
create native eisy backups.

If the plugin is uninstalled and reinstalled, an existing USB mount may
retain permissions associated with the previous plugin user. If USB storage
reports a permission error, safely unmount and remount the registered drive
using the installed USB control helper, then verify access.

1. Install eisy Backup from the Universal Devices plugin store.
2. Enable Allow Unrestricted ISY Access when required.
3. Open the plugin's HTTPS web interface at `https://EISY-IP:8768`, replacing `EISY-IP` with your eisy's IP address.
4. Create an administrator password on first access.
5. Log in with username `admin` and your password.

## Google Drive Setup

1. Select **Connect Google Drive** in the web interface.
2. Follow the displayed Google authorization link.
3. Enter the displayed verification code and approve the requested access.
4. Return to the web interface and verify that Google Drive is connected.
5. Optionally change the Google Drive backup folder and save settings.

The plugin requests the Google Drive `drive.file` scope, which limits access to files created or managed by the application rather than unrestricted access to your entire Drive.

## Creating a Backup

1. Open the plugin's web interface.
2. Click **Backup Now**.
3. Wait for the backup operation to complete.
4. Confirm the result in Backup History and the IoX Admin Console.

Backups can be initiated on demand or configured to run automatically using the scheduling settings in the web interface.

## Backup History and Downloads

The Backup History section displays previous backup attempts and their results.

When a successful backup ZIP is still available locally, a **Download** button appears beside that entry. Click it to save the ZIP to your computer.

The plugin retains only the newest local backup archive during normal operation. Older backups must be retrieved from Google Drive.

## Restoring a Backup

1. Obtain the required backup ZIP from the local Download button or Google Drive.
2. Open the Universal Devices eisy-ui backup/restore interface.
3. Use the existing restore function and select the downloaded ZIP.
4. Follow the restore instructions presented by eisy-ui.

The eisy Backup plugin creates and downloads backup archives; it does not directly execute system restores.

## Security and Privacy

- The web interface uses HTTPS and administrator authentication.
- Google authorization uses OAuth; your Google password is not entered into the plugin.
- Backup files are uploaded directly from your eisy to your Google Drive account.
- Google authorization tokens are stored locally.
- Google access can be revoked through your Google Account settings.
- See `PRIVACY.md` for the full privacy policy.

Protect downloaded backup ZIP files because they may contain sensitive system configuration.

## USB Backup

Version 1.2.0 adds optional USB backup storage alongside Google Drive.

### Initial USB Drive Registration

1. Connect a supported FAT-formatted USB drive to the eisy.
2. Open the eisy Backup HTTPS web interface.
3. Under USB Backup Settings, select the drive from the USB device list.
4. Click **Register USB Drive**.
5. A dialog displays the SSH command required to install and configure the USB mount service.
6. Copy the command and execute it in an SSH session connected to the eisy.
7. Return to the web interface and click **Complete Registration**.
8. Verify that the registered drive is recognized.
9. Enable USB backups and save settings.

The setup process verifies the drive's FAT volume ID before installing
the mount service. The service automatically mounts the registered
drive at boot.

The USB backup directory is created automatically at:

`/mnt/usbbackup/eisy-backups/`

The USB drive is not formatted by the registration process. It must
already have a supported FAT filesystem.

### Creating USB Backups

When USB backups are enabled, each successful native eisy backup is
copied to the registered USB drive.

The plugin:

- Verifies the original ZIP archive.
- Copies the archive to a temporary file on the USB drive.
- Verifies the copied ZIP archive.
- Renames the verified copy to its final filename.
- Removes older USB backups according to the USB retention setting.

USB retention is independent of Google Drive and local retention.

USB copy failures are recorded separately and do not change the
success status of the primary backup.

### Viewing and Downloading USB Backups

The **USB Backup Storage** section of the web interface displays:

- Saved backup filenames.
- File sizes.
- Available and total USB storage capacity.
- Download links for individual backup archives.

The USB storage listing is available when the registered drive is
mounted and recognized.

### Safely Removing or Replacing a USB Drive

Before physically disconnecting a drive:

1. Open USB Backup Settings in the web interface.
2. Click **Safely Remove USB Drive**.
3. Confirm the operation.
4. Wait for the interface to report successful removal.
5. Verify that USB backups are disabled before disconnecting the drive.

To unregister a drive, safely unmount it first, then click
**Unregister USB Drive**. Existing backup archives are not deleted.

To replace a drive:

1. Safely remove and unregister the existing drive.
2. Disconnect the old drive and connect the replacement.
3. Refresh the USB device list.
4. Register the replacement drive.
5. Execute the newly displayed SSH setup command.
6. Return to the web interface and click **Complete Registration**.
7. Enable USB backups.

The replacement drive must have a supported FAT filesystem.

### USB Backup IoX Node

The **USB Backup** child node provides:

- **USB Status:** Not Configured, Ready, or Error.
- **USB Mounted:** Whether the selected drive is mounted.
- **USB Writable:** Whether the drive accepts writes.
- **USB Free Space MB:** Available storage space.
- **Last USB Backup Result:** Never Run, Success, Failed, or Disabled.

Use **Check USB** to refresh drive status. This command does not
create a backup.

Drive status is also checked periodically. The last USB backup result
is saved in backup history and restored after plugin restart.

### Important Notes

- USB backup storage is optional.
- Supported USB filesystems are FAT/MS-DOS filesystems recognized as
  `msdosfs` by FreeBSD.
- USB backups supplement Google Drive; they do not replace it.
- Keep at least one backup somewhere other than the eisy itself.

## NAS Backup Access

Version 1.1.0 adds authenticated REST downloads for NAS systems.

### Configuration

1. Open the plugin's HTTPS interface on port 8768.
2. Sign in as administrator.
3. Under **NAS Backup Access**, click **Copy Token**.
4. Configure the NAS to send an HTTPS GET request.

**Download URL:**

`https://EISY-IP:8768/api/backup/latest`

**Authentication header:**

`Authorization: Bearer YOUR_TOKEN`

### Example

```bash
curl --fail --show-error \
  --header "Authorization: Bearer YOUR_TOKEN" \
  --output eisy-latest-backup.zip \
  https://EISY-IP:8768/api/backup/latest
```

The endpoint downloads the newest successful backup
still available locally. It does not create a new backup.

Missing or invalid tokens return HTTP 401.
If no successful local backup exists, HTTP 404 is returned.

### Token management

Administrators can show, copy, or regenerate the API token.
Regeneration immediately invalidates the previous token.

Keep the token and downloaded backups secure.
Configure the NAS to trust the plugin's HTTPS certificate.

## Multiple eisy Systems Using One Google Drive Account

Multiple eisy systems can back up to the same Google Drive account.

Each eisy should use a different Google Drive folder to keep its
backups separate.

### Example configuration

| System | Google Drive folder |
|---|---|
| Home eisy | `eisy Backups - Home` |
| Office eisy | `eisy Backups - Office` |

### Setup

1. Install eisy Backup on each system.
2. Authorize each installation with the same Google account.
3. Open the plugin's HTTPS configuration interface on each eisy.
4. Enable Google Drive backups.
5. Enter a unique name in the **Google Drive folder** field.
6. Save the configuration.
7. Create a test backup and verify its destination in Google Drive.

The default folder name is `eisy Backups`.

Each installation independently manages its backup history and
retention settings. **Important:** Each eisy must use a different Google Drive folder.
If two systems use the same folder, the backup retention policy on
either system may delete backups created by the other system.

## Version History

**1.2.0 — USB backup support**

- Added optional USB backup copying and independent retention.
- Added ZIP verification and interrupted-copy recovery.
- Added USB Backup IoX child node with drive status and backup results.
- Added persistent USB backup history and immediate IoX result updates.

**1.1.1 — Backup history permissions fix**

- Fixed HTTP 500 errors in native eisy-ui backups caused by backup history file permissions.
- Preserved eisyui read/write access when backup history is replaced.

**1.1.0 — NAS REST backup downloads**

- Added authenticated REST download of the latest backup.
- Added NAS API token management to the HTML interface.
- Added token regeneration and NAS setup documentation.

**1.0.0 — Initial release**

Native eisy backups, Google Drive integration, backup history, retention, authenticated HTTPS web interface, local ZIP downloads, and IoX status monitoring.
