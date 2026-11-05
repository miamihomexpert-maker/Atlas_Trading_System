from atlas.config import SYSTEM_NAME, VERSION, TRADING_MODE, LIVE_TRADING
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
from atlas.news import get_news_status
from config.settings import load_settings
from atlas.adaptive import get_adaptive_status

logger = get_logger()

def main():
 settings = load_settings()
 logger.info(f'{SYSTEM_NAME} V{VERSION} ONLINE')
 logger.info(f'Trading mode: {TRADING_MODE}')
 logger.info(f'Live trading: {LIVE_TRADING}')
 logger.info(f'Configured instruments: {", ".join(settings.instruments)}')
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
 logger.info('System status: INITIALIZED; live trading disabled')


if __name__ == '__main__':
 main()
