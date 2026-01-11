from services.bot_lifecycle import BotLifecycleMixin
from services.component_manager import ComponentManagerMixin
from services.signal_handler import SignalHandlerMixin
from services.state_recovery import StateRecoveryMixin


class BotOrchestrator(
    BotLifecycleMixin,
    ComponentManagerMixin,
    SignalHandlerMixin,
    StateRecoveryMixin,
):
    pass
