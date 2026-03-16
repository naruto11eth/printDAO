"""Main entry point for PrintDAO Telegram bot."""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import BotConfig, setup_logging
from bot.utils.auth import configure_admin_ids
from bot.routers.general import set_bot_start_time
from bot.storage.redis_client import RedisClient
from bot.services.wallet_service import WalletService
from bot.services.notification_service import NotificationService
from bot.services.position_indexer import PositionIndexer
from bot.services.whale_detector import ConfigurableWhaleDetector
from bot.services.analytics_service import AnalyticsService
from bot.services.copy_trading_service import CopyTradingService
from bot.services.leaderboard_service import LeaderboardService
from bot.routers import general, tracking, polymarket, hyperliquid, settings
from bot.routers import leaderboard as leaderboard_router
from bot.middlewares import RateLimitMiddleware, ThrottleMiddleware
from bot.schedulers import MarketMonitorScheduler
from indexers.polymarket import PolymarketIndexer
from indexers.hyperliquid import HyperliquidClient

logger = logging.getLogger(__name__)


async def main() -> None:
    """Main entry point."""
    # Load configuration
    config = BotConfig.from_env()

    # Setup logging
    setup_logging(config.log_level)

    # Validate config
    if not config.validate():
        logger.error("Invalid configuration. Please check your .env file.")
        sys.exit(1)

    logger.info("Starting PrintDAO bot...")

    configure_admin_ids(config.admin_user_ids)
    set_bot_start_time()

    # Initialize Redis
    redis_client = RedisClient(config.redis_url)
    try:
        await redis_client.connect()
    except Exception as e:
        logger.error(f"Failed to connect to Redis: {e}")
        logger.info("Make sure Redis is running: docker-compose up -d redis")
        sys.exit(1)

    # Initialize bot with HTML parse mode by default
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Initialize services
    wallet_service = WalletService(redis_client, config.max_tracked_wallets_per_user)
    notification_service = NotificationService(bot)

    # Initialize Hyperliquid client
    hyperliquid_client = HyperliquidClient(
        testnet=False,
        max_retries=5,
        backoff_seconds=0.5,
    )

    # Initialize Polymarket indexer
    polymarket_indexer = PolymarketIndexer(
        redis_client=redis_client,
        notification_service=notification_service,
        whale_threshold=config.polymarket_whale_threshold,
        poll_interval=30.0,
    )

    # Initialize Position indexer for analytics tracking
    position_indexer = PositionIndexer(
        redis_client=redis_client,
        wallet_service=wallet_service,
        hyperliquid_client=hyperliquid_client,
        poll_interval=60.0,
    )

    # Initialize configurable whale detector
    whale_detector = ConfigurableWhaleDetector(redis_client=redis_client)

    # Initialize analytics service
    analytics_service = AnalyticsService(
        hl_client=hyperliquid_client,
        redis=redis_client,
    )

    # Initialize copy trading service
    copy_trading_service = CopyTradingService(redis=redis_client)

    # Initialize market monitor scheduler
    market_scheduler = MarketMonitorScheduler(
        redis=redis_client,
        bot=bot,
    )

    # Initialize dispatcher with services in workflow_data
    dp = Dispatcher()

    # Register middlewares
    dp.message.middleware(ThrottleMiddleware(limit=0.5))
    dp.message.middleware(RateLimitMiddleware(redis_client))
    dp.callback_query.middleware(RateLimitMiddleware(redis_client))

    # Store services in dispatcher's workflow_data for access from handlers
    dp["redis_client"] = redis_client
    dp["wallet_service"] = wallet_service
    dp["notification_service"] = notification_service
    dp["hyperliquid_client"] = hyperliquid_client
    dp["polymarket_indexer"] = polymarket_indexer
    dp["position_indexer"] = position_indexer
    dp["whale_detector"] = whale_detector
    dp["analytics_service"] = analytics_service
    dp["copy_trading_service"] = copy_trading_service
    leaderboard_service = LeaderboardService(redis_client, analytics_service, hyperliquid_client)
    dp["leaderboard_service"] = leaderboard_service
    dp["market_scheduler"] = market_scheduler
    dp["config"] = config

    # Register routers
    dp.include_router(general.router)
    dp.include_router(tracking.router)
    dp.include_router(polymarket.router)
    dp.include_router(hyperliquid.router)
    dp.include_router(settings.router)
    dp.include_router(leaderboard_router.router)

    # Startup handler
    @dp.startup()
    async def on_startup() -> None:
        logger.info("Bot started successfully!")
        # Get bot info
        bot_info = await bot.get_me()
        logger.info(f"Bot username: @{bot_info.username}")
        logger.info("Add the bot to your Telegram group to start tracking whales!")

        # Start Polymarket indexer
        await polymarket_indexer.start()

        # Start Position indexer
        await position_indexer.start()

        # Start market monitor scheduler
        await market_scheduler.start()
        await market_scheduler.restore_user_jobs()

    # Shutdown handler
    @dp.shutdown()
    async def on_shutdown() -> None:
        logger.info("Shutting down...")
        # Stop scheduler
        await market_scheduler.stop()
        # Stop indexers
        await polymarket_indexer.stop()
        await position_indexer.stop()
        await redis_client.disconnect()
        logger.info("Shutdown complete.")

    # Start polling
    logger.info("Starting bot polling...")
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
    )


def run() -> None:
    """Run the bot."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run()
