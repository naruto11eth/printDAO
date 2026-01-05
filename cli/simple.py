#!/usr/bin/env python3
"""Simple CLI for PrintDAO - Live terminal output with exchange tabs."""

import asyncio
import sys
import os
import tty
import termios
import select
from datetime import datetime
from decimal import Decimal
from threading import Thread
from typing import Optional

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


# ============== Keyboard Handler ==============

class KeyboardHandler:
    """Non-blocking keyboard input handler for Unix systems."""

    def __init__(self, callback):
        self.callback = callback
        self.running = False
        self._thread: Optional[Thread] = None
        self._old_settings = None

    def start(self) -> None:
        """Start listening for keyboard input."""
        self.running = True
        self._thread = Thread(target=self._listen, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop listening for keyboard input."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=0.5)

    def _listen(self) -> None:
        """Listen for keyboard input in a separate thread."""
        fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)  # Use cbreak for Ctrl+C to still work
            while self.running:
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    key = sys.stdin.read(1).lower()
                    self.callback(key)
        except Exception:
            pass  # Silently handle any terminal errors
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass


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

    for t in trades:  # Show ALL trades
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

    for t in whales:  # Show ALL whales
        time_str = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")
        side_style = "green" if t.side.value == "BUY" else "red"

        table.add_row(
            time_str,
            Text(t.side.value, side_style),
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

    for t in trades:  # Show ALL trades
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

    for t in whales:  # Show ALL whales
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

def create_header_text(current_exchange: str, stats: dict) -> Text:
    """Create header text with tab indicators."""
    header_text = Text()
    header_text.append("PRINTDAO", style="bold magenta")
    header_text.append(" | ", style="dim")

    # Tab indicators
    tabs = [
        ("P", "polymarket", "green"),
        ("H", "hyperliquid", "blue"),
        ("N", "nado", "cyan"),
        ("L", "lighter", "yellow"),
        ("A", "all", "magenta"),
    ]

    for i, (key, name, color) in enumerate(tabs):
        if i > 0:
            header_text.append(" ", style="dim")

        if current_exchange == name:
            header_text.append(f"[{key}] {name.upper()}", style=f"bold {color}")
        else:
            header_text.append(f"[{key}]", style="dim")

    header_text.append(" | ", style="dim")
    header_text.append(f"Trades: {stats.get('total_trades', 0)}", style="cyan")
    header_text.append(f" | Vol: ${stats.get('total_volume', 0):,.0f}", style="green")
    header_text.append(f" | Whales: {stats.get('whale_count', 0)}", style="yellow")
    header_text.append(f" | {stats.get('last_update', 'N/A')}", style="dim")

    return header_text


def create_footer_text() -> Text:
    """Create footer with keyboard shortcuts."""
    return Text(
        "[P] Polymarket | [H] Hyperliquid | [N] Nado | [L] Lighter | [A] All | [Q] Quit",
        style="dim"
    )


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

    layout["header"].update(Panel(create_header_text("polymarket", stats), style="bold"))
    layout["trades"].update(Panel(create_pm_trades_table(trades), border_style="green"))
    layout["whales"].update(Panel(create_pm_whales_table(whales), border_style="yellow"))
    layout["footer"].update(Panel(create_footer_text()))

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

    layout["header"].update(Panel(create_header_text("hyperliquid", stats), style="bold"))
    layout["trades"].update(Panel(create_hl_trades_table(trades), border_style="blue"))
    layout["whales"].update(Panel(create_hl_whales_table(whales), border_style="yellow"))
    layout["footer"].update(Panel(create_footer_text()))

    return layout


def create_coming_soon_layout(exchange_name: str, stats: dict) -> Layout:
    """Create coming soon placeholder layout."""
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )

    # Coming soon message
    coming_soon = Text()
    coming_soon.append(f"\n\n{exchange_name.upper()}\n", style="bold cyan")
    coming_soon.append("Coming Soon\n\n", style="bold yellow")
    coming_soon.append("This exchange integration is under development.\n", style="dim")
    coming_soon.append("Check back soon for updates!", style="dim")

    layout["header"].update(Panel(create_header_text(exchange_name, stats), style="bold"))
    layout["main"].update(Panel(coming_soon, title=f"{exchange_name.title()} Integration"))
    layout["footer"].update(Panel(create_footer_text()))

    return layout


def create_all_exchanges_layout(
    pm_trades: list, pm_whales: list, pm_stats: dict,
    hl_trades: list, hl_whales: list, hl_stats: dict
) -> Layout:
    """Create layout showing all exchanges."""
    layout = Layout()

    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )

    layout["main"].split_row(
        Layout(name="polymarket"),
        Layout(name="hyperliquid"),
    )

    # Combined stats
    combined_stats = {
        "total_trades": pm_stats.get("total_trades", 0) + hl_stats.get("total_trades", 0),
        "total_volume": pm_stats.get("total_volume", 0) + hl_stats.get("total_volume", 0),
        "whale_count": pm_stats.get("whale_count", 0) + hl_stats.get("whale_count", 0),
        "last_update": pm_stats.get("last_update", "N/A"),
    }

    # Polymarket panel (trades only)
    pm_content = Layout()
    pm_content.split_column(
        Layout(create_pm_trades_table(pm_trades), name="trades"),
    )

    # Hyperliquid panel (trades only)
    hl_content = Layout()
    hl_content.split_column(
        Layout(create_hl_trades_table(hl_trades), name="trades"),
    )

    layout["header"].update(Panel(create_header_text("all", combined_stats), style="bold"))
    layout["polymarket"].update(Panel(pm_content, title="Polymarket", border_style="green"))
    layout["hyperliquid"].update(Panel(hl_content, title="Hyperliquid", border_style="blue"))
    layout["footer"].update(Panel(create_footer_text()))

    return layout


