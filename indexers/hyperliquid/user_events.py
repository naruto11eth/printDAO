"""Dispatch HL userFills / userEvents WS payloads to stream:user_events:hl.

HL's `userEvents` channel does NOT include the user address in its response
envelope — the only way to correlate is:
  - userFills: response includes `user` field (schema confirmed in docs)
  - userEvents.liquidation: payload has `liquidated_user`
  - userEvents.funding: may or may not have user field (unreliable)
  - userEvents.fills: no user — ignore (userFills is the source of truth)
  - userEvents.nonUserCancel: not consumed by Tier 1/2 features, skipped

Downstream consumers can parse the full raw payload via the `payload` field
(JSON-serialized). Headline fields (address, event_type) are hoisted out for
efficient filtering on the consumer side.
"""

import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

STREAM_NAME = "stream:user_events:hl"


class _Producer(Protocol):
    async def publish(self, stream_name: str, fields: dict[str, Any]) -> str: ...


class UserEventDispatcher:
    """Translates HL WS user-channel payloads into bus events. Stateless."""

    def __init__(self, producer: _Producer):
        self.producer = producer

    async def handle_user_fills(self, data: dict) -> None:
        """Handler for `userFills` channel.

        Shape: {isSnapshot?: bool, user: str, fills: [WsFill]}.
        """
        user = self._extract_address(data.get("user"))
        if not user:
            return
        fills = data.get("fills") or []
        if not fills:
            return
        await self._publish({
            "address": user,
            "event_type": "fills",
            "is_snapshot": "1" if data.get("isSnapshot") else "0",
            "fills_count": len(fills),
            "payload": self._encode(data),
        })

    async def handle_user_event(self, data: dict) -> None:
        """Handler for `user` channel (userEvents subscription)."""
        if "liquidation" in data:
            liq = data.get("liquidation") or {}
            user = self._extract_address(
                liq.get("liquidated_user") or liq.get("liquidatedUser"),
            )
            if not user:
                logger.debug("liquidation event without liquidated_user")
                return
            await self._publish({
                "address": user,
                "event_type": "liquidation",
                "payload": self._encode(data),
            })
            return

        if "funding" in data:
            fund = data.get("funding") or {}
            user = self._extract_address(fund.get("user"))
            if not user:
                # userEvents.funding sometimes omits user; can't correlate — skip.
                return
            await self._publish({
                "address": user,
                "event_type": "funding",
                "payload": self._encode(data),
            })
            return

        # fills  → duplicate of userFills (without user field); skip.
        # nonUserCancel → not consumed by any B/C-phase indexer.

    async def _publish(self, fields: dict[str, Any]) -> None:
        try:
            await self.producer.publish(STREAM_NAME, fields)
        except Exception as e:
            logger.error(
                "Failed to publish user event (%s): %s",
                fields.get("event_type"), e,
            )

    @staticmethod
    def _extract_address(raw: Any) -> str | None:
        if not raw:
            return None
        s = str(raw).lower()
        return s if s.startswith("0x") and len(s) == 42 else None

    @staticmethod
    def _encode(data: dict) -> str:
        # Separators keep the stream field size minimal.
        return json.dumps(data, separators=(",", ":"), default=str)
