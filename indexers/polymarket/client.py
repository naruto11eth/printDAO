"""Polymarket API client with rate limiting."""

import asyncio
import logging
from typing import Optional
from datetime import datetime, timedelta

import aiohttp

from .models import Position, Trade, Activity, PortfolioValue

logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple rate limiter for API calls."""

    def __init__(self, calls_per_second: float = 5.0):
        self.calls_per_second = calls_per_second
        self.min_interval = 1.0 / calls_per_second
        self.last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until we can make another call."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            time_since_last = now - self.last_call
            if time_since_last < self.min_interval:
                await asyncio.sleep(self.min_interval - time_since_last)
            self.last_call = asyncio.get_event_loop().time()


class PolymarketClient:
    """Async client for Polymarket Data API."""

    BASE_URL = "https://data-api.polymarket.com"
    GAMMA_URL = "https://gamma-api.polymarket.com"

    def __init__(
        self,
        calls_per_second: float = 5.0,
        timeout: float = 30.0,
    ):
        self.rate_limiter = RateLimiter(calls_per_second)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        url: str,
        params: Optional[dict] = None,
    ) -> dict | list:
        """Make a rate-limited request."""
        await self.rate_limiter.acquire()

        session = await self._get_session()

        try:
            async with session.request(method, url, params=params) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 429:
                    # Rate limited, wait and retry
                    logger.warning("Rate limited, waiting 5 seconds...")
                    await asyncio.sleep(5)
                    return await self._request(method, url, params)
                else:
                    text = await response.text()
                    logger.error(f"API error {response.status}: {text}")
                    return []
        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching {url}")
            return []
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return []

    async def get_positions(
        self,
        address: str,
        market: Optional[str] = None,
        event_id: Optional[int] = None,
        size_threshold: float = 1.0,
        limit: int = 100,
        offset: int = 0,
        sort_by: str = "TOKENS",
        sort_direction: str = "DESC",
    ) -> list[Position]:
        """Get positions for a wallet address.

        Args:
            address: Wallet address (0x...)
            market: Optional condition ID filter
            event_id: Optional event ID filter
            size_threshold: Minimum position size
            limit: Results per page (max 500)
            offset: Pagination offset
            sort_by: Sort field (TOKENS, CURRENT, INITIAL, CASHPNL, PERCENTPNL, etc.)
            sort_direction: ASC or DESC

        Returns:
            List of Position objects
        """
        params = {
            "user": address,
            "sizeThreshold": size_threshold,
            "limit": min(limit, 500),
            "offset": offset,
            "sortBy": sort_by,
            "sortDirection": sort_direction,
        }

        if market:
            params["market"] = market
        if event_id:
            params["eventId"] = event_id

        url = f"{self.BASE_URL}/positions"
        data = await self._request("GET", url, params)

        if not isinstance(data, list):
            return []

        return [Position.from_api(item) for item in data]

    async def get_all_positions(
        self,
        address: str,
        size_threshold: float = 0.0,
    ) -> list[Position]:
        """Get all positions for a wallet (handles pagination).

        Args:
            address: Wallet address
            size_threshold: Minimum position size

        Returns:
            Complete list of positions
        """
        all_positions = []
        offset = 0
        limit = 500

        while True:
            positions = await self.get_positions(
                address=address,
                size_threshold=size_threshold,
                limit=limit,
                offset=offset,
            )

            if not positions:
                break

            all_positions.extend(positions)

            if len(positions) < limit:
                break

            offset += limit

        return all_positions

    async def get_activity(
        self,
        address: str,
        activity_type: Optional[str] = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
        side: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        sort_by: str = "TIMESTAMP",
        sort_direction: str = "DESC",
    ) -> list[Activity]:
        """Get on-chain activity for a wallet.

        Args:
            address: Wallet address
            activity_type: TRADE, SPLIT, MERGE, REDEEM, REWARD, CONVERSION
            start: Start timestamp (seconds)
            end: End timestamp (seconds)
            side: BUY or SELL (only for trades)
            limit: Results per page (max 500)
            offset: Pagination offset
            sort_by: TIMESTAMP, TOKENS, or CASH
            sort_direction: ASC or DESC

        Returns:
            List of Activity objects
        """
        params = {
            "user": address,
            "limit": min(limit, 500),
            "offset": offset,
            "sortBy": sort_by,
            "sortDirection": sort_direction,
        }

        if activity_type:
            params["type"] = activity_type
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if side:
            params["side"] = side

        url = f"{self.BASE_URL}/activity"
        data = await self._request("GET", url, params)

        if not isinstance(data, list):
            return []

        return [Activity.from_api(item) for item in data]

    async def get_recent_trades(
        self,
        address: str,
        hours: int = 24,
        limit: int = 100,
    ) -> list[Activity]:
        """Get recent trades for a wallet.

        Args:
            address: Wallet address
            hours: Look back period in hours
            limit: Max trades to return

        Returns:
            List of trade activities
        """
        end = int(datetime.now().timestamp())
        start = int((datetime.now() - timedelta(hours=hours)).timestamp())

        return await self.get_activity(
            address=address,
            activity_type="TRADE",
            start=start,
            end=end,
            limit=limit,
        )

    async def get_portfolio_value(self, address: str) -> Optional[PortfolioValue]:
        """Get total portfolio value for a wallet.

        Args:
            address: Wallet address

        Returns:
            PortfolioValue object or None
        """
        params = {"user": address}
        url = f"{self.BASE_URL}/value"
        data = await self._request("GET", url, params)

        if not data or isinstance(data, list):
            return None

        return PortfolioValue.from_api(data)
