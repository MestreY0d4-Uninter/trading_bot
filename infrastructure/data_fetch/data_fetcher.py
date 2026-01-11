from infrastructure.data_fetch.candle_fetcher import CandleFetcherMixin
from infrastructure.data_fetch.data_fetcher_base import (
    DataFetcherBase,
)
from infrastructure.data_fetch.ticker_fetcher import TickerFetcherMixin


class DataFetcher(DataFetcherBase, CandleFetcherMixin, TickerFetcherMixin):
    pass
