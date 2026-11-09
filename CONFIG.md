# eisy Backup — Configuration

eisy Backup provides on-demand system backups, local backup history, Google Drive uploads, and authenticated NAS downloads.

## Open the Configuration Interface

The plugin's web configuration interface is available at:

**https://YOUR-EISY-IP:8768**

Replace `YOUR-EISY-IP` with your eisy's IP address.

Use the administrator password configured during initial setup.

## Backup Options

- **Local backups:** Store backups on the eisy with configurable retention.
- **Google Drive:** Authorize your Google account and configure cloud backup storage.
- **NAS downloads:** Generate an API token for authenticated backup downloads to a NAS.
- **IoX programs:** Trigger backups using the Backup node's command.

Backups are initiated on demand. Automatic scheduling is not built into the plugin.

## Documentation

[Complete Configuration Guide](https://lovintexas.github.io/eisy-backup/)

## Security

Keep administrator passwords, Google authorization credentials, and NAS API tokens private.
