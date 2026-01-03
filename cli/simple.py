#!/usr/bin/env python3
"""Simple CLI for PrintDAO - Live terminal output with exchange tabs."""

import asyncio
import sys
from datetime import datetime
from decimal import Decimal
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text

from indexers.polymarket import PolymarketClient
from indexers.polymarket import Trade as PMTrade
from indexers.hyperliquid import HyperliquidClient
from indexers.hyperliquid import Trade as HLTrade


console = Console()


# ============== Polymarket Tables ==============

def create_pm_trades_table(trades: list[PMTrade]) -> Table:
    """Create Polymarket trades table."""
    table = Table(title="Recent Trades", show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Time", style="dim", width=8)
    table.add_column("Side", width=4)
    table.add_column("Size", justify="right", width=10)
    table.add_column("Market", width=30, overflow="ellipsis")
    table.add_column("Position", width=12, overflow="ellipsis")
    table.add_column("Price", justify="right", width=6)
    table.add_column("Trader", width=15, overflow="ellipsis")

    for t in trades[:15]:
        time_str = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")
        side_style = "green" if t.side.value == "BUY" else "red"
        trader = t.pseudonym or t.name or f"{t.proxy_wallet[:10]}..."

        table.add_row(
            time_str,
            Text(t.side.value, style=side_style),
            f"${t.usdc_size:,.0f}",
            t.title[:30] + ("..." if len(t.title) > 30 else ""),
            t.outcome[:12],
            f"{t.price*100:.1f}%",
            trader[:15],
        )

    return table


def create_pm_whales_table(whales: list[PMTrade]) -> Table:
    """Create Polymarket whale trades table."""
    table = Table(title="Whale Alerts ($10k+)", show_header=True, header_style="bold yellow", expand=True)
    table.add_column("Time", style="dim", width=8)
    table.add_column("Side", width=4)
    table.add_column("Size", justify="right", style="bold yellow", width=10)
    table.add_column("Market", width=25, overflow="ellipsis")
    table.add_column("Position", width=10, overflow="ellipsis")
    table.add_column("Price", justify="right", width=6)

    for t in whales[:10]:
        time_str = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")
        side_style = "green" if t.side.value == "BUY" else "red"

        table.add_row(
            time_str,
            Text(t.side.value, style=side_style),
            f"${t.usdc_size:,.0f}",
            t.title[:25] + ("..." if len(t.title) > 25 else ""),
            t.outcome[:10],
            f"{t.price*100:.1f}%",
        )

    return table


# ============== Hyperliquid Tables ==============

def create_hl_trades_table(trades: list[HLTrade]) -> Table:
    """Create Hyperliquid trades table."""
    table = Table(title="Recent Trades", show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Time", style="dim", width=8)
    table.add_column("Coin", width=6)
    table.add_column("Side", width=5)
    table.add_column("Size", justify="right", width=12)
    table.add_column("Price", justify="right", width=12)
    table.add_column("Notional", justify="right", width=14)

    for t in trades[:15]:
        time_str = t.datetime.strftime("%H:%M:%S")
        is_buy = t.side.value == "B"
        side_text = "LONG" if is_buy else "SHORT"
        side_style = "green" if is_buy else "red"

        table.add_row(
            time_str,
            t.coin,
            Text(side_text, style=side_style),
            f"{t.size:,.4f}",
            f"${t.price:,.2f}",
            f"${t.notional_usd:,.0f}",
        )

    return table


def create_hl_whales_table(whales: list[HLTrade]) -> Table:
    """Create Hyperliquid whale trades table."""
    table = Table(title="Whale Alerts ($10M+)", show_header=True, header_style="bold yellow", expand=True)
    table.add_column("Time", style="dim", width=8)
    table.add_column("Coin", width=6)
    table.add_column("Side", width=5)
    table.add_column("Notional", justify="right", style="bold yellow", width=14)
    table.add_column("Price", justify="right", width=12)

    for t in whales[:10]:
        time_str = t.datetime.strftime("%H:%M:%S")
        is_buy = t.side.value == "B"
        side_text = "LONG" if is_buy else "SHORT"
        side_style = "green" if is_buy else "red"

        table.add_row(
            time_str,
            t.coin,
            Text(side_text, style=side_style),
            f"${t.notional_usd:,.0f}",
            f"${t.price:,.2f}",
        )

    return table


# ============== Layout Builders ==============

def create_polymarket_layout(trades: list, whales: list, stats: dict) -> Layout:
    """Create Polymarket layout."""
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )

    layout["main"].split_row(
        Layout(name="trades", ratio=2),
        Layout(name="whales", ratio=1),
    )

    # Header
    header_text = Text()
    header_text.append("PRINTDAO", style="bold magenta")
    header_text.append(" | ", style="dim")
    header_text.append("[P] POLYMARKET", style="bold green")
    header_text.append(" | [H] Hyperliquid", style="dim")
    header_text.append(" | ", style="dim")
    header_text.append(f"Trades: {stats.get('total_trades', 0)}", style="cyan")
    header_text.append(f" | Vol: ${stats.get('total_volume', 0):,.0f}", style="green")
    header_text.append(f" | Whales: {stats.get('whale_count', 0)}", style="yellow")
    header_text.append(f" | {stats.get('last_update', 'N/A')}", style="dim")

    layout["header"].update(Panel(header_text, style="bold"))

    # Main content
    layout["trades"].update(Panel(create_pm_trades_table(trades), border_style="green"))
    layout["whales"].update(Panel(create_pm_whales_table(whales), border_style="yellow"))

    # Footer
    footer_text = Text("Press Ctrl+C to exit | [P] Polymarket | [H] Hyperliquid", style="dim")
    layout["footer"].update(Panel(footer_text))

    return layout


def create_hyperliquid_layout(trades: list, whales: list, stats: dict) -> Layout:
    """Create Hyperliquid layout."""
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )

    layout["main"].split_row(
        Layout(name="trades", ratio=2),
        Layout(name="whales", ratio=1),
    )

    # Header
    header_text = Text()
    header_text.append("PRINTDAO", style="bold magenta")
    header_text.append(" | ", style="dim")
    header_text.append("[P] Polymarket", style="dim")
    header_text.append(" | ", style="dim")
    header_text.append("[H] HYPERLIQUID", style="bold blue")
    header_text.append(" | ", style="dim")
    header_text.append(f"Trades: {stats.get('total_trades', 0)}", style="cyan")
    header_text.append(f" | Vol: ${stats.get('total_volume', 0):,.0f}", style="green")
    header_text.append(f" | Whales: {stats.get('whale_count', 0)}", style="yellow")
    header_text.append(f" | {stats.get('last_update', 'N/A')}", style="dim")

    layout["header"].update(Panel(header_text, style="bold"))

    # Main content
    layout["trades"].update(Panel(create_hl_trades_table(trades), border_style="blue"))
    layout["whales"].update(Panel(create_hl_whales_table(whales), border_style="yellow"))

    # Footer
    footer_text = Text("Press Ctrl+C to exit | [P] Polymarket | [H] Hyperliquid", style="dim")
    layout["footer"].update(Panel(footer_text))

    return layout


