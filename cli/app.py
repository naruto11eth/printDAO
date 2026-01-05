"""PrintDAO CLI - Terminal interface for whale tracking."""

import asyncio
from datetime import datetime
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import Header, Footer, Static, Label, DataTable, TabbedContent, TabPane, Button, Input
from textual.reactive import reactive
from textual import work

from indexers.polymarket import PolymarketClient, Trade, Position


class TradeCard(Static):
    """A card displaying a single trade."""

    def __init__(self, trade: Trade, **kwargs):
        super().__init__(**kwargs)
        self.trade = trade

    def compose(self) -> ComposeResult:
        t = self.trade
        side_class = "buy" if t.side.value == "BUY" else "sell"
        trader = t.pseudonym or t.name or f"{t.proxy_wallet[:10]}..."
        time_str = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")

        content = f"""[bold {side_class}]{t.side.value}[/] ${t.usdc_size:,.0f}
{t.title[:45]}{'...' if len(t.title) > 45 else ''}
{t.outcome} @ {t.price*100:.1f}%
by {trader} | {time_str}"""

        yield Static(content, classes="trade-content")


class LiveTradesPanel(Static):
    """Panel showing live trades stream."""

    trades: reactive[list] = reactive([], always_update=True)

    def compose(self) -> ComposeResult:
        yield Static("[bold]Live Trades[/]", classes="panel-title")
        yield ScrollableContainer(id="trades-container")

    def watch_trades(self, trades: list) -> None:
        """Update display when trades change."""
        container = self.query_one("#trades-container")
        container.remove_children()

        for trade in trades[:20]:  # Show last 20
            t = trade
            side = "[green]BUY[/]" if t.side.value == "BUY" else "[red]SELL[/]"
            trader = t.pseudonym or t.name or f"{t.proxy_wallet[:8]}..."
            time_str = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")

            text = f"{side} [bold]${t.usdc_size:,.0f}[/] | {t.title[:35]}... | {t.outcome} @ {t.price*100:.1f}% | {trader} | {time_str}"
            container.mount(Static(text, classes="trade-item"))


class WhaleAlertsPanel(Static):
    """Panel showing whale alerts."""

    whales: reactive[list] = reactive([], always_update=True)

    def compose(self) -> ComposeResult:
        yield Static("[bold yellow]🐋 Whale Alerts ($10k+)[/]", classes="panel-title")
        yield ScrollableContainer(id="whales-container")

    def watch_whales(self, whales: list) -> None:
        """Update display when whales change."""
        container = self.query_one("#whales-container")
        container.remove_children()

        for trade in whales[:15]:
            t = trade
            side = "[green]BUY[/]" if t.side.value == "BUY" else "[red]SELL[/]"
            trader = t.pseudonym or t.name or f"{t.proxy_wallet[:8]}..."
            time_str = datetime.fromtimestamp(t.timestamp).strftime("%H:%M:%S")

            text = f"🐋 {side} [bold yellow]${t.usdc_size:,.0f}[/]\n   {t.title[:40]}...\n   {t.outcome} @ {t.price*100:.1f}% | {trader}"
            container.mount(Static(text, classes="whale-item"))


class StatsPanel(Static):
    """Panel showing overall stats."""

    stats: reactive[dict] = reactive({}, always_update=True)

    def compose(self) -> ComposeResult:
        yield Static("[bold]Stats[/]", classes="panel-title")
        yield Static("Loading...", id="stats-content")

    def watch_stats(self, stats: dict) -> None:
        """Update stats display."""
        if not stats:
            return

        content = self.query_one("#stats-content")
        text = f"""[bold]Total Trades:[/] {stats.get('total_trades', 0)}
[bold]Total Volume:[/] ${stats.get('total_volume', 0):,.0f}
[bold]Whale Trades:[/] {stats.get('whale_count', 0)}
[bold]Avg Size:[/] ${stats.get('avg_size', 0):,.0f}
[bold]Last Update:[/] {stats.get('last_update', 'N/A')}"""
        content.update(text)


