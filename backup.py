import udi_interface

LOGGER = udi_interface.LOGGER


class Controller(udi_interface.Node):
    id = 'controller'
    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 25, 'name': 'Backup Status'},
        {'driver': 'GV1', 'value': 7, 'uom': 56, 'name': 'Google Drive Backups to Retain'},
        {'driver': 'GV2', 'value': 0, 'uom': 151, 'name': 'Last Backup'},
        {'driver': 'GV3', 'value': 0, 'uom': 25, 'name': 'Last Backup Result'},
        {'driver': 'GV4', 'value': 0, 'uom': 25, 'name': 'Google Drive Backup Enabled'},
        {'driver': 'GV5', 'value': 0, 'uom': 25, 'name': 'Google Authentication'},
        {'driver': 'GV6', 'value': 0, 'uom': 25, 'name': 'Last Google Backup Result'}
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
        web_server.register_usb_backup_result_callback(
            self.update_usb_backup_result
        )
        self.usb_long_poll_count = 0

    def update_usb_backup_result(self, value):
        self.usb_node.setDriver("GV4", value, report=True)

    def start(self):
        LOGGER.info("eisy Backup controller started")
        import web_server
        config = web_server.load_config()
        self.setDriver('ST', 1)
        self.setDriver('GV1', config['retention'])
        self.setDriver('GV4', int(config.get('google_drive_enabled', False)))
        self.setDriver('GV5', int(web_server.google_oauth_available()))
        self.update_backup_history()
        self.update_google_status()
        self.usb_node.check_usb()
        self.usb_node.update_backup_result()

    def poll(self, poll_type):
        if poll_type != "longPoll":
            return

        self.usb_long_poll_count += 1
        if self.usb_long_poll_count >= 3:
            self.usb_long_poll_count = 0
            self.usb_node.check_usb()
            self.usb_node.update_backup_result()
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
            self.update_google_status()

    def update_google_status(self):
        import web_server
        from backup_engine import get_history

        config = web_server.load_config()
        self.setDriver('GV4', int(config.get('google_drive_enabled', False)))
        self.setDriver('GV5', int(web_server.google_oauth_available()))

        history = get_history()
        result = history[0].get('google_drive') if history else None
        values = {
            'Uploaded': 1,
            'Failed': 2,
            'Upload unverified': 2,
            'Skipped': 3,
            'Disabled': 3,
            'Not authenticated': 3,
        }
        self.setDriver('GV6', values.get(result, 0))

    def set_google(self, command):
        import web_server

        value = int(command['value'])
        if value not in (0, 1):
            LOGGER.warning("Invalid Google backup setting: %s", value)
            return

        config = web_server.load_config()
        config['google_drive_enabled'] = bool(value)
        web_server.save_config(config)
        self.update_google_status()
        LOGGER.info("Google Drive backup %s",
                    "enabled" if value else "disabled")

    def query(self, command=None):
        self.update_backup_history()
        self.update_google_status()
        self.usb_node.check_usb()
        self.usb_node.update_backup_result()
        self.reportDrivers()
        self.usb_node.reportDrivers()

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
        if not 1 <= value <= 60:
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
        'SET_RETENTION': set_retention,
        'SET_GOOGLE': set_google
    }
