from core.engine.trading_loop_execution import TradingLoopExecutionMixin
from core.engine.trading_loop_state import TradingLoop as TradingLoopState


class TradingLoop(TradingLoopState, TradingLoopExecutionMixin):
    pass
