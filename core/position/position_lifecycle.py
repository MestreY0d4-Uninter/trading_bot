from core.position.position_entry import PositionEntryMixin
from core.position.position_exit import PositionExitMixin
from core.position.position_state import PositionStateManager


class PositionManager(PositionStateManager, PositionEntryMixin, PositionExitMixin):
    pass
