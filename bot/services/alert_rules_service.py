"""Structured alert rules — safe evaluation without code injection."""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

from bot.storage.redis_client import RedisClient

logger = logging.getLogger(__name__)

ALLOWED_METRICS = {"trade_value", "whale_score", "pnl_change", "position_size"}
ALLOWED_OPERATORS = {"gt", "lt", "gte", "lte", "eq"}
MAX_RULES_PER_USER = 10
RULES_KEY = "alert_rules:{user_id}"


@dataclass
class AlertRule:
    """A structured alert rule — no string-based DSL, no eval()."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    metric: str = ""
    operator: str = "gt"
    threshold: float = 0.0
    asset_filter: str = ""
    exchange_filter: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AlertRule":
        from shared.models import safe_from_dict
        return safe_from_dict(cls, data)

    def evaluate(self, trade_data: dict[str, Any]) -> bool:
        """Evaluate this rule against a trade event.

        Uses whitelisted metrics and operators only — no dynamic code execution.
        """
        if self.asset_filter:
            trade_asset = trade_data.get("asset", "").upper()
            if trade_asset != self.asset_filter.upper():
                return False

        if self.exchange_filter:
            trade_exchange = trade_data.get("exchange", "")
            if trade_exchange != self.exchange_filter:
                return False

        value = trade_data.get(self.metric)
        if value is None:
            return False

        try:
            value = float(value)
        except (ValueError, TypeError):
            return False

        if self.operator == "gt":
            return value > self.threshold
        elif self.operator == "lt":
            return value < self.threshold
        elif self.operator == "gte":
            return value >= self.threshold
        elif self.operator == "lte":
            return value <= self.threshold
        elif self.operator == "eq":
            return abs(value - self.threshold) < 0.001
        return False

    @property
    def description(self) -> str:
        """Human-readable description of the rule."""
        op_symbols = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<=", "eq": "="}
        op = op_symbols.get(self.operator, self.operator)
        parts = [f"{self.metric} {op} {self.threshold:,.0f}"]
        if self.asset_filter:
            parts.append(f"asset={self.asset_filter}")
        if self.exchange_filter:
            parts.append(f"exchange={self.exchange_filter}")
        return " | ".join(parts)


class AlertRulesService:
    """Manages per-user alert rules with safe structured evaluation."""

    def __init__(self, redis: RedisClient):
        self.redis = redis

    def _key(self, user_id: int) -> str:
        return RULES_KEY.format(user_id=user_id)

    async def add_rule(
        self,
        user_id: int,
        metric: str,
        operator: str,
        threshold: float,
        asset_filter: str = "",
        exchange_filter: str = "",
    ) -> AlertRule | str:
        """Add an alert rule. Returns AlertRule on success, error string on failure."""
        if metric not in ALLOWED_METRICS:
            return f"Invalid metric. Allowed: {', '.join(sorted(ALLOWED_METRICS))}"
        if operator not in ALLOWED_OPERATORS:
            return f"Invalid operator. Allowed: {', '.join(sorted(ALLOWED_OPERATORS))}"
        if threshold < 0:
            return "Threshold must be non-negative."

        key = self._key(user_id)
        existing = await self.get_rules(user_id)
        if len(existing) >= MAX_RULES_PER_USER:
            return f"Maximum {MAX_RULES_PER_USER} rules per user."

        rule = AlertRule(
            metric=metric,
            operator=operator,
            threshold=threshold,
            asset_filter=asset_filter,
            exchange_filter=exchange_filter,
        )

        await self.redis.hset(key, rule.id, json.dumps(rule.to_dict()))
        return rule

    async def get_rules(self, user_id: int) -> list[AlertRule]:
        """Get all rules for a user."""
        key = self._key(user_id)
        data = await self.redis.hgetall(key)
        rules = []
        for rule_id, rule_json in data.items():
            try:
                if isinstance(rule_json, str):
                    rule_data = json.loads(rule_json)
                else:
                    rule_data = rule_json
                rules.append(AlertRule.from_dict(rule_data))
            except Exception as e:
                logger.warning(f"Failed to parse rule {rule_id}: {e}")
        return rules

    async def delete_rule(self, user_id: int, rule_id: str) -> bool:
        """Delete a rule by ID."""
        key = self._key(user_id)
        return await self.redis.hdel(key, rule_id) > 0

    async def evaluate_trade(self, user_id: int, trade_data: dict[str, Any]) -> list[AlertRule]:
        """Evaluate all user rules against a trade event. Returns triggered rules."""
        rules = await self.get_rules(user_id)
        return [rule for rule in rules if rule.evaluate(trade_data)]

    async def evaluate_trade_all_users(self, trade_data: dict[str, Any]) -> dict[int, list[AlertRule]]:
        """Evaluate rules for all users. Returns {user_id: [triggered_rules]}."""
        pattern = "alert_rules:*"
        keys = await self.redis.scan_keys(pattern)

        triggered = {}
        for key_str in keys:
            try:
                user_id = int(key_str.split(":")[1])
            except (IndexError, ValueError):
                continue

            matching = await self.evaluate_trade(user_id, trade_data)
            if matching:
                triggered[user_id] = matching

        return triggered