# ============== Main App ==============

class PrintDAOCLI:
    """Main CLI application."""

    def __init__(self):
        self.current_exchange = "polymarket"  # or "hyperliquid"

        # Polymarket data
        self.pm_client = PolymarketClient()
        self.pm_trades: list[PMTrade] = []
        self.pm_whales: list[PMTrade] = []
        self.pm_seen_tx: set[str] = set()
        self.pm_stats = {"total_trades": 0, "total_volume": 0, "whale_count": 0, "last_update": "Starting..."}

        # Hyperliquid data
        self.hl_client = HyperliquidClient()
        self.hl_trades: list[HLTrade] = []
        self.hl_whales: list[HLTrade] = []
        self.hl_seen_tid: set[str] = set()
        self.hl_stats = {"total_trades": 0, "total_volume": 0, "whale_count": 0, "last_update": "Starting..."}

        # Top coins to monitor on Hyperliquid
        self.hl_coins = ["BTC", "ETH", "SOL", "DOGE", "WIF", "PEPE", "ARB", "SUI", "AVAX", "LINK"]

    def get_current_layout(self) -> Layout:
        """Get layout for current exchange."""
        if self.current_exchange == "polymarket":
            return create_polymarket_layout(self.pm_trades, self.pm_whales, self.pm_stats)
        else:
            return create_hyperliquid_layout(self.hl_trades, self.hl_whales, self.hl_stats)

    async def fetch_polymarket(self) -> None:
        """Fetch Polymarket data."""
        try:
            trades = await self.pm_client.get_trades(min_size=1000, limit=50)

            for t in trades:
                if t.transaction_hash not in self.pm_seen_tx:
                    self.pm_seen_tx.add(t.transaction_hash)
                    self.pm_trades.insert(0, t)

                    if t.usdc_size >= 10000:
                        self.pm_whales.insert(0, t)

            # Keep lists bounded
            self.pm_trades = self.pm_trades[:100]
            self.pm_whales = self.pm_whales[:50]

            # Update stats
            if self.pm_trades:
                self.pm_stats = {
                    "total_trades": len(self.pm_trades),
                    "total_volume": sum(t.usdc_size for t in self.pm_trades),
                    "whale_count": len(self.pm_whales),
                    "last_update": datetime.now().strftime("%H:%M:%S"),
                }
        except Exception as e:
            self.pm_stats["last_update"] = f"Error: {str(e)[:15]}"

    async def fetch_hyperliquid(self) -> None:
        """Fetch Hyperliquid data."""
        try:
            for coin in self.hl_coins:
                try:
                    trades = await self.hl_client.get_recent_trades(coin, limit=20)

                    for t in trades:
                        if t.tid not in self.hl_seen_tid:
                            self.hl_seen_tid.add(t.tid)
                            self.hl_trades.insert(0, t)

                            if t.notional_usd >= Decimal("10000000"):
                                self.hl_whales.insert(0, t)

                except Exception as e:
                    pass  # Skip failed coin fetches

            # Keep lists bounded
            self.hl_trades = self.hl_trades[:100]
            self.hl_whales = self.hl_whales[:50]

            # Sort by time
            self.hl_trades.sort(key=lambda t: t.time_ms, reverse=True)
            self.hl_whales.sort(key=lambda t: t.time_ms, reverse=True)

            # Update stats
            if self.hl_trades:
                self.hl_stats = {
                    "total_trades": len(self.hl_trades),
                    "total_volume": float(sum(t.notional_usd for t in self.hl_trades)),
                    "whale_count": len(self.hl_whales),
                    "last_update": datetime.now().strftime("%H:%M:%S"),
                }
        except Exception as e:
            self.hl_stats["last_update"] = f"Error: {str(e)[:15]}"

    async def run(self) -> None:
        """Run the CLI application."""
        console.print("[bold magenta]Starting PrintDAO CLI...[/]")
        console.print("[dim]Press P for Polymarket, H for Hyperliquid, Ctrl+C to exit[/]\n")

        try:
            with Live(self.get_current_layout(), refresh_per_second=1, console=console) as live:
                while True:
                    # Fetch data for both exchanges
                    await asyncio.gather(
                        self.fetch_polymarket(),
                        self.fetch_hyperliquid(),
                    )

                    # Update display
                    live.update(self.get_current_layout())

                    await asyncio.sleep(5)

        except KeyboardInterrupt:
            console.print("\n[bold red]Shutting down...[/]")
        finally:
            await self.pm_client.close()


