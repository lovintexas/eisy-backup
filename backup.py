import udi_interface

LOGGER = udi_interface.LOGGER


class Controller(udi_interface.Node):
    id = 'controller'
    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 25, 'name': 'Backup Status'}
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

    commands = {
        'QUERY': query,
        'BACKUP': backup_now
    }
