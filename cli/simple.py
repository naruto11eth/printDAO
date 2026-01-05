#!/usr/bin/env python3
"""Simple CLI for PrintDAO - Live terminal output with exchange tabs and search.

Usage:
    python -m cli.simple                    # Live dashboard (default)
    python -m cli.simple --pm-whales        # Polymarket whales
    python -m cli.simple --pm-whales ">50k" # PM whales above $50k
    python -m cli.simple --pm-trades        # Recent PM trades
    python -m cli.simple --pm-trades trump  # PM trades matching "trump"
    python -m cli.simple --hl-whales        # Hyperliquid whales
    python -m cli.simple --hl-whales btc    # HL whales for BTC
    python -m cli.simple --hl-trades BTC    # Recent BTC trades
    python -m cli.simple --hl-trades ETH ">100k"  # ETH trades above $100k
"""

import argparse
import asyncio
import sys
import os
import tty
import termios
import select
import re
from datetime import datetime
from decimal import Decimal
from threading import Thread
from typing import Optional, Callable

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
    """Non-blocking keyboard input handler with search mode support."""

    def __init__(self, on_key: Callable, on_search: Callable):
        self.on_key = on_key
        self.on_search = on_search
        self.running = False
        self.search_mode = False
        self.search_buffer = ""
        self._thread: Optional[Thread] = None
        self._old_settings = None

    def start(self) -> None:
        self.running = True
        self._thread = Thread(target=self._listen, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=0.5)

    def _listen(self) -> None:
        fd = sys.stdin.fileno()
        self._old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while self.running:
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    key = sys.stdin.read(1)
                    self._handle_key(key)
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, self._old_settings)
            except Exception:
                pass

    def _handle_key(self, key: str) -> None:
        if self.search_mode:
            if key == '\x1b':  # Escape
                self.search_mode = False
                self.search_buffer = ""
                self.on_search("")
            elif key == '\n' or key == '\r':  # Enter
                self.search_mode = False
                self.on_search(self.search_buffer)
            elif key == '\x7f':  # Backspace
                self.search_buffer = self.search_buffer[:-1]
                self.on_search(self.search_buffer)
            elif key.isprintable():
                self.search_buffer += key
                self.on_search(self.search_buffer)
        else:
            if key == '/':
                self.search_mode = True
                self.search_buffer = ""
            else:
                self.on_key(key.lower())


# ============== Filter Logic ==============

def parse_size_filter(query: str) -> tuple[Optional[float], Optional[float]]:
    """Parse size filter from query. Returns (min_size, max_size)."""
    # Match patterns like ">40k", "40k+", "<100k", "10k-50k"
    query = query.lower().replace(",", "").replace("$", "")

    # Convert k/m to numbers
    def parse_num(s: str) -> float:
        s = s.strip()
        if s.endswith('k'):
            return float(s[:-1]) * 1000
        elif s.endswith('m'):
            return float(s[:-1]) * 1000000
        return float(s)

    # >40k or 40k+
    match = re.match(r'^[>]?(\d+\.?\d*[km]?)\+?$', query)
    if match and (query.startswith('>') or query.endswith('+')):
        return (parse_num(match.group(1)), None)

    # <40k
    match = re.match(r'^<(\d+\.?\d*[km]?)$', query)
    if match:
        return (None, parse_num(match.group(1)))

    # 10k-50k
    match = re.match(r'^(\d+\.?\d*[km]?)-(\d+\.?\d*[km]?)$', query)
    if match:
        return (parse_num(match.group(1)), parse_num(match.group(2)))

    return (None, None)


def filter_pm_trades(trades: list[PMTrade], query: str) -> list[PMTrade]:
    """Filter Polymarket trades by market name or size."""
    if not query:
        return trades

    query_lower = query.lower()
    min_size, max_size = parse_size_filter(query)

    if min_size is not None or max_size is not None:
        return [t for t in trades if
                (min_size is None or t.usdc_size >= min_size) and
                (max_size is None or t.usdc_size <= max_size)]

    # Text search in market title
    return [t for t in trades if query_lower in t.title.lower() or query_lower in t.outcome.lower()]


