"""Polymarket API client with rate limiting."""

import asyncio
import logging
from typing import Optional
from datetime import datetime, timedelta

import aiohttp

from shared.resilience import CircuitBreaker, CircuitOpenError
from .models import Position, Trade, Activity, PortfolioValue, Market, Tag, MarketHolder

logger = logging.getLogger(__name__)

_pm_breaker = CircuitBreaker(name="polymarket", failure_threshold=5, recovery_timeout=60.0)


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
        timeout: float = 60.0,  # Gamma API can be slow
    ):
        self.rate_limiter = RateLimiter(calls_per_second)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session with connection pooling."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=20, limit_per_host=10, ttl_dns_cache=300,
            )
            self._session = aiohttp.ClientSession(
                timeout=self.timeout, connector=connector,
            )
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
        _max_retries: int = 5,
    ) -> dict | list:
        """Make a rate-limited request with bounded retry on 429 and circuit breaker."""
        _pm_breaker._check_can_execute()
        session = await self._get_session()

        for attempt in range(_max_retries):
            await self.rate_limiter.acquire()
            try:
                async with session.request(method, url, params=params) as response:
                    if response.status == 200:
                        _pm_breaker._record_success()
                        return await response.json()
                    elif response.status == 429:
                        delay = min(5 * (2 ** attempt), 60)
                        logger.warning(
                            f"Rate limited (attempt {attempt + 1}/{_max_retries}), "
                            f"retrying in {delay}s"
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        text = await response.text()
                        logger.error(f"API error {response.status}: {text}")
                        return []
            except asyncio.TimeoutError:
                _pm_breaker._record_failure()
                logger.error(f"Timeout fetching {url} (attempt {attempt + 1})")
                if attempt < _max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return []
            except CircuitOpenError:
                raise
            except Exception as e:
                _pm_breaker._record_failure()
                logger.error(f"Error fetching {url}: {e}")
                return []

        _pm_breaker._record_failure()
        logger.error(f"Max retries ({_max_retries}) exceeded for {url}")
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

    async def get_trades(
        self,
        address: Optional[str] = None,
        market: Optional[str] = None,
        min_size: Optional[float] = None,
        side: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Trade]:
        """Get trades for a wallet or market.

        Args:
            address: Optional wallet address
            market: Optional condition ID
            min_size: Minimum trade size in USDC
            side: BUY or SELL
            limit: Results per page
            offset: Pagination offset

        Returns:
            List of Trade objects
        """
        params = {
            "limit": min(limit, 500),
            "offset": offset,
        }

        if address:
            params["user"] = address
        if market:
            params["market"] = market
        if min_size:
            params["filterType"] = "CASH"
            params["filterAmount"] = min_size
        if side:
            params["side"] = side

        url = f"{self.BASE_URL}/trades"
        data = await self._request("GET", url, params)

        if not isinstance(data, list):
            return []

        return [Trade.from_api(item) for item in data]

    async def get_whale_trades(
        self,
        min_size: float = 10000.0,
        limit: int = 50,
    ) -> list[Trade]:
        """Get recent whale trades (large trades).

        Args:
            min_size: Minimum trade size in USDC
            limit: Max trades to return

        Returns:
            List of whale trades
        """
        return await self.get_trades(
            min_size=min_size,
            limit=limit,
        )

    async def get_market_holders(
        self,
        condition_id: str,
        limit: int = 100,
    ) -> list[dict]:
        """Get top holders for a market.

        Args:
            condition_id: Market condition ID
            limit: Max holders to return

        Returns:
            List of holder data
        """
        params = {
            "market": condition_id,
            "limit": limit,
        }

        url = f"{self.BASE_URL}/holders"
        data = await self._request("GET", url, params)

        if not isinstance(data, list):
            return []

        return data

    # ==================== Gamma API Methods ====================

    async def get_markets(
        self,
        active: bool = True,
        closed: bool = False,
        archived: bool = False,
        order_by: str = "liquidity",
        ascending: bool = False,
        limit: int = 20,
        offset: int = 0,
        tag_slug: Optional[str] = None,
        liquidity_min: Optional[float] = 10000.0,
        volume_min: Optional[float] = None,
    ) -> list[Market]:
        """Get markets from Gamma API sorted by liquidity/volume.

        Args:
            active: Filter for active markets only
            closed: Include closed markets
            archived: Include archived markets
            order_by: Sort field (liquidity, volume, volume24hr)
            ascending: Sort ascending (False = highest first)
            limit: Results per page
            offset: Pagination offset
            tag_slug: Filter by tag/category (note: API filtering unreliable)
            liquidity_min: Minimum liquidity in USD (filters out spam)
            volume_min: Minimum volume in USD

        Returns:
            List of Market objects sorted by liquidity
        """
        # Fetch extra to ensure good sorting after client-side filter
        fetch_limit = min(limit * 2, 100)

        params = {
            "limit": fetch_limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "archived": str(archived).lower(),
        }

        # Note: tag_slug filtering is unreliable on API side
        # We skip it and do client-side filtering if needed

        # Filter out low liquidity/spam markets
        if liquidity_min is not None:
            params["liquidity_num_min"] = liquidity_min

        if volume_min is not None:
            params["volume_num_min"] = volume_min

        url = f"{self.GAMMA_URL}/markets"
        data = await self._request("GET", url, params)

        if not isinstance(data, list):
            return []

        markets = [Market.from_api(item) for item in data]

        # Client-side sorting (API ordering is unreliable)
        if order_by == "liquidity":
            markets.sort(key=lambda m: m.liquidity, reverse=not ascending)
        elif order_by == "volume":
            markets.sort(key=lambda m: m.volume, reverse=not ascending)
        elif order_by == "volume24hr":
            markets.sort(key=lambda m: m.volume_24h, reverse=not ascending)

        return markets[:limit]

    async def search_markets(
        self,
        query: str,
        limit: int = 20,
        active_only: bool = True,
        liquidity_min: float = 1000.0,
    ) -> list[Market]:
        """Search markets by name/description (client-side filtering).

        Args:
            query: Search query (searches question text)
            limit: Max results
            active_only: Only return active markets
            liquidity_min: Minimum liquidity to include

        Returns:
            List of matching Market objects sorted by liquidity
        """
        # Fetch markets with decent liquidity, filter client-side
        # Note: API search params don't work reliably
        params = {
            "limit": 100,  # Fetch max to search through
            "liquidity_num_min": liquidity_min,
        }

        if active_only:
            params["active"] = "true"
            params["closed"] = "false"
            params["archived"] = "false"

        url = f"{self.GAMMA_URL}/markets"
        data = await self._request("GET", url, params)

        if not isinstance(data, list):
            return []

        # Client-side search and filter
        query_lower = query.lower()
        question_matches = []
        description_matches = []

        for item in data:
            market = Market.from_api(item)
            # Prioritize question matches over description matches
            if query_lower in market.question.lower():
                question_matches.append(market)
            elif query_lower in market.description.lower():
                description_matches.append(market)

        # Sort each group by liquidity, then combine (question matches first)
        question_matches.sort(key=lambda m: m.liquidity, reverse=True)
        description_matches.sort(key=lambda m: m.liquidity, reverse=True)
        markets = question_matches + description_matches

        return markets[:limit]

    async def get_market_by_slug(
        self,
        slug: str,
    ) -> Optional[Market]:
        """Get single market by slug.

        Args:
            slug: Market slug

        Returns:
            Market object or None
        """
        url = f"{self.GAMMA_URL}/markets/{slug}"
        data = await self._request("GET", url)

        if not data or isinstance(data, list):
            return None

        return Market.from_api(data)

    async def get_market_by_condition_id(
        self,
        condition_id: str,
    ) -> Optional[Market]:
        """Get single market by condition ID.

        Args:
            condition_id: Market condition ID

        Returns:
            Market object or None
        """
        params = {"condition_id": condition_id}
        url = f"{self.GAMMA_URL}/markets"
        data = await self._request("GET", url, params)

        if not data or not isinstance(data, list) or len(data) == 0:
            return None

        return Market.from_api(data[0])

    async def get_market_positions(
        self,
        condition_id: str,
        limit: int = 50,
    ) -> list[MarketHolder]:
        """Get top holders/positions for a market.

        Args:
            condition_id: Market condition ID
            limit: Max holders per outcome

        Returns:
            List of MarketHolder objects
        """
        holders_data = await self.get_market_holders(condition_id, limit)

        holders = []
        for item in holders_data:
            holders.append(MarketHolder.from_api(item))

        return holders

    async def get_tags(self) -> list[Tag]:
        """Get available categories/tags.

        Returns:
            List of Tag objects
        """
        url = f"{self.GAMMA_URL}/tags"
        data = await self._request("GET", url)

        if not isinstance(data, list):
            return []

        return [Tag.from_api(item) for item in data]

    async def get_events(
        self,
        active: bool = True,
        closed: bool = False,
        archived: bool = False,
        order_by: str = "liquidity",
        ascending: bool = False,
        limit: int = 20,
        offset: int = 0,
        tag_slug: Optional[str] = None,
        liquidity_min: Optional[float] = 10000.0,
    ) -> list[dict]:
        """Get events from Gamma API.

        Args:
            active: Filter for active events
            closed: Include closed events
            archived: Include archived events
            order_by: Sort field
            ascending: Sort ascending
            limit: Results per page
            offset: Pagination offset
            tag_slug: Filter by tag
            liquidity_min: Minimum liquidity in USD

        Returns:
            List of event dicts
        """
        params = {
            "limit": min(limit, 100),
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "archived": str(archived).lower(),
            "order": order_by,
            "ascending": str(ascending).lower(),
        }

        if tag_slug:
            params["tag_slug"] = tag_slug

        if liquidity_min is not None:
            params["liquidity_num_min"] = liquidity_min

        url = f"{self.GAMMA_URL}/events"
        data = await self._request("GET", url, params)

        if not isinstance(data, list):
            return []

        return data
