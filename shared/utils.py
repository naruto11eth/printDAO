"""Shared utility functions for PrintDAO."""

import re
from typing import Optional
from decimal import Decimal

from shared.constants import ETHEREUM_ADDRESS_PATTERN, PNL_PERIOD_ALIASES, VALID_PNL_PERIODS

