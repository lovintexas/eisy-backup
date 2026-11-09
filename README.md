# eisy Backup

**[Configuration Guide — setup, Google Drive authorization, and required API access](https://lovintexas.github.io/eisy-backup/)**


**Version 1.1.1**

## Overview

eisy Backup is a Universal Devices plugin for creating native eisy system backups and uploading them directly to your own Google Drive account.

Version 1.0.0 provides on-demand backups, Google Drive authorization, backup history, configurable Google Drive folder, retention management, local backup downloads, and IoX status reporting.

## Requirements

- Universal Devices eisy with a supported plugin environment.
- Internet access for Google Drive uploads and authorization.
- An existing Google account.
- Allow Unrestricted ISY Access enabled for the plugin.

## Installation

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

Backups are initiated on demand in version 1.0.0. Automatic scheduling is not included in this release.

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

**1.1.1 — Backup history permissions fix**

- Fixed HTTP 500 errors in native eisy-ui backups caused by backup history file permissions.
- Preserved eisyui read/write access when backup history is replaced.

**1.1.0 — NAS REST backup downloads**

- Added authenticated REST download of the latest backup.
- Added NAS API token management to the HTML interface.
- Added token regeneration and NAS setup documentation.

**1.0.0 — Initial release**

Native eisy backups, Google Drive integration, backup history, retention, authenticated HTTPS web interface, local ZIP downloads, and IoX status monitoring.