class PrintDAOApp(App):
    """PrintDAO Terminal Application."""

    CSS = """
    Screen {
        layout: grid;
        grid-size: 2 2;
        grid-gutter: 1;
    }

    #trades-panel {
        column-span: 1;
        row-span: 2;
        border: solid green;
        padding: 1;
    }

    #whales-panel {
        border: solid yellow;
        padding: 1;
    }

    #stats-panel {
        border: solid blue;
        padding: 1;
    }

    .panel-title {
        text-style: bold;
        padding-bottom: 1;
    }

    .trade-item {
        padding: 0 1;
        margin-bottom: 1;
    }

    .whale-item {
        padding: 0 1;
        margin-bottom: 1;
        background: $surface;
    }

    TabbedContent {
        height: 100%;
    }

    TabPane {
        padding: 1;
    }

    ScrollableContainer {
        height: 100%;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("p", "switch_polymarket", "Polymarket"),
        ("h", "switch_hyperliquid", "Hyperliquid"),
    ]

    def __init__(self):
        super().__init__()
        self.client = PolymarketClient()
        self.all_trades: list[Trade] = []
        self.whale_trades: list[Trade] = []
        self.seen_tx: set[str] = set()
        self.stats = {
            "total_trades": 0,
            "total_volume": 0,
            "whale_count": 0,
            "avg_size": 0,
            "last_update": "Never",
        }

    def compose(self) -> ComposeResult:
        yield Header()
        yield Container(
            TabbedContent(
                TabPane("Polymarket", id="polymarket-tab"),
                TabPane("Hyperliquid (Coming Soon)", id="hyperliquid-tab"),
            ),
        )
        yield Footer()

    def on_mount(self) -> None:
        """Start background tasks when app mounts."""
        self.title = "PrintDAO - Whale Tracker"
        self.sub_title = "Live Market Activity"

        # Build Polymarket tab content
        polymarket_tab = self.query_one("#polymarket-tab")
        polymarket_tab.mount(
            Horizontal(
                LiveTradesPanel(id="trades-panel"),
                Vertical(
                    WhaleAlertsPanel(id="whales-panel"),
                    StatsPanel(id="stats-panel"),
                ),
            )
        )

        # Start fetching data
        self.fetch_trades()

    @work(exclusive=True)
    async def fetch_trades(self) -> None:
        """Fetch and update trades continuously."""
        while True:
            try:
                # Fetch recent trades
                trades = await self.client.get_trades(min_size=1000, limit=50)

                new_trades = []
                for t in trades:
                    if t.transaction_hash not in self.seen_tx:
                        self.seen_tx.add(t.transaction_hash)
                        new_trades.append(t)
                        self.all_trades.insert(0, t)

                        if t.usdc_size >= 10000:
                            self.whale_trades.insert(0, t)

                # Keep lists bounded
                self.all_trades = self.all_trades[:100]
                self.whale_trades = self.whale_trades[:50]

                # Update stats
                if self.all_trades:
                    self.stats = {
                        "total_trades": len(self.all_trades),
                        "total_volume": sum(t.usdc_size for t in self.all_trades),
                        "whale_count": len(self.whale_trades),
                        "avg_size": sum(t.usdc_size for t in self.all_trades) / len(self.all_trades),
                        "last_update": datetime.now().strftime("%H:%M:%S"),
                    }

                # Update UI
                self.call_from_thread(self._update_ui)

                await asyncio.sleep(5)  # Poll every 5 seconds

            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
                await asyncio.sleep(10)

    def _update_ui(self) -> None:
        """Update UI components with new data."""
        try:
            trades_panel = self.query_one("#trades-panel", LiveTradesPanel)
            trades_panel.trades = self.all_trades.copy()

            whales_panel = self.query_one("#whales-panel", WhaleAlertsPanel)
            whales_panel.whales = self.whale_trades.copy()

            stats_panel = self.query_one("#stats-panel", StatsPanel)
            stats_panel.stats = self.stats.copy()
        except Exception:
            pass

    def action_refresh(self) -> None:
        """Manual refresh."""
        self.fetch_trades()
        self.notify("Refreshing...")

    def action_switch_polymarket(self) -> None:
        """Switch to Polymarket tab."""
        tabs = self.query_one(TabbedContent)
        tabs.active = "polymarket-tab"

    def action_switch_hyperliquid(self) -> None:
        """Switch to Hyperliquid tab."""
        tabs = self.query_one(TabbedContent)
        tabs.active = "hyperliquid-tab"


def run():
    """Run the CLI app."""
    app = PrintDAOApp()
    app.run()


if __name__ == "__main__":
    run()
