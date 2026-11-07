# eisy Backup — Privacy Policy

Last updated: November 7, 2026

## Overview

eisy Backup is a Universal Devices plugin that creates backups of an eisy system and uploads them to the user's own Google Drive account.

The plugin operates locally on the user's eisy device and does not require a third-party backup service.

## Google Drive Access

eisy Backup requests the Google Drive `drive.file` permission to:

- Create a folder for eisy backups.
- Upload backup archives to the user's Google Drive.
- Verify uploaded backup files.
- List and delete backup files managed by the plugin according to the user's retention settings.

The plugin does not request permission to access all files in the user's Google Drive.

## Data Storage

Backup archives are stored in the Google Drive account authorized by the user.

Google OAuth authorization tokens are stored locally on the user's eisy device, outside the plugin's native backup data directory.

Temporary backup archives are removed from the eisy after processing.

## Data Sharing

eisy Backup does not intentionally transmit backup contents or Google authorization tokens to the plugin developer.

Backup files are transmitted directly from the user's eisy device to Google's services.

The plugin does not sell user data or use Google Drive information for advertising.

## Security

Connections to Google services use HTTPS.

The plugin uses Google's OAuth authorization process. Users authorize access through Google without providing their Google account passwords to the plugin.

## Revoking Access

Users can revoke eisy Backup's access through their Google Account settings:

https://myaccount.google.com/connections

Revoking access prevents further Google Drive operations until authorization is restored.

Previously uploaded backup files remain in Google Drive unless separately deleted.

## Changes

This policy may be updated as the plugin evolves. Changes will be published in this GitHub repository.
