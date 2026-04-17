#!/usr/bin/env python3
"""Standalone trade indexer - runs perpetually and indexes ALL trades.

Phase A ingest pivot: HL trades default to WebSocket (set HL_USE_WS_TRADES=0
to fall back to REST polling). WS feeds trades via
`HyperliquidTradeIndexer.ingest_ws_trades`; the indexer's `_run_loop` stays
alive for stats/pruning. Trade events are also fanned out to the Redis Streams
event bus (`stream:trades:hl`) for downstream consumers added in Phase B.
"""

import asyncio
import sys
import os
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indexers.polymarket.trade_indexer import PolymarketTradeIndexer
from indexers.hyperliquid.trade_indexer import HyperliquidTradeIndexer
from indexers.hyperliquid.ws_client import HyperliquidWS
from indexers.hyperliquid.models import Trade as HLTrade
from bot.storage.redis_client import RedisClient
from bot.services.stream_service import EventBusProducer
from shared.constants import StreamName

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _trade_to_stream_fields(trade: HLTrade) -> dict:
    return {
        "tid": trade.tid,
        "coin": trade.coin,
        "time_ms": trade.time_ms,
        "price": str(trade.price),
        "size": str(trade.size),
        "side": trade.side.value,
        "notional_usd": str(trade.notional_usd),
        "direction": trade.direction.value,
        "hash": trade.hash,
        "taker": trade.users[0] if trade.users else "",
        "maker": trade.users[1] if len(trade.users) > 1 else "",
    }


