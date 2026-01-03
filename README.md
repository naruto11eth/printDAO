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

### 4. Run the CLI (Live Dashboard)

```bash
# Run Polymarket dashboard (default)
python -m cli.simple

# Run Hyperliquid dashboard
python -m cli.simple hyperliquid

# Or use shortcuts
python -m cli.simple pm   # Polymarket
python -m cli.simple hl   # Hyperliquid
```

You'll see a live-updating dashboard with:
- **Polymarket**: Recent trades ($1k+), whale alerts ($10k+)
- **Hyperliquid**: Trades across top 10 coins (BTC, ETH, SOL, etc.), whale alerts ($10M+)
- Real-time stats (volume, trade count)

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

### Wallet Tracking

```
/track <address> [label]       - Start tracking a wallet
/untrack <address|label>       - Stop tracking a wallet
/list                          - List your tracked wallets
```

### Wallet Groups

```
/creategroup <name> [desc]     - Create a wallet group
/groups                        - List your groups
/groupinfo <name>              - Show group details
/addtogroup <group> <wallet>   - Add wallet to group
/deletegroup <name>            - Delete a group
```

### Polymarket

```
/pm_positions <address>        - Show Polymarket positions
/pm_pnl <address> [period]     - Show PnL (day|week|month|all)
/pm_trades <address> [hours]   - Recent trades
/pm_whales [min_size]          - View whale trades ($10k+ default)
/pm_subscribe                  - Subscribe to whale alerts
/pm_unsubscribe                - Unsubscribe from whale alerts
```

### Hyperliquid

```
/hl_positions <address>        - Show Hyperliquid positions
/hl_pnl <address> [period]     - Show PnL
/hl_sentiment <coin>           - Market sentiment
/hl_trades <coin>              - Recent trades for a coin
```

**CLI Only (no Telegram commands yet):**
- Live trades dashboard for top 10 coins
- Whale alerts for $10M+ trades
- Market sentiment analysis

## Project Structure

```
printdao/
├── cli/
│   ├── simple.py            # Live terminal dashboard
│   └── app.py               # Full TUI app (textual)
├── bot/
│   ├── main.py              # Telegram bot entry point
│   ├── config.py            # Configuration
│   ├── routers/             # Command handlers
│   ├── services/            # Business logic
│   └── storage/             # Redis client & models
├── indexers/                # Exchange integrations
│   ├── hyperliquid/
│   └── polymarket/          # Polymarket API client & tracker
├── shared/                  # Shared utilities
├── tests/                   # Test files
├── docker-compose.yml       # Docker services
├── requirements.txt         # Python dependencies
└── .env                     # Configuration (create from .env.example)
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