import udi_interface

LOGGER = udi_interface.LOGGER


class Controller(udi_interface.Node):
    id = 'controller'
    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 25, 'name': 'Backup Status'},
        {'driver': 'GV1', 'value': 7, 'uom': 56, 'name': 'Google Drive Backups to Retain'},
        {'driver': 'GV2', 'value': 0, 'uom': 151, 'name': 'Last Backup'},
        {'driver': 'GV3', 'value': 0, 'uom': 25, 'name': 'Last Backup Result'}
    ]

    def __init__(self, polyglot):
        super().__init__(polyglot, 'controller', 'controller', 'eisy Backup')
        self.poly = polyglot
        import web_server
        web_server.register_backup_status_callback(self.update_backup_status)
        self.poly.subscribe(self.poly.START, self.start, self.address)
        self.poly.subscribe(self.poly.POLL, self.poll)
        self.poly.addNode(self)

        from usb_backup_node import USBBackup
        self.usb_node = USBBackup(self.poly)
        self.usb_long_poll_count = 0

    def start(self):
        LOGGER.info("eisy Backup controller started")
        import web_server
        import google_drive

        try:
            tokens = google_drive.load_tokens(
                web_server.GOOGLE_AUTH_DIRECTORY
            ) or {}
            status = 1 if tokens.get("refresh_token") else 0
        except Exception:
            LOGGER.exception("Unable to check Google Drive authorization")
            status = 3

        self.setDriver('ST', status)
        self.setDriver('GV1', web_server.load_config()['retention'])
        self.update_backup_history()
        self.usb_node.check_usb()

    def poll(self, poll_type):
        if poll_type != "longPoll":
            return

        self.usb_long_poll_count += 1
        if self.usb_long_poll_count >= 3:
            self.usb_long_poll_count = 0
            self.usb_node.check_usb()
            self.usb_node.reportDrivers()

    def update_backup_history(self):
        from datetime import datetime
        from backup_engine import get_history

        history = get_history()
        completed = next(
            (item for item in history
             if item.get('completed') and
             item.get('status') in ('successful', 'failed')),
            None
        )

        if completed is None:
            self.setDriver('GV2', 0)
            self.setDriver('GV3', 0)
            return

        try:
            timestamp = int(datetime.fromisoformat(
                completed['completed'].replace('Z', '+00:00')
            ).timestamp())
            self.setDriver('GV2', timestamp)
            self.setDriver(
                'GV3', 1 if completed['status'] == 'successful' else 2
            )
        except (ValueError, TypeError, OverflowError):
            LOGGER.exception("Invalid backup completion timestamp")

    def update_backup_status(self, value):
        self.setDriver('ST', value)
        if value in (1, 3):
            self.update_backup_history()

    def query(self, command=None):
        self.reportDrivers()

    def backup_now(self, command=None):
        import web_server

        try:
            web_server.start_backup_job()
            LOGGER.info("Backup requested from IoX")
        except RuntimeError as exc:
            LOGGER.warning("IoX backup request rejected: %s", exc)

    def set_retention(self, command):
        import web_server

        value = int(command['value'])
        if not 1 <= value <= 30:
            LOGGER.warning("Invalid retention value: %s", value)
            return

        config = web_server.load_config()
        config['retention'] = value
        web_server.save_config(config)
        self.setDriver('GV1', value)
        LOGGER.info("Google Drive retention set to %s backups", value)

    commands = {
        'QUERY': query,
        'BACKUP': backup_now,
        'SET_RETENTION': set_retention
    }