def filter_hl_trades(trades: list[HLTrade], query: str) -> list[HLTrade]:
    """Filter Hyperliquid trades by coin or size."""
    if not query:
        return trades

    query_lower = query.lower()
    min_size, max_size = parse_size_filter(query)

    if min_size is not None or max_size is not None:
        return [t for t in trades if
                (min_size is None or float(t.notional_usd) >= min_size) and
                (max_size is None or float(t.notional_usd) <= max_size)]

    # Text search in coin name
    return [t for t in trades if query_lower in t.coin.lower()]


# ============== Polymarket Tables ==============

def create_pm_trades_table(trades: list[PMTrade], title: str = "Recent Trades") -> Table:
    """Create Polymarket trades table with full market name and PNL."""
    table = Table(title=title, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Side", width=4)
    table.add_column("Size", justify="right", width=10)
    table.add_column("Market", overflow="ellipsis")  # Expanded - no fixed width
    table.add_column("Position", width=10, overflow="ellipsis")
    table.add_column("Price", justify="right", width=6)
    table.add_column("PNL", justify="right", width=10)

    for t in trades:
        side_style = "green" if t.side.value == "BUY" else "red"
        # PNL: potential profit if position wins
        # BUY: profit = size * (1 - price) if outcome wins
        # SELL: profit = size * price if outcome loses
        if t.side.value == "BUY":
            pnl = t.usdc_size * (1 - t.price) / t.price if t.price > 0 else 0
            pnl_style = "green"
        else:
            pnl = t.usdc_size * t.price / (1 - t.price) if t.price < 1 else 0
            pnl_style = "red"

        table.add_row(
            Text(t.side.value, style=side_style),
            f"${t.usdc_size:,.0f}",
            t.title,  # Full market name
            t.outcome[:10],
            f"{t.price*100:.0f}%",
            Text(f"${pnl:,.0f}", style=pnl_style),
        )

    return table


# ============== Hyperliquid Tables ==============

def create_hl_trades_table(trades: list[HLTrade], title: str = "Recent Trades") -> Table:
    """Create Hyperliquid trades table with PNL indicator."""
    table = Table(title=title, show_header=True, header_style="bold cyan", expand=True)
    table.add_column("Coin", width=6)
    table.add_column("Side", width=5)
    table.add_column("Size", justify="right", width=12)
    table.add_column("Price", justify="right", width=12)
    table.add_column("Notional", justify="right", width=14)

    for t in trades:
        is_buy = t.side.value == "B"
        side_text = "LONG" if is_buy else "SHORT"
        side_style = "green" if is_buy else "red"

        table.add_row(
            t.coin,
            Text(side_text, style=side_style),
            f"{t.size:,.4f}",
            f"${t.price:,.2f}",
            f"${t.notional_usd:,.0f}",
        )

    return table


# ============== Whale Tables ==============

def create_combined_whales_table(pm_whales: list[PMTrade], hl_whales: list[HLTrade]) -> tuple[Table, Table]:
    """Create combined whale tables for both exchanges."""
    # Polymarket whales
    pm_table = Table(title="Polymarket Whales ($10k+)", show_header=True, header_style="bold yellow", expand=True)
    pm_table.add_column("Time", style="dim", width=8)
    pm_table.add_column("Side", width=4)
    pm_table.add_column("Size", justify="right", style="bold yellow", width=10)
    pm_table.add_column("Market", overflow="ellipsis")
    pm_table.add_column("Pos", width=8)
    pm_table.add_column("Price", justify="right", width=6)

    for t in pm_whales[:50]:
        time_str = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")
        side_style = "green" if t.side.value == "BUY" else "red"
        pm_table.add_row(
            time_str,
            Text(t.side.value, side_style),
            f"${t.usdc_size:,.0f}",
            t.title[:40],
            t.outcome[:8],
            f"{t.price*100:.0f}%",
        )

    # Hyperliquid whales
    hl_table = Table(title="Hyperliquid Whales ($1M+)", show_header=True, header_style="bold yellow", expand=True)
    hl_table.add_column("Time", style="dim", width=8)
    hl_table.add_column("Coin", width=6)
    hl_table.add_column("Side", width=5)
    hl_table.add_column("Notional", justify="right", style="bold yellow", width=14)
    hl_table.add_column("Price", justify="right", width=12)

    for t in hl_whales[:50]:
        time_str = t.datetime.strftime("%H:%M:%S")
        is_buy = t.side.value == "B"
        side_text = "LONG" if is_buy else "SHORT"
        side_style = "green" if is_buy else "red"
        hl_table.add_row(
            time_str,
            t.coin,
            Text(side_text, style=side_style),
            f"${t.notional_usd:,.0f}",
            f"${t.price:,.2f}",
        )

    return pm_table, hl_table


# ============== Layout Builders ==============

def create_header_text(current_exchange: str, stats: dict, search_query: str = "", search_mode: bool = False) -> Text:
    """Create header text with tab indicators and search."""
    header_text = Text()
    header_text.append("PRINTDAO", style="bold magenta")
    header_text.append(" | ", style="dim")

    tabs = [
        ("P", "polymarket", "green"),
        ("H", "hyperliquid", "blue"),
        ("W", "whales", "yellow"),
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

    # Search indicator
    if search_mode:
        header_text.append(f" | Search: {search_query}_", style="bold yellow")
    elif search_query:
        header_text.append(f" | Filter: {search_query}", style="yellow")
        header_text.append(" [Esc clear]", style="dim")

    return header_text


def create_footer_text(search_mode: bool = False) -> Text:
    """Create footer with keyboard shortcuts."""
    if search_mode:
        return Text("Type to search | Enter to apply | Esc to cancel", style="bold yellow")
    return Text(
        "[P] Polymarket | [H] Hyperliquid | [W] Whales | [A] All | [/] Search | [Q] Quit",
        style="dim"
    )


def create_polymarket_layout(trades: list, stats: dict, search_query: str = "", search_mode: bool = False) -> Layout:
    """Create Polymarket layout - trades only, full width."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )

    filtered = filter_pm_trades(trades, search_query)
    title = f"Recent Trades ({len(filtered)}/{len(trades)})" if search_query else "Recent Trades"

    layout["header"].update(Panel(create_header_text("polymarket", stats, search_query, search_mode), style="bold"))
    layout["main"].update(Panel(create_pm_trades_table(filtered, title), border_style="green"))
    layout["footer"].update(Panel(create_footer_text(search_mode)))

    return layout


def create_hyperliquid_layout(trades: list, stats: dict, search_query: str = "", search_mode: bool = False) -> Layout:
    """Create Hyperliquid layout - trades only, full width."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )

    filtered = filter_hl_trades(trades, search_query)
    title = f"Recent Trades ({len(filtered)}/{len(trades)})" if search_query else "Recent Trades"

    layout["header"].update(Panel(create_header_text("hyperliquid", stats, search_query, search_mode), style="bold"))
    layout["main"].update(Panel(create_hl_trades_table(filtered, title), border_style="blue"))
    layout["footer"].update(Panel(create_footer_text(search_mode)))

    return layout


def create_whales_layout(pm_whales: list, hl_whales: list, stats: dict, search_mode: bool = False) -> Layout:
    """Create combined whales layout."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )

    layout["main"].split_row(
        Layout(name="pm_whales"),
        Layout(name="hl_whales"),
    )

    pm_table, hl_table = create_combined_whales_table(pm_whales, hl_whales)

    layout["header"].update(Panel(create_header_text("whales", stats, "", search_mode), style="bold"))
    layout["pm_whales"].update(Panel(pm_table, border_style="green"))
    layout["hl_whales"].update(Panel(hl_table, border_style="blue"))
    layout["footer"].update(Panel(create_footer_text(search_mode)))

    return layout


def create_all_exchanges_layout(
    pm_trades: list, hl_trades: list, stats: dict,
    search_query: str = "", search_mode: bool = False
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

    pm_filtered = filter_pm_trades(pm_trades, search_query)
    hl_filtered = filter_hl_trades(hl_trades, search_query)

    layout["header"].update(Panel(create_header_text("all", stats, search_query, search_mode), style="bold"))
    layout["polymarket"].update(Panel(create_pm_trades_table(pm_filtered, "Polymarket"), border_style="green"))
    layout["hyperliquid"].update(Panel(create_hl_trades_table(hl_filtered, "Hyperliquid"), border_style="blue"))
    layout["footer"].update(Panel(create_footer_text(search_mode)))

    return layout


# ============== Main App ==============

class PrintDAOCLI:
    """Main CLI application with keyboard tab switching and search."""

    EXCHANGES = ["polymarket", "hyperliquid", "whales", "all"]

    def __init__(self):
        self.current_exchange = "all"
        self.running = True
        self.search_query = ""
        self.search_mode = False

        # Polymarket data
        self.pm_client = PolymarketClient()
        self.pm_trades: list[PMTrade] = []
        self.pm_whales: list[PMTrade] = []
        self.pm_seen_tx: set[str] = set()
        self.pm_stats = {"total_trades": 0, "total_volume": 0, "whale_count": 0}

        # Hyperliquid data
        self.hl_client = HyperliquidClient()
        self.hl_trades: list[HLTrade] = []
        self.hl_whales: list[HLTrade] = []
        self.hl_seen_tid: set[str] = set()
        self.hl_stats = {"total_trades": 0, "total_volume": 0, "whale_count": 0}

        # Top coins to monitor
        self.hl_coins = ["BTC", "ETH", "SOL", "DOGE", "WIF", "PEPE", "ARB", "SUI", "AVAX", "LINK"]

        # Keyboard handler
        self.keyboard = KeyboardHandler(self._on_keypress, self._on_search)

    def _on_keypress(self, key: str) -> None:
        """Handle keyboard input."""
        key_map = {
            'p': 'polymarket',
            'h': 'hyperliquid',
            'w': 'whales',
            'a': 'all',
            'q': None,
        }

        if key == '\x1b':  # Escape - clear search
            self.search_query = ""
        elif key in key_map:
            if key_map[key] is None:
                self.running = False
            else:
                self.current_exchange = key_map[key]

    def _on_search(self, query: str) -> None:
        """Handle search query updates."""
        self.search_query = query
        self.search_mode = self.keyboard.search_mode

    def get_current_layout(self) -> Layout:
        """Get layout for current exchange."""
        combined_stats = {
            "total_trades": len(self.pm_trades) + len(self.hl_trades),
            "total_volume": self.pm_stats.get("total_volume", 0) + self.hl_stats.get("total_volume", 0),
        }

        if self.current_exchange == "polymarket":
            return create_polymarket_layout(self.pm_trades, self.pm_stats, self.search_query, self.search_mode)
        elif self.current_exchange == "hyperliquid":
            return create_hyperliquid_layout(self.hl_trades, self.hl_stats, self.search_query, self.search_mode)
        elif self.current_exchange == "whales":
            return create_whales_layout(self.pm_whales, self.hl_whales, combined_stats, self.search_mode)
        else:
            return create_all_exchanges_layout(self.pm_trades, self.hl_trades, combined_stats, self.search_query, self.search_mode)

    async def fetch_polymarket(self) -> None:
        """Fetch Polymarket data."""
        try:
            trades = await self.pm_client.get_trades(limit=500)

            for t in trades:
                if t.transaction_hash not in self.pm_seen_tx:
                    self.pm_seen_tx.add(t.transaction_hash)
                    self.pm_trades.insert(0, t)
                    if t.usdc_size >= 10000:
                        self.pm_whales.insert(0, t)

            self.pm_trades = self.pm_trades[:10000]
            self.pm_whales = self.pm_whales[:1000]

            if self.pm_trades:
                self.pm_stats = {
                    "total_trades": len(self.pm_trades),
                    "total_volume": sum(t.usdc_size for t in self.pm_trades),
                    "whale_count": len(self.pm_whales),
                }
        except Exception:
            pass

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
                            if t.notional_usd >= Decimal("1000000"):  # $1M for HL whales
                                self.hl_whales.insert(0, t)
                except Exception:
                    pass

            self.hl_trades = self.hl_trades[:10000]
            self.hl_whales = self.hl_whales[:1000]
            self.hl_trades.sort(key=lambda t: t.time_ms, reverse=True)
            self.hl_whales.sort(key=lambda t: t.time_ms, reverse=True)

            if self.hl_trades:
                self.hl_stats = {
                    "total_trades": len(self.hl_trades),
                    "total_volume": float(sum(t.notional_usd for t in self.hl_trades)),
                    "whale_count": len(self.hl_whales),
                }
        except Exception:
            pass

    async def run(self) -> None:
        """Run the CLI application."""
        console.print("[bold magenta]Starting PrintDAO CLI...[/]")
        console.print("[dim]P/H/W/A to switch tabs | / to search | Q to quit[/]\n")

        self.keyboard.start()

        try:
            with Live(self.get_current_layout(), refresh_per_second=4, console=console) as live:
                while self.running:
                    await asyncio.gather(
                        self.fetch_polymarket(),
                        self.fetch_hyperliquid(),
                    )
                    live.update(self.get_current_layout())
                    await asyncio.sleep(2)
        except KeyboardInterrupt:
            pass
        finally:
            self.keyboard.stop()
            await self.pm_client.close()
            console.print("\n[bold red]Shutting down...[/]")


# ============== One-off Commands ==============

async def cmd_pm_whales(filter_str: Optional[str] = None) -> None:
    """Show Polymarket whale trades."""
    client = PolymarketClient()

    try:
        console.print("[bold cyan]Fetching Polymarket whale trades...[/]")
        trades = await client.get_trades(limit=500)

        # Filter for whales (>$10k default)
        min_size = 10000.0
        max_size = None
        text_filter = None

        if filter_str:
            min_s, max_s = parse_size_filter(filter_str)
            if min_s is not None or max_s is not None:
                if min_s:
                    min_size = min_s
                if max_s:
                    max_size = max_s
            else:
                text_filter = filter_str

        # Apply filters
        filtered = [t for t in trades if t.usdc_size >= min_size]
        if max_size:
            filtered = [t for t in filtered if t.usdc_size <= max_size]
        if text_filter:
            text_lower = text_filter.lower()
            filtered = [t for t in filtered if text_lower in t.title.lower() or text_lower in t.outcome.lower()]

        if not filtered:
            console.print("[yellow]No whale trades found matching filter[/]")
            return

        # Create table
        table = Table(title=f"Polymarket Whales ({len(filtered)} trades)", show_header=True, header_style="bold cyan")
        table.add_column("Time", style="dim", width=8)
        table.add_column("Side", width=4)
        table.add_column("Size", justify="right", style="bold yellow", width=10)
        table.add_column("Market")
        table.add_column("Position", width=10)
        table.add_column("Price", justify="right", width=6)
        table.add_column("PNL", justify="right", width=10)

        for t in filtered[:30]:
            time_str = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")
            side_style = "green" if t.side.value == "BUY" else "red"

            if t.side.value == "BUY":
                pnl = t.usdc_size * (1 - t.price) / t.price if t.price > 0 else 0
                pnl_style = "green"
            else:
                pnl = t.usdc_size * t.price / (1 - t.price) if t.price < 1 else 0
                pnl_style = "red"

            table.add_row(
                time_str,
                Text(t.side.value, style=side_style),
                f"${t.usdc_size:,.0f}",
                t.title[:40],
                t.outcome[:10],
                f"{t.price*100:.0f}%",
                Text(f"${pnl:,.0f}", style=pnl_style),
            )

        console.print(table)

    finally:
        await client.close()


async def cmd_pm_trades(filter_str: Optional[str] = None) -> None:
    """Show recent Polymarket trades."""
    client = PolymarketClient()

    try:
        console.print("[bold cyan]Fetching Polymarket trades...[/]")
        trades = await client.get_trades(limit=500)

        # Apply filter
        filtered = filter_pm_trades(trades, filter_str or "")

        if not filtered:
            console.print("[yellow]No trades found matching filter[/]")
            return

        table = create_pm_trades_table(filtered[:30], f"Polymarket Trades ({len(filtered)} found)")
        console.print(table)

    finally:
        await client.close()


async def cmd_hl_whales(filter_str: Optional[str] = None) -> None:
    """Show Hyperliquid whale trades."""
    client = HyperliquidClient()

    console.print("[bold cyan]Fetching Hyperliquid whale trades...[/]")

    # Parse filter
    min_size = 1000000.0  # $1M default
    max_size = None
    coin_filter = None

    if filter_str:
        min_s, max_s = parse_size_filter(filter_str)
        if min_s is not None or max_s is not None:
            if min_s:
                min_size = min_s
            if max_s:
                max_size = max_s
        else:
            coin_filter = filter_str.upper()

    # Fetch whale trades
    trades = await client.get_whale_trades(threshold=Decimal(str(min_size)), limit=50)

    # Apply additional filters
    if max_size:
        trades = [t for t in trades if float(t.notional_usd) <= max_size]
    if coin_filter:
        trades = [t for t in trades if coin_filter in t.coin.upper()]

    if not trades:
        console.print("[yellow]No whale trades found matching filter[/]")
        return

    # Create table
    table = Table(title=f"Hyperliquid Whales ({len(trades)} trades)", show_header=True, header_style="bold cyan")
    table.add_column("Time", style="dim", width=8)
    table.add_column("Coin", width=6)
    table.add_column("Side", width=5)
    table.add_column("Notional", justify="right", style="bold yellow", width=14)
    table.add_column("Size", justify="right", width=12)
    table.add_column("Price", justify="right", width=12)

    for t in trades[:30]:
        time_str = t.datetime.strftime("%H:%M:%S")
        is_buy = t.side.value == "B"
        side_text = "LONG" if is_buy else "SHORT"
        side_style = "green" if is_buy else "red"

        table.add_row(
            time_str,
            t.coin,
            Text(side_text, style=side_style),
            f"${t.notional_usd:,.0f}",
            f"{t.size:,.4f}",
            f"${t.price:,.2f}",
        )

    console.print(table)


async def cmd_hl_trades(coin: str, filter_str: Optional[str] = None) -> None:
    """Show recent Hyperliquid trades for a coin."""
    client = HyperliquidClient()

    console.print(f"[bold cyan]Fetching {coin.upper()} trades...[/]")

    # Parse size filter
    min_size, max_size = None, None
    if filter_str:
        min_size, max_size = parse_size_filter(filter_str)

    # Fetch trades
    fetch_limit = 200 if min_size or max_size else 50
    trades = await client.get_recent_trades(coin.upper(), limit=fetch_limit)

    # Apply filters
    if min_size:
        trades = [t for t in trades if float(t.notional_usd) >= min_size]
    if max_size:
        trades = [t for t in trades if float(t.notional_usd) <= max_size]

    if not trades:
        console.print("[yellow]No trades found matching filter[/]")
        return

    table = create_hl_trades_table(trades[:30], f"{coin.upper()} Trades ({len(trades)} found)")
    console.print(table)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="PrintDAO CLI - Live whale tracking for Polymarket and Hyperliquid",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                        Live dashboard (default)
  %(prog)s --pm-whales            Polymarket whales (>$10k)
  %(prog)s --pm-whales ">50k"     PM whales above $50k
  %(prog)s --pm-whales trump      PM whales matching "trump"
  %(prog)s --pm-trades            Recent PM trades
  %(prog)s --pm-trades ">40k"     PM trades above $40k
  %(prog)s --hl-whales            Hyperliquid whales (>$1M)
  %(prog)s --hl-whales btc        HL whales for BTC
  %(prog)s --hl-trades BTC        Recent BTC trades
  %(prog)s --hl-trades ETH ">100k" ETH trades above $100k

Filter syntax:
  >40k or 40k+    Above $40k
  <100k           Below $100k
  10k-50k         Between $10k-$50k
  btc / trump     Text search
        """
    )

    parser.add_argument(
        "--pm-whales",
        nargs="?",
        const="",
        metavar="FILTER",
        help="Show Polymarket whale trades (optional filter)"
    )
    parser.add_argument(
        "--pm-trades",
        nargs="?",
        const="",
        metavar="FILTER",
        help="Show recent Polymarket trades (optional filter)"
    )
    parser.add_argument(
        "--hl-whales",
        nargs="?",
        const="",
        metavar="FILTER",
        help="Show Hyperliquid whale trades (optional filter)"
    )
    parser.add_argument(
        "--hl-trades",
        nargs="+",
        metavar=("COIN", "FILTER"),
        help="Show Hyperliquid trades for COIN (optional size filter)"
    )

    return parser.parse_args()


def run() -> None:
    """Run the CLI."""
    args = parse_args()

    # Check for one-off commands
    if args.pm_whales is not None:
        asyncio.run(cmd_pm_whales(args.pm_whales or None))
    elif args.pm_trades is not None:
        asyncio.run(cmd_pm_trades(args.pm_trades or None))
    elif args.hl_whales is not None:
        asyncio.run(cmd_hl_whales(args.hl_whales or None))
    elif args.hl_trades is not None:
        coin = args.hl_trades[0]
        filter_str = args.hl_trades[1] if len(args.hl_trades) > 1 else None
        asyncio.run(cmd_hl_trades(coin, filter_str))
    else:
        # Default: live dashboard
        cli = PrintDAOCLI()
        asyncio.run(cli.run())


if __name__ == "__main__":
    run()