# ============== Main App ==============

class PrintDAOCLI:
    """Main CLI application with keyboard tab switching."""

    EXCHANGES = ["polymarket", "hyperliquid", "nado", "lighter", "all"]

    def __init__(self):
        self.current_exchange = "all"  # Default to all view
        self.running = True

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

        # Keyboard handler
        self.keyboard = KeyboardHandler(self._on_keypress)

    def _on_keypress(self, key: str) -> None:
        """Handle keyboard input."""
        key_map = {
            'p': 'polymarket',
            'h': 'hyperliquid',
            'n': 'nado',
            'l': 'lighter',
            'a': 'all',
            'q': None,  # Quit signal
        }

        if key in key_map:
            if key_map[key] is None:
                self.running = False
            else:
                self.current_exchange = key_map[key]

    def get_current_layout(self) -> Layout:
        """Get layout for current exchange."""
        if self.current_exchange == "polymarket":
            return create_polymarket_layout(self.pm_trades, self.pm_whales, self.pm_stats)
        elif self.current_exchange == "hyperliquid":
            return create_hyperliquid_layout(self.hl_trades, self.hl_whales, self.hl_stats)
        elif self.current_exchange == "all":
            return create_all_exchanges_layout(
                self.pm_trades, self.pm_whales, self.pm_stats,
                self.hl_trades, self.hl_whales, self.hl_stats
            )
        else:
            # Nado or Lighter - coming soon
            return create_coming_soon_layout(self.current_exchange, self.pm_stats)

    async def fetch_polymarket(self) -> None:
        """Fetch Polymarket data."""
        try:
            trades = await self.pm_client.get_trades(limit=500)  # Fetch max trades per poll

            for t in trades:
                if t.transaction_hash not in self.pm_seen_tx:
                    self.pm_seen_tx.add(t.transaction_hash)
                    self.pm_trades.insert(0, t)

                    if t.usdc_size >= 10000:
                        self.pm_whales.insert(0, t)

            # Keep lists bounded (high limits for full history)
            self.pm_trades = self.pm_trades[:10000]
            self.pm_whales = self.pm_whales[:1000]

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
                    trades = await self.hl_client.get_recent_trades(coin, limit=100)

                    for t in trades:
                        if t.tid not in self.hl_seen_tid:
                            self.hl_seen_tid.add(t.tid)
                            self.hl_trades.insert(0, t)

                            if t.notional_usd >= Decimal("10000000"):
                                self.hl_whales.insert(0, t)

                except Exception:
                    pass  # Skip failed coin fetches

            # Keep lists bounded (high limits for full history)
            self.hl_trades = self.hl_trades[:10000]
            self.hl_whales = self.hl_whales[:1000]

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
        """Run the CLI application with keyboard input."""
        console.print("[bold magenta]Starting PrintDAO CLI...[/]")
        console.print("[dim]Use P/H/N/L/A to switch tabs, Q to quit[/]\n")

        # Start keyboard handler
        self.keyboard.start()

        try:
            with Live(self.get_current_layout(), refresh_per_second=2, console=console) as live:
                while self.running:
                    # Fetch data for both exchanges
                    await asyncio.gather(
                        self.fetch_polymarket(),
                        self.fetch_hyperliquid(),
                    )

                    # Update display
                    live.update(self.get_current_layout())

                    # Short sleep for responsive keyboard handling
                    await asyncio.sleep(3)

        except KeyboardInterrupt:
            pass
        finally:
            self.keyboard.stop()
            await self.pm_client.close()
            console.print("\n[bold red]Shutting down...[/]")


async def run_single_exchange(exchange: str) -> None:
    """Run CLI for a single exchange (legacy mode)."""
    cli = PrintDAOCLI()
    cli.current_exchange = exchange

    console.print(f"[bold magenta]Starting PrintDAO CLI - {exchange.upper()}[/]")
    console.print("[dim]Use P/H/N/L/A to switch tabs, Q to quit[/]\n")

    await cli.run()


def run() -> None:
    """Run the CLI."""
    # Check for command line argument for exchange
    if len(sys.argv) > 1:
        exchange = sys.argv[1].lower()
        exchange_map = {
            "p": "polymarket", "polymarket": "polymarket", "pm": "polymarket",
            "h": "hyperliquid", "hyperliquid": "hyperliquid", "hl": "hyperliquid",
            "n": "nado", "nado": "nado",
            "l": "lighter", "lighter": "lighter",
            "a": "all", "all": "all",
        }

        if exchange in exchange_map:
            asyncio.run(run_single_exchange(exchange_map[exchange]))
        else:
            console.print(f"[red]Unknown exchange: {exchange}[/]")
            console.print("Usage: python -m cli.simple [polymarket|hyperliquid|nado|lighter|all]")
            sys.exit(1)
    else:
        # Default: run with all exchanges view
        cli = PrintDAOCLI()
        asyncio.run(cli.run())


if __name__ == "__main__":
    run()
