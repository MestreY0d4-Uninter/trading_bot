#!/usr/bin/env python3

import argparse
import logging
import sys


def parse_args():
    parser = argparse.ArgumentParser(
        description="Trading Bot Dashboard - Real-time Web Interface"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8050,
        help="Port to run dashboard on (default: 8050)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Run in debug mode with hot reload"
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help="Run in production mode with Waitress WSGI server",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="data/trading_bot.db",
        help="Path to database file (default: data/trading_bot.db)",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if args.production and args.debug:
        print("❌ Cannot use --production and --debug together")
        sys.exit(1)

    dashboard = None

    try:
        if args.production:
            try:
                from waitress import serve
            except ImportError:
                print("❌ Waitress não instalado. Instale com: pip install waitress")
                sys.exit(1)

            logging.getLogger("waitress").setLevel(logging.ERROR)

            from api.dashboard_api import app

            print("\n" + "=" * 60)
            print("🤖 TRADING BOT DASHBOARD - PRODUCTION MODE")
            print("=" * 60)
            print(f"📊 URL: http://{args.host}:{args.port}")
            print(f"🗄️  Database: {args.db_path}")
            print("🔄 Auto-refresh: 2 seconds")
            print("🔒 WSGI Server: Waitress (production-ready)")
            print("=" * 60)
            print("Press Ctrl+C to stop\n")

            serve(app.server, host=args.host, port=args.port, threads=4)

        else:
            from api.dashboard_api import TradingDashboard

            dashboard = TradingDashboard(db_path=args.db_path, port=args.port)

            print("\n" + "=" * 60)
            print("🤖 TRADING BOT DASHBOARD - DEVELOPMENT MODE")
            print("=" * 60)
            print(f"📊 URL: http://{args.host}:{args.port}")
            print(f"🗄️  Database: {args.db_path}")
            print("🔄 Auto-refresh: 2 seconds")
            print(f"🐛 Debug mode: {'ON' if args.debug else 'OFF'}")
            print("=" * 60 + "\n")

            dashboard.run(debug=args.debug, host=args.host)

    except KeyboardInterrupt:
        print("\n\n👋 Dashboard shutdown gracefully")
        if dashboard:
            if dashboard._event_loop and not dashboard._event_loop.is_closed():
                dashboard._event_loop.run_until_complete(dashboard.close())
                dashboard._event_loop.close()
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Error starting dashboard: {e}")
        if dashboard:
            if dashboard._event_loop and not dashboard._event_loop.is_closed():
                dashboard._event_loop.run_until_complete(dashboard.close())
                dashboard._event_loop.close()
        sys.exit(1)


if __name__ == "__main__":
    main()
