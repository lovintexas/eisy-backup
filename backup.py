import udi_interface

LOGGER = udi_interface.LOGGER


class Controller(udi_interface.Node):
    id = 'controller'
    drivers = [
        {'driver': 'ST', 'value': 0, 'uom': 25, 'name': 'Backup Status'}
    ]

    def __init__(self, polyglot):
        super().__init__(polyglot, 'ctl', 'ctl', 'eisy Backup')
        self.poly = polyglot
        self.poly.subscribe(self.poly.START, self.start, self.address)
        self.poly.addNode(self)

    def start(self):
        LOGGER.info("eisy Backup controller started")
        self.setDriver('ST', 0)

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
