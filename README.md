# PrintDAO

A multi-exchange whale tracking platform with both CLI and Telegram interfaces.

## Features

- **Live CLI Dashboard** - Real-time terminal UI showing trades and whale alerts
- **Telegram Bot** - Track wallets and get alerts in any Telegram group
- **Multi-Exchange** - Supports Hyperliquid (perps) and Polymarket (prediction markets)
- **Wallet Tracking** - Track any wallet address and get alerts when they trade
- **Whale Alerts** - Automatic alerts for large trades ($10k+ on Polymarket)
- **PnL Tracking** - View performance metrics for any wallet
- **Wallet Groups** - Organize tracked wallets into groups
- **Trader Analytics** - TrySuper.co-style metrics (Profit Factor, ROE, Win Rate, Max Drawdown)
- **Copy Trading Signals** - Get signals when tracked wallets trade
- **Configurable Whale Detection** - Customize thresholds per user
- **Rate Limiting** - Built-in protection against API abuse

## Quick Start

### Prerequisites

- Python 3.11+
- Docker (for Redis)
- Telegram Bot Token (from [@BotFather](https://t.me/botfather))

### 1. Clone and Setup

```bash
cd /Users/naruto/Desktop/web3_projects/printdao

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# Copy example config
cp .env.example .env

# Edit .env with your bot token
# TELEGRAM_BOT_TOKEN=your_token_here
```

### 3. Start Redis

**Option A: Using Docker (recommended)**
```bash
# Make sure Docker Desktop is running first!
# Then start Redis:
docker-compose up -d redis

# Verify it's running
docker-compose ps
```

**Option B: Install Redis directly on Mac**
```bash
# Using Homebrew
brew install redis
brew services start redis

# Verify it's running
redis-cli ping  # Should return PONG
```

### 4. Run the CLI

**Live Dashboard (default)**
```bash
python -m cli.simple
```

Keyboard controls:
- `P` - Polymarket tab
- `H` - Hyperliquid tab
- `W` - Whales tab (both exchanges)
- `A` - All exchanges
- `/` - Search/filter mode
- `↑/↓` - Scroll one row
- `←/→` - Scroll one page (10 rows)
- `Q` - Quit

**Single Exchange Mode**
```bash
python -m cli.simple --hl    # Hyperliquid only
python -m cli.simple --pm    # Polymarket only
```

**One-off Commands**
```bash
# Polymarket
python -m cli.simple --pm-whales            # Whales >$10k
python -m cli.simple --pm-whales ">50k"     # Above $50k
python -m cli.simple --pm-whales trump      # Filter by market name
python -m cli.simple --pm-trades            # Recent trades
python -m cli.simple --pm-trades bitcoin    # Filter by text

# Hyperliquid
python -m cli.simple --hl-whales            # Whales >$1M
python -m cli.simple --hl-whales btc        # BTC whales only
python -m cli.simple --hl-trades BTC        # Recent BTC trades
python -m cli.simple --hl-trades ETH ">100k" # ETH trades >$100k

# Help
python -m cli.simple --help
```

Press `Ctrl+C` to exit.

### 5. Run the Telegram Bot (Optional)

```bash
# Run the Telegram bot
python -m bot.main
```

You should see:
```
Bot started successfully!
Bot username: @your_bot_name
Add the bot to your Telegram group to start tracking whales!
```

Then:
1. Add your bot to a Telegram group
2. Send `/start` to see the welcome message
3. Send `/help` to see all commands

## Commands

### Filter Syntax

Both CLI and Telegram support the same filter syntax:

```
>40k or 40k+    Above $40k
<100k           Below $100k
10k-50k         Between $10k-$50k
btc / trump     Text search (coin or market name)
btc >500k       Combined filters
```

### Telegram Bot Commands

**Wallet Tracking**
```
/track <address> [label]       - Start tracking a wallet
/untrack <address|label>       - Stop tracking a wallet
/list                          - List your tracked wallets
```

**Wallet Groups**
```
/creategroup <name> [desc]     - Create a wallet group
/groups                        - List your groups
/groupinfo <name>              - Show group details
/addtogroup <group> <wallet>   - Add wallet to group
/deletegroup <name>            - Delete a group
```

**Polymarket**
```
/pm_positions <address>        - Show positions
/pm_pnl <address> [period]     - Show PnL (day|week|month|all)
/pm_trades <address> [hours]   - Recent trades for address
/pm_whales [filter]            - Whale trades with optional filter
/pm_indexed [filter]           - Indexed trades from Redis
/pm_subscribe                  - Subscribe to whale alerts
/pm_unsubscribe                - Unsubscribe from alerts
```

**Hyperliquid**
```
/hl_positions <address>        - Show positions
/hl_pnl <address>              - Show PnL
/hl_trades <coin> [filter]     - Recent trades with optional filter
/hl_whales [filter]            - Whale trades with optional filter
/hl_indexed [filter]           - Indexed trades from Redis
/hl_sentiment <coin>           - Market sentiment
/hl_market <coin>              - Market info
/hl_coins                      - List available coins
/hl_fills <address>            - Recent fills for address
/hl_analytics <address>        - Trader analytics (TrySuper.co style)
```

**Settings & Configuration**
```
/settings                      - View all settings
/whale_config                  - Configure whale detection thresholds
/setinterval <exchange> <sec>  - Set monitoring interval
/intervals                     - View current intervals
/notify <all|whale_only|none>  - Set notification preferences
/mute <minutes>                - Mute notifications temporarily
/unmute                        - Unmute notifications
```

**Filter Examples**
```
/pm_whales >50k                - PM whales above $50k
/pm_whales trump               - PM whales matching "trump"
/pm_indexed 10k-100k           - PM trades between $10k-$100k
/hl_whales btc                 - HL whales for BTC
/hl_trades ETH >100k           - ETH trades above $100k
/hl_indexed sol                - Indexed SOL trades
```

### CLI Commands

**Live Dashboard**
```bash
python -m cli.simple           # Live dashboard with tabs
```

**One-off Queries**
```bash
python -m cli.simple --pm-whales [filter]
python -m cli.simple --pm-trades [filter]
python -m cli.simple --hl-whales [filter]
python -m cli.simple --hl-trades <coin> [filter]
```

## Project Structure

```
printdao/
├── cli/
│   ├── simple.py            # Live terminal dashboard
│   └── app.py               # Full TUI app (textual)
├── bot/
│   ├── main.py              # Telegram bot entry point
│   ├── config.py            # Configuration
│   ├── routers/
│   │   ├── general.py       # /start, /help commands
│   │   ├── tracking.py      # /track, /untrack, wallet groups
│   │   ├── polymarket.py    # /pm_* commands
│   │   ├── hyperliquid.py   # /hl_* commands
│   │   └── settings.py      # /settings, /whale_config
│   ├── services/
│   │   ├── analytics_service.py    # Trader metrics calculations
│   │   ├── copy_trading_service.py # Copy trading signals
│   │   ├── wallet_service.py       # Wallet tracking
│   │   └── notification_service.py # Alert delivery
│   ├── keyboards/           # Inline keyboard builders
│   ├── middlewares/         # Rate limiting, throttling
│   ├── schedulers/          # Background job scheduling
│   └── storage/             # Redis client & models
├── indexers/                # Exchange integrations
│   ├── hyperliquid/
│   └── polymarket/
├── shared/                  # Shared utilities
├── tests/                   # Test files
├── docker-compose.yml       # Docker services
├── requirements.txt         # Python dependencies
└── .env                     # Configuration
```

## Development

### Running Tests

```bash
pytest tests/
```

### Docker Deployment

```bash
# Build and run everything
docker-compose up -d

# View logs
docker-compose logs -f bot

# Stop
docker-compose down
```

## Troubleshooting

### Redis Connection Error

Make sure Redis is running:
```bash
docker-compose up -d redis
docker-compose ps  # Should show "Up"
```

### Bot Not Responding

1. Check the bot token is correct in `.env`
2. Make sure the bot was added to the group
3. Check logs: `docker-compose logs bot`

### Permission Errors in Groups

Make sure the bot has permission to send messages in the group.

## License

MIT