async def main_with_input():
    """Main with keyboard input handling."""
    import sys
    import select

    cli = PrintDAOCLI()

    console.print("[bold magenta]Starting PrintDAO CLI...[/]")
    console.print("[dim]Press P for Polymarket, H for Hyperliquid, Q to quit[/]\n")

    try:
        with Live(cli.get_current_layout(), refresh_per_second=1, console=console) as live:
            while True:
                # Fetch data
                await asyncio.gather(
                    cli.fetch_polymarket(),
                    cli.fetch_hyperliquid(),
                )

                # Update display
                live.update(cli.get_current_layout())

                await asyncio.sleep(5)

    except KeyboardInterrupt:
        console.print("\n[bold red]Shutting down...[/]")
    finally:
        await cli.pm_client.close()


def run():
    """Run the CLI."""
    # Check for command line argument for exchange
    if len(sys.argv) > 1:
        exchange = sys.argv[1].lower()
        if exchange in ["p", "polymarket", "pm"]:
            asyncio.run(run_single_exchange("polymarket"))
        elif exchange in ["h", "hyperliquid", "hl"]:
            asyncio.run(run_single_exchange("hyperliquid"))
        else:
            console.print(f"[red]Unknown exchange: {exchange}[/]")
            console.print("Usage: python -m cli.simple [polymarket|hyperliquid]")
            sys.exit(1)
    else:
        # Default: run with both exchanges
        asyncio.run(main_with_input())


async def run_single_exchange(exchange: str):
    """Run CLI for a single exchange."""
    cli = PrintDAOCLI()
    cli.current_exchange = exchange

    console.print(f"[bold magenta]Starting PrintDAO CLI - {exchange.upper()}[/]")
    console.print("[dim]Press Ctrl+C to exit[/]\n")

    try:
        with Live(cli.get_current_layout(), refresh_per_second=1, console=console) as live:
            while True:
                if exchange == "polymarket":
                    await cli.fetch_polymarket()
                else:
                    await cli.fetch_hyperliquid()

                live.update(cli.get_current_layout())
                await asyncio.sleep(5)

    except KeyboardInterrupt:
        console.print("\n[bold red]Shutting down...[/]")
    finally:
        await cli.pm_client.close()


if __name__ == "__main__":
    run()