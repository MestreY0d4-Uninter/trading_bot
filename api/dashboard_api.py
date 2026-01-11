import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Any

import dash
import dash_bootstrap_components as dbc
from dash import Input, Output, dcc, html

from database.db_handler import DatabaseHandler
from shared.observability.metrics import metrics
from utils import decimal_math


class TradingDashboard:
    def __init__(self, db_path: str = "data/trading_bot.db", port: int = 8050):
        self.db_path = db_path
        self.port = port
        self.update_count = 0
        self.start_time = datetime.now()
        self._event_loop = None

        assets_path = os.path.join(os.path.dirname(__file__), "assets")

        self.app = dash.Dash(
            __name__,
            external_stylesheets=[dbc.themes.SUPERHERO],
            assets_folder=assets_path,
            title="Trading Bot Dashboard",
            update_title=None,
            suppress_callback_exceptions=False,
        )

        self.db = None

        self._setup_layout()
        self._setup_callbacks()

    def _setup_layout(self):
        self.app.index_string = """
        <!DOCTYPE html>
        <html>
            <head>
                {%metas%}
                <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
                <meta http-equiv="Pragma" content="no-cache">
                <meta http-equiv="Expires" content="0">
                <title>{%title%}</title>
                {%favicon%}
                {%css%}
            </head>
            <body>
                {%app_entry%}
                <footer>
                    {%config%}
                    {%scripts%}
                    {%renderer%}
                </footer>
            </body>
        </html>
        """

        self.app.layout = dbc.Container(
            [
                dcc.Interval(
                    id="interval-component",
                    interval=2000,
                    n_intervals=0,
                ),
                dbc.Row(
                    dbc.Col(
                        html.H1(
                            "🤖 Trading Bot Dashboard",
                            className="text-center mb-4 mt-4",
                        ),
                        width=12,
                    )
                ),
                dbc.Row(
                    [
                        dbc.Col(
                            self._create_metric_card(
                                "Starting Balance", "starting-balance-value", "🏁"
                            ),
                            md=3,
                        ),
                        dbc.Col(
                            self._create_metric_card(
                                "Cash Available", "cash-available-value", "💵"
                            ),
                            md=3,
                        ),
                        dbc.Col(
                            self._create_metric_card(
                                "In Positions", "in-positions-value", "📦"
                            ),
                            md=3,
                        ),
                        dbc.Col(
                            self._create_metric_card(
                                "Total Equity", "total-equity-value", "💰"
                            ),
                            md=3,
                        ),
                    ],
                    className="mb-4",
                ),
                dbc.Row(
                    [
                        dbc.Col(
                            self._create_metric_card(
                                "Daily P&L", "daily-pnl-value", "📊"
                            ),
                            md=3,
                        ),
                        dbc.Col(
                            self._create_metric_card(
                                "Win Rate", "win-rate-value", "🎯"
                            ),
                            md=3,
                        ),
                        dbc.Col(
                            self._create_metric_card(
                                "Open Positions", "open-positions-value", "📈"
                            ),
                            md=3,
                        ),
                        dbc.Col(
                            self._create_metric_card(
                                "Total Trades (24h)", "total-trades-value", "📉"
                            ),
                            md=3,
                        ),
                    ],
                    className="mb-4",
                ),
                dbc.Row(
                    [
                        dbc.Col(
                            self._create_metric_card(
                                "Avg Duration", "avg-duration-value", "⏱️"
                            ),
                            md=6,
                        ),
                        dbc.Col(
                            self._create_metric_card(
                                "Signal Accept Rate", "signal-rate-value", "✅"
                            ),
                            md=6,
                        ),
                    ],
                    className="mb-4",
                ),
                dbc.Row(
                    [
                        dbc.Col(
                            dbc.Card(
                                [
                                    dbc.CardHeader(html.H4("Recent Trades (24h)")),
                                    dbc.CardBody(id="recent-trades-table"),
                                ],
                                className="mb-4",
                                style={"maxHeight": "500px", "overflowY": "auto"},
                            ),
                            md=8,
                        ),
                        dbc.Col(
                            dbc.Card(
                                [
                                    dbc.CardHeader(html.H4("Active Positions")),
                                    dbc.CardBody(id="positions-table"),
                                ],
                                className="mb-4",
                                style={"maxHeight": "500px", "overflowY": "auto"},
                            ),
                            md=4,
                        ),
                    ]
                ),
                dbc.Row(
                    dbc.Col(
                        dbc.Card(
                            [
                                dbc.CardHeader(html.H4("Trading Pairs")),
                                dbc.CardBody(id="trading-pairs-table"),
                            ],
                            className="mb-4",
                            style={"maxHeight": "400px", "overflowY": "auto"},
                        ),
                        md=12,
                    )
                ),
                dbc.Row(
                    dbc.Col(
                        html.Div(
                            [
                                html.Small("Last updated: "),
                                html.Small(
                                    id="last-update-time", className="text-muted"
                                ),
                            ],
                            className="text-center mb-3",
                        ),
                        width=12,
                    )
                ),
            ],
            fluid=True,
            style={"maxWidth": "1400px"},
        )

    def _create_metric_card(self, title: str, value_id: str, icon: str) -> dbc.Card:
        return dbc.Card(
            dbc.CardBody(
                [
                    html.H6(f"{icon} {title}", className="text-muted mb-2"),
                    html.H3(id=value_id, className="mb-0"),
                ]
            ),
            className="text-center shadow-sm",
        )

    def _setup_callbacks(self):
        @self.app.callback(
            [
                Output("starting-balance-value", "children"),
                Output("cash-available-value", "children"),
                Output("in-positions-value", "children"),
                Output("total-equity-value", "children"),
                Output("daily-pnl-value", "children"),
                Output("daily-pnl-value", "style"),
                Output("win-rate-value", "children"),
                Output("open-positions-value", "children"),
                Output("total-trades-value", "children"),
                Output("avg-duration-value", "children"),
                Output("signal-rate-value", "children"),
                Output("positions-table", "children"),
                Output("recent-trades-table", "children"),
                Output("trading-pairs-table", "children"),
                Output("last-update-time", "children"),
            ],
            Input("interval-component", "n_intervals"),
        )
        def update_dashboard(n):
            """
            Atualiza todos os dados do dashboard
            Dash callbacks são sync, então usamos event loop isolado para chamar código async
            """
            try:
                # Try to get running loop (Python 3.14+)
                asyncio.get_running_loop()
                # If we're here, there's already a running loop
                # Use asyncio.run in a thread to avoid nested loop error
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run, self._async_update_dashboard()
                    )
                    result = future.result(timeout=30)
            except RuntimeError:
                # No running loop, safe to use run_until_complete
                if self._event_loop is None or self._event_loop.is_closed():
                    self._event_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(self._event_loop)

                result = self._event_loop.run_until_complete(
                    self._async_update_dashboard()
                )

            self.update_count += 1
            self._print_update_status()
            return result

    async def _async_update_dashboard(self):
        if self.db is None:
            self.db = DatabaseHandler(self.db_path)
            await self.db.initialize()

        balance_data = await self._get_balance_data()
        pnl_data = await self._get_pnl_data()
        win_rate = await self._get_win_rate()
        positions = await self._get_positions()
        positions_table = await self._create_positions_table(positions)
        recent_trades_data = await self._get_recent_trades()
        recent_trades = self._create_recent_trades_table(recent_trades_data)
        trading_pairs = await self._create_trading_pairs_table()

        metrics_summary = await metrics.get_summary()
        trades_metrics = metrics_summary.get("trades", {})
        signals_metrics = metrics_summary.get("signals", {})

        starting_balance_value = f"${balance_data['starting_balance']:,.2f}"
        cash_available_value = f"${balance_data['cash_available']:,.2f}"
        in_positions_value = f"${balance_data['in_positions']:,.2f}"
        total_equity_value = f"${balance_data['total_equity']:,.2f}"

        pnl_value = f"${pnl_data['daily']:,.2f}"
        pnl_style = {"color": "#00ff9d" if pnl_data["daily"] >= 0 else "#ff445e"}

        win_rate_value = f"{win_rate:.1f}%"
        open_positions_value = str(len(positions))

        total_trades_value = str(len(recent_trades_data))

        avg_duration = trades_metrics.get("avg_duration", 0)
        if avg_duration >= 60:
            avg_duration_value = f"{avg_duration / 60:.1f}h"
        else:
            avg_duration_value = f"{avg_duration:.0f}m"

        acceptance_rate = signals_metrics.get("acceptance_rate", 0)
        signal_rate_value = f"{acceptance_rate:.1f}%"

        last_update = datetime.now().strftime("%H:%M:%S")

        return (
            starting_balance_value,
            cash_available_value,
            in_positions_value,
            total_equity_value,
            pnl_value,
            pnl_style,
            win_rate_value,
            open_positions_value,
            total_trades_value,
            avg_duration_value,
            signal_rate_value,
            positions_table,
            recent_trades,
            trading_pairs,
            last_update,
        )

    async def _get_balance_data(self) -> dict:
        try:
            daily_metrics = await self.db.get_daily_metrics()
            starting_balance = 0.0

            if daily_metrics and daily_metrics.get("starting_balance"):
                starting_balance = float(daily_metrics["starting_balance"])
            else:
                return {
                    "starting_balance": 0.0,
                    "cash_available": 0.0,
                    "in_positions": 0.0,
                    "total_equity": 0.0,
                }

            current_balance = float(daily_metrics.get("ending_balance", 0))

            metrics_data = daily_metrics.get("data", {})
            total_equity = float(metrics_data.get("total_equity", current_balance))

            open_trades = await self.db.get_open_trades()
            in_positions = sum(
                float(t.get("entry_price", 0)) * float(t.get("quantity", 0))
                for t in (open_trades or [])
            )

            cash_available = current_balance - in_positions

            return {
                "starting_balance": starting_balance,
                "cash_available": cash_available,
                "in_positions": in_positions,
                "total_equity": total_equity,
            }
        except Exception:
            return {
                "starting_balance": 0.0,
                "cash_available": 0.0,
                "in_positions": 0.0,
                "total_equity": 0.0,
            }

    async def _get_pnl_data(self) -> dict:
        account_metrics = await metrics.get_account_metrics()
        return {"daily": float(account_metrics.get("daily_pnl", 0))}

    async def _get_win_rate(self) -> float:
        performance = await metrics.get_performance_metrics()
        return float(performance.get("win_rate", 0))

    async def _get_positions(self) -> list:
        open_trades = await self.db.get_open_trades()
        return open_trades or []

    async def _get_recent_trades(self) -> list:
        trades = await self.db.get_trade_history(hours=24)
        return (trades or [])[:15]

    async def _create_positions_table(self, positions: list) -> Any:
        if not positions:
            return html.P("No open positions", className="text-muted text-center mt-3")

        symbols = list({pos.get("symbol", "") for pos in positions})

        prices = {}
        if symbols:
            try:
                async with self.db.pool.acquire() as conn:
                    cursor = await conn.cursor()
                    placeholders = ",".join("?" * len(symbols))
                    await cursor.execute(
                        f"""
                        SELECT symbol, price
                        FROM market_data
                        WHERE symbol IN ({placeholders})
                        AND (symbol, timestamp) IN (
                            SELECT symbol, MAX(timestamp)
                            FROM market_data
                            WHERE symbol IN ({placeholders})
                            GROUP BY symbol
                        )
                        """,
                        symbols + symbols,
                    )
                    prices = {row[0]: float(row[1]) for row in await cursor.fetchall()}
            except Exception:
                pass

        table_rows = []

        for pos in positions:
            symbol = pos.get("symbol", "")
            entry_price = float(pos.get("entry_price", 0))
            quantity = float(pos.get("quantity", 0))

            current_price = prices.get(symbol, entry_price)

            if entry_price > 0:
                pnl = float(
                    decimal_math.calculate_pnl(entry_price, current_price, quantity)
                )
                pnl_pct = float(
                    decimal_math.calculate_pnl_percentage(entry_price, current_price)
                )
            else:
                pnl = 0
                pnl_pct = 0

            pnl_class = "pnl-positive" if pnl >= 0 else "pnl-negative"

            table_rows.append(
                html.Tr(
                    [
                        html.Td(symbol, className="fw-bold"),
                        html.Td(f"${entry_price:.2f}", className="text-end"),
                        html.Td(f"${current_price:.2f}", className="text-end"),
                        html.Td(
                            f"${pnl:+.2f} ({pnl_pct:+.2f}%)",
                            className=f"text-end fw-bold {pnl_class}",
                        ),
                    ]
                )
            )

        return html.Table(
            [
                html.Thead(
                    html.Tr(
                        [
                            html.Th("Symbol"),
                            html.Th("Entry", className="text-end"),
                            html.Th("Current", className="text-end"),
                            html.Th("P&L", className="text-end"),
                        ]
                    )
                ),
                html.Tbody(table_rows),
            ],
            className="table table-sm table-dark table-striped table-hover",
        )

    async def _create_trading_pairs_table(self) -> Any:

        configured_pairs = [
            "BTCUSDT",
            "ETHUSDT",
            "BNBUSDT",
            "ADAUSDT",
            "DOTUSDT",
            "SOLUSDT",
            "XRPUSDT",
            "AVAXUSDT",
            "POLUSDT",
            "LINKUSDT",
            "ATOMUSDT",
            "UNIUSDT",
            "ALGOUSDT",
        ]

        open_positions = await self.db.get_open_trades()
        position_symbols = {pos.get("symbol"): pos for pos in (open_positions or [])}

        prices = {}
        try:
            async with self.db.pool.acquire() as conn:
                cursor = await conn.cursor()
                placeholders = ",".join("?" * len(configured_pairs))
                await cursor.execute(
                    f"""
                    SELECT symbol, price, timestamp
                    FROM market_data
                    WHERE symbol IN ({placeholders})
                    AND (symbol, timestamp) IN (
                        SELECT symbol, MAX(timestamp)
                        FROM market_data
                        WHERE symbol IN ({placeholders})
                        GROUP BY symbol
                    )
                    """,
                    configured_pairs + configured_pairs,
                )
                for row in await cursor.fetchall():
                    prices[row[0]] = {
                        "price": float(row[1]),
                        "timestamp": row[2],
                    }
        except Exception:
            pass

        table_rows = []

        for symbol in sorted(configured_pairs):
            price_data = prices.get(symbol, {})
            current_price = price_data.get("price", 0)

            if symbol in position_symbols:
                status_badge = html.Span("🔵 Position", className="badge bg-primary")
                pos = position_symbols[symbol]
                entry_price = float(pos.get("entry_price", 0))
                quantity = float(pos.get("quantity", 0))

                if entry_price > 0 and current_price > 0:
                    pnl = float(
                        decimal_math.calculate_pnl(entry_price, current_price, quantity)
                    )
                    pnl_pct = float(
                        decimal_math.calculate_pnl_percentage(
                            entry_price, current_price
                        )
                    )
                else:
                    pnl = 0
                    pnl_pct = 0

                pnl_text = f"${pnl:+.2f} ({pnl_pct:+.2f}%)"
                pnl_class = "pnl-positive" if pnl >= 0 else "pnl-negative"
            else:
                status_badge = html.Span("🟢 Available", className="badge bg-success")
                pnl_text = "-"
                pnl_class = "text-muted"

            price_text = f"${current_price:.2f}" if current_price > 0 else "No data"

            table_rows.append(
                html.Tr(
                    [
                        html.Td(symbol, className="fw-bold"),
                        html.Td(status_badge),
                        html.Td(price_text, className="text-end"),
                        html.Td(
                            pnl_text,
                            className=f"text-end fw-bold {pnl_class}",
                        ),
                    ]
                )
            )

        return html.Table(
            [
                html.Thead(
                    html.Tr(
                        [
                            html.Th("Symbol"),
                            html.Th("Status"),
                            html.Th("Price", className="text-end"),
                            html.Th("P&L", className="text-end"),
                        ]
                    )
                ),
                html.Tbody(table_rows),
            ],
            className="table table-sm table-hover",
        )

    def _create_recent_trades_table(self, trades: list) -> Any:

        if not trades:
            return html.P("No recent trades", className="text-muted text-center mt-3")

        table_rows = []

        for trade in trades:
            if trade.get("exit_price") is None:
                continue

            symbol = trade.get("symbol", "")
            entry_price = float(trade.get("entry_price") or 0)
            exit_price = float(trade.get("exit_price") or 0)
            realized_pnl = float(trade.get("realized_pnl") or 0)
            exit_time = trade.get("exit_time", "")
            entry_time = trade.get("entry_time", "")
            exit_reason = trade.get("exit_reason", "")

            pnl_pct = ((exit_price / entry_price) - 1) * 100 if entry_price > 0 else 0
            pnl_class = "pnl-positive" if realized_pnl >= 0 else "pnl-negative"
            icon = "✅" if realized_pnl >= 0 else "❌"

            duration = ""
            if exit_time and entry_time:
                try:
                    from datetime import datetime

                    exit_dt = datetime.fromisoformat(
                        str(exit_time).replace("Z", "+00:00")
                    )
                    entry_dt = datetime.fromisoformat(
                        str(entry_time).replace("Z", "+00:00")
                    )
                    duration_minutes = (exit_dt - entry_dt).total_seconds() / 60
                    if duration_minutes >= 60:
                        duration = f"{duration_minutes / 60:.1f}h"
                    else:
                        duration = f"{duration_minutes:.0f}m"
                except Exception:
                    duration = "-"

            table_rows.append(
                html.Tr(
                    [
                        html.Td(f"{icon} {symbol}", className="fw-bold"),
                        html.Td(f"${entry_price:.2f}", className="text-end"),
                        html.Td(f"${exit_price:.2f}", className="text-end"),
                        html.Td(
                            f"{pnl_pct:+.2f}%",
                            className=f"text-end fw-bold {pnl_class}",
                        ),
                        html.Td(duration, className="text-center"),
                        html.Td(exit_reason, className="text-center text-muted"),
                        html.Td(
                            exit_time[-8:] if exit_time else "",
                            className="text-end text-muted",
                        ),
                    ]
                )
            )

        return html.Table(
            [
                html.Thead(
                    html.Tr(
                        [
                            html.Th("Symbol"),
                            html.Th("Entry", className="text-end"),
                            html.Th("Exit", className="text-end"),
                            html.Th("P&L %", className="text-end"),
                            html.Th("Duration", className="text-center"),
                            html.Th("Reason", className="text-center"),
                            html.Th("Time", className="text-end"),
                        ]
                    )
                ),
                html.Tbody(table_rows),
            ],
            className="table table-sm table-dark table-striped table-hover",
        )

    def _print_update_status(self):
        uptime = datetime.now() - self.start_time
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)
        seconds = int(uptime.total_seconds() % 60)

        if hours > 0:
            uptime_str = f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            uptime_str = f"{minutes}m {seconds}s"
        else:
            uptime_str = f"{seconds}s"

        sys.stdout.write(
            f"\r🔄 Updates: {self.update_count} | ⏱️ Uptime: {uptime_str}    "
        )
        sys.stdout.flush()

    async def close(self):
        if self.db:
            await self.db.close()

    def run(self, debug: bool = False, host: str = "0.0.0.0"):
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)

        print("\n" + "=" * 60)
        print("🤖 TRADING BOT DASHBOARD - DEVELOPMENT MODE")
        print("=" * 60)
        print(f"📊 URL: http://{host}:{self.port}")
        print(f"🗄️  Database: {self.db_path}")
        print("🔄 Auto-refresh: 2 seconds")
        print(f"🐛 Debug mode: {'ON' if debug else 'OFF'}")
        print("=" * 60 + "\n")

        self.app.run(host=host, port=self.port, debug=debug)


def main():
    dashboard = TradingDashboard(port=8050)
    dashboard.run(debug=False)


if __name__ == "__main__":
    main()

# Export para run_dashboard.py
_dashboard_instance = None


def get_app():
    global _dashboard_instance
    if _dashboard_instance is None:
        _dashboard_instance = TradingDashboard()
    return _dashboard_instance.app


if "run_dashboard" in sys.argv[0] and "--production" not in sys.argv:
    app = None
else:
    app = get_app()
