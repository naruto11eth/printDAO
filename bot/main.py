"""Main entry point for PrintDAO Telegram bot."""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import BotConfig, setup_logging
from bot.storage.redis_client import RedisClient
from bot.services.wallet_service import WalletService
from bot.services.notification_service import NotificationService
from bot.routers import general, tracking, polymarket
from indexers.polymarket import PolymarketIndexer

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

    # Initialize Polymarket indexer
    polymarket_indexer = PolymarketIndexer(
        redis_client=redis_client,
        notification_service=notification_service,
        whale_threshold=config.polymarket_whale_threshold,
        poll_interval=30.0,
    )

    # Initialize dispatcher with services in workflow_data
    dp = Dispatcher()

    # Store services in dispatcher's workflow_data for access from handlers
    dp["redis_client"] = redis_client
    dp["wallet_service"] = wallet_service
    dp["notification_service"] = notification_service
    dp["polymarket_indexer"] = polymarket_indexer
    dp["config"] = config

    # Register routers
    dp.include_router(general.router)
    dp.include_router(tracking.router)
    dp.include_router(polymarket.router)

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

    # Shutdown handler
    @dp.shutdown()
    async def on_shutdown() -> None:
        logger.info("Shutting down...")
        # Stop Polymarket indexer
        await polymarket_indexer.stop()
        await redis_client.disconnect()
        logger.info("Shutdown complete.")

    # Start polling
    try:
        logger.info("Starting bot polling...")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await redis_client.disconnect()


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