class HeadlessIndexer:
    """Headless indexer for Docker/Railway deployment."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.use_ws_trades = _env_bool("HL_USE_WS_TRADES", True)

        self.redis_client = RedisClient(redis_url)
        self.producer = EventBusProducer(self.redis_client)

        async def on_trade(trade: HLTrade) -> None:
            try:
                await self.producer.publish(
                    StreamName.TRADES_HL, _trade_to_stream_fields(trade),
                )
            except Exception as e:
                logger.debug("Stream publish failed for trade %s: %s", trade.tid, e)

        self.pm_indexer = PolymarketTradeIndexer(redis_url=redis_url, poll_interval=2.0)
        self.hl_indexer = HyperliquidTradeIndexer(
            redis_url=redis_url,
            poll_interval=3.0,
            on_trade=on_trade,
            rest_polling_enabled=not self.use_ws_trades,
        )

        self.hl_ws: HyperliquidWS | None = None
        if self.use_ws_trades:
            self.hl_ws = HyperliquidWS(on_trade=self.hl_indexer.ingest_ws_trades)
            for coin in self.hl_indexer.coins:
                self.hl_ws.subscribe_trades(coin)

        self.running = True

    async def log_stats(self):
        while self.running:
            await asyncio.sleep(60)
            pm = self.pm_indexer._stats
            hl = self.hl_indexer._stats
            ws_state = "connected" if self.hl_ws and self.hl_ws.is_connected else "off"
            logger.info(
                "PM: %d trades, $%.0f vol, %d/min | "
                "HL: %d trades, $%.0f vol, %d/min, ws=%s",
                pm.total_trades_indexed, pm.total_volume_usd, pm.trades_last_minute,
                hl.total_trades_indexed, hl.total_volume_usd, hl.trades_last_minute,
                ws_state,
            )

    async def run(self):
        logger.info("Starting PrintDAO Trade Indexer (headless mode)")
        logger.info(f"Redis: {self.redis_url}")
        logger.info(
            "HL ingest: %s | PM ingest: REST",
            "WebSocket" if self.use_ws_trades else "REST",
        )

        await self.redis_client.connect()
        await self.pm_indexer.start()
        await self.hl_indexer.start()
        if self.hl_ws is not None:
            await self.hl_ws.start()

        stats_task = asyncio.create_task(self.log_stats())

        try:
            while self.running:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
        finally:
            self.running = False
            stats_task.cancel()
            if self.hl_ws is not None:
                await self.hl_ws.stop()
            await self.pm_indexer.stop()
            await self.hl_indexer.stop()
            await self.redis_client.disconnect()

            pm = self.pm_indexer._stats
            hl = self.hl_indexer._stats
            logger.info(
                "Final - PM: %d trades | HL: %d trades",
                pm.total_trades_indexed, hl.total_trades_indexed,
            )


def is_terminal():
    return sys.stdout.isatty() and os.environ.get("HEADLESS") != "1"


async def main():
    redis_url = os.environ.get("REDIS_URL") or (sys.argv[1] if len(sys.argv) > 1 else "redis://localhost:6379/0")

    if is_terminal():
        from rich.console import Console
        from rich.live import Live
        from rich.table import Table
        from rich.panel import Panel
        from rich.layout import Layout
        from rich.text import Text

        console = Console()

        class IndexerDashboard:
            """Live dashboard for terminal — dev-only, still uses REST polling."""

            def __init__(self):
                self.pm_indexer = PolymarketTradeIndexer(redis_url=redis_url, poll_interval=2.0)
                self.hl_indexer = HyperliquidTradeIndexer(redis_url=redis_url, poll_interval=3.0)
                self.running = True
                self.recent_pm_trades = []
                self.recent_hl_trades = []

            def create_stats_table(self) -> Table:
                table = Table(title="Indexer Statistics", expand=True)
                table.add_column("Metric", style="cyan")
                table.add_column("Polymarket", justify="right", style="green")
                table.add_column("Hyperliquid", justify="right", style="blue")

                pm = self.pm_indexer._stats
                hl = self.hl_indexer._stats

                table.add_row("Total Indexed", f"{pm.total_trades_indexed:,}", f"{hl.total_trades_indexed:,}")
                table.add_row("Trades/min", f"{pm.trades_last_minute}", f"{hl.trades_last_minute}")
                table.add_row("Trades/hour", f"{pm.trades_last_hour:,}", f"{hl.trades_last_hour:,}")
                table.add_row("Whale Trades", f"{pm.whale_trades:,}", f"{hl.whale_trades:,}")
                table.add_row("Volume", f"${pm.total_volume_usd:,.0f}", f"${hl.total_volume_usd:,.0f}")
                table.add_row("Errors", f"{pm.errors}", f"{hl.errors}")
                table.add_row("Uptime", f"{pm.uptime_seconds/60:.1f}m", f"{hl.uptime_seconds/60:.1f}m")

                return table

            def create_layout(self) -> Layout:
                layout = Layout()
                layout.split_column(
                    Layout(name="header", size=3),
                    Layout(name="stats", size=12),
                    Layout(name="footer", size=3),
                )

                header = Text()
                header.append("PRINTDAO TRADE INDEXER", style="bold magenta")
                header.append(f" | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", style="dim")
                header.append(" | ", style="dim")
                header.append("RUNNING", style="bold green")

                layout["header"].update(Panel(header, style="bold"))
                layout["stats"].update(Panel(self.create_stats_table(), border_style="cyan"))
                layout["footer"].update(Panel(Text("Ctrl+C to stop | Indexing to Redis", style="dim")))

                return layout

            async def run(self):
                console.print("[bold magenta]Starting PrintDAO Trade Indexer...[/]")
                console.print(f"[dim]Redis: {redis_url}[/]\n")

                await self.pm_indexer.start()
                await self.hl_indexer.start()

                try:
                    with Live(self.create_layout(), refresh_per_second=1, console=console) as live:
                        while self.running:
                            live.update(self.create_layout())
                            await asyncio.sleep(1)
                except KeyboardInterrupt:
                    console.print("\n[yellow]Stopping...[/]")
                finally:
                    await self.pm_indexer.stop()
                    await self.hl_indexer.stop()

                    pm = self.pm_indexer._stats
                    hl = self.hl_indexer._stats
                    console.print(f"\n[bold]Final:[/] PM: {pm.total_trades_indexed:,} | HL: {hl.total_trades_indexed:,}")

        dashboard = IndexerDashboard()
        await dashboard.run()
    else:
        indexer = HeadlessIndexer(redis_url)
        await indexer.run()


if __name__ == "__main__":
    asyncio.run(main())
