#!/usr/bin/env python3
"""Standalone trade indexer - runs perpetually and indexes ALL trades."""

import asyncio
import sys
import os
import logging
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indexers.polymarket.trade_indexer import PolymarketTradeIndexer
from indexers.hyperliquid.trade_indexer import HyperliquidTradeIndexer

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class HeadlessIndexer:
    """Headless indexer for Docker/Railway deployment."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.pm_indexer = PolymarketTradeIndexer(redis_url=redis_url, poll_interval=2.0)
        self.hl_indexer = HyperliquidTradeIndexer(redis_url=redis_url, poll_interval=3.0)
        self.running = True

    async def log_stats(self):
        """Log stats periodically."""
        while self.running:
            await asyncio.sleep(60)  # Log every minute
            pm = self.pm_indexer._stats
            hl = self.hl_indexer._stats
            logger.info(
                f"PM: {pm.total_trades_indexed:,} trades, ${pm.total_volume_usd:,.0f} vol, {pm.trades_last_minute}/min | "
                f"HL: {hl.total_trades_indexed:,} trades, ${hl.total_volume_usd:,.0f} vol, {hl.trades_last_minute}/min"
            )

    async def run(self):
        """Run indexers without UI."""
        logger.info("Starting PrintDAO Trade Indexer (headless mode)")
        logger.info(f"Redis: {self.redis_url}")
        logger.info("Indexing ALL trades from Polymarket and Hyperliquid")

        await self.pm_indexer.start()
        await self.hl_indexer.start()

        stats_task = asyncio.create_task(self.log_stats())

        try:
            while self.running:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
        finally:
            self.running = False
            stats_task.cancel()
            await self.pm_indexer.stop()
            await self.hl_indexer.stop()

            pm = self.pm_indexer._stats
            hl = self.hl_indexer._stats
            logger.info(f"Final - PM: {pm.total_trades_indexed:,} trades | HL: {hl.total_trades_indexed:,} trades")


def is_terminal():
    """Check if running in a terminal."""
    return sys.stdout.isatty() and os.environ.get("HEADLESS") != "1"


async def main():
    # Get Redis URL from env or args
    redis_url = os.environ.get("REDIS_URL") or (sys.argv[1] if len(sys.argv) > 1 else "redis://localhost:6379/0")

    if is_terminal():
        # Import Rich only when needed (terminal mode)
        from rich.console import Console
        from rich.live import Live
        from rich.table import Table
        from rich.panel import Panel
        from rich.layout import Layout
        from rich.text import Text

        console = Console()

        class IndexerDashboard:
            """Live dashboard for terminal."""

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
        # Headless mode for Docker/Railway
        indexer = HeadlessIndexer(redis_url)
        await indexer.run()


if __name__ == "__main__":
    asyncio.run(main())
