import udi_interface

LOGGER = udi_interface.LOGGER


class Controller(udi_interface.Node):
    id = 'controller'
    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 25, 'name': 'Backup Status'},
        {'driver': 'GV1', 'value': 7, 'uom': 56, 'name': 'Google Drive Backups to Retain'}
    ]

    def __init__(self, polyglot):
        super().__init__(polyglot, 'controller', 'controller', 'eisy Backup')
        self.poly = polyglot
        import web_server
        web_server.register_backup_status_callback(self.update_backup_status)
        self.poly.subscribe(self.poly.START, self.start, self.address)
        self.poly.addNode(self)

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

    def update_backup_status(self, value):
        self.setDriver('ST', value)

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
        if not 1 <= value <= 365:
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
