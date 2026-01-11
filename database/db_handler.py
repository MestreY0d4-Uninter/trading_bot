from database.db_maintenance import DatabaseMaintenanceMixin
from database.db_trades import DatabaseTradesMixin


class DatabaseHandler(DatabaseTradesMixin, DatabaseMaintenanceMixin):
    pass
