import os
import sys

from atlas.config import LIVE_TRADING, SYSTEM_NAME, TRADING_MODE, VERSION
from atlas.logger import get_logger
from atlas.market_data import get_market_status
from atlas.scanner import get_scanner_status
from atlas.signals import get_signals_status
from atlas.strategy import get_strategy_status
from atlas.risk import get_risk_status
from atlas.portfolio import get_portfolio_status
from atlas.execution import get_execution_status
from atlas.broker import get_broker_status
from atlas.alerts import get_alerts_status
from atlas.telegram import get_telegram_status
from atlas.database import get_database_status
from atlas.company import get_company_status
from atlas.news import get_news_status
from atlas.adaptive import get_adaptive_status
from atlas.envfile import load_dotenv
from atlas.session import run_book_status, run_paper_loop, run_paper_session
from config.settings import load_settings


logger = get_logger()
_USAGE = (
    "Usage: python main.py [--paper] [--loop] | --dashboard | --train | --status | --status-book\n"
    "Paper session environment variables are listed in README.md."
)


def log_status():
    settings = load_settings()
    logger.info("%s V%s ONLINE", SYSTEM_NAME, VERSION)
    logger.info("Trading mode: %s", TRADING_MODE)
    logger.info("Live trading: %s", LIVE_TRADING)
    logger.info("Configured instruments: %s", ", ".join(settings.instruments))
    logger.info(get_market_status())
    logger.info(get_scanner_status())
    logger.info(get_signals_status())
    logger.info(get_strategy_status())
    logger.info(get_adaptive_status())
    logger.info(get_risk_status())
    logger.info(get_portfolio_status())
    logger.info(get_execution_status())
    logger.info(get_broker_status())
    logger.info(get_alerts_status())
    logger.info(get_telegram_status())
    logger.info(get_database_status())
    logger.info(get_news_status())
    logger.info(get_company_status())
    logger.info("System status: INITIALIZED; live trading disabled")
    return {"mode": "status", "live_trading": False}


def main(argv=None):
    load_dotenv()
    args = list(sys.argv[1:] if argv is None else argv)
    if args in ([], ["--paper"]):
        return run_paper_session()
    if args in (["--paper", "--loop"], ["--loop", "--paper"], ["--loop"]):
        try:
            return run_paper_loop()
        except KeyboardInterrupt:
            logger.info("Paper loop stopped")
            return {
                "mode": "paper-loop",
                "stopped": "interrupt",
                "live_trading": False,
            }
    if args == ["--status"]:
        return log_status()
    if args == ["--status-book"]:
        return run_book_status()
    if args == ["--train"]:
        from atlas.baseline import ensure_baseline_model
        from atlas.database import AtlasDatabase

        settings = load_settings()
        database = AtlasDatabase(os.getenv("ATLAS_DATABASE_PATH", "database/atlas.sqlite3"))
        try:
            report = ensure_baseline_model(database, settings.instruments)
        finally:
            database.close()
        logger.info(
            "Baseline model %s; promoted=%s; %s",
            report.get("status"),
            report.get("promoted"),
            report.get("reason"),
        )
        return report
    if args == ["--dashboard"]:
        from atlas.dashboard import serve_dashboard
        serve_dashboard()
        return {"mode": "dashboard", "live_trading": False}
    raise SystemExit(_USAGE)


if __name__ == "__main__":
    main()
