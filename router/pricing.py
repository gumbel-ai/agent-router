"""API-equivalent token estimates, never subscription charges or billing data."""

from pydantic import BaseModel, ConfigDict, Field

PRICE_DATE = "2026-10-13"
PRICE_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"


class PriceTier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    above_input_tokens: int = Field(default=0, ge=0)
    input: float = Field(ge=0, allow_inf_nan=False)
    output: float = Field(ge=0, allow_inf_nan=False)
    cache_read: float = Field(ge=0, allow_inf_nan=False)
    cache_write_5m: float = Field(ge=0, allow_inf_nan=False)
    cache_write_1h: float = Field(ge=0, allow_inf_nan=False)
    fast_multiplier: float | None = Field(default=None, gt=0, allow_inf_nan=False)


# Standard first-party, global-route USD / million tokens, checked on PRICE_DATE.
# Exact IDs only: unfamiliar models never inherit a similarly named model's rate.
DEFAULT_PRICES = {
    "claude-haiku-5-5": [
        PriceTier(input=.1, output=.5, cache_read=.01, cache_write_5m=.125, cache_write_1h=.2),
        PriceTier(above_input_tokens=100_000, input=.5, output=2.5, cache_read=.05, cache_write_5m=.625, cache_write_1h=1)],
    "claude-sonnet-5-5": [PriceTier(input=2, output=10, cache_read=.1, cache_write_5m=2.5, cache_write_1h=4)],
    "claude-opus-5-5": [PriceTier(input=4, output=20, cache_read=.2, cache_write_5m=5, cache_write_1h=8, fast_multiplier=2)],
}


def estimate(messages: dict[str, dict], prices: dict[str, list[PriceTier]]) -> tuple[float | None, str | None]:
    if not messages:
        return None, "Token usage unavailable"
    total = 0.0
    for message in messages.values():
        tiers = prices.get(message.get("model"))
        if not tiers:
            return None, "Exact model price unavailable"
        if any(key not in message for key in ("input_tokens", "output_tokens")):
            return None, "Input/output token usage incomplete"
        context = sum(message.get(key, 0) for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        tier = tiers[0]
        for candidate in tiers[1:]:
            if context > candidate.above_input_tokens:
                tier = candidate
        write = message.get("cache_creation_input_tokens", 0)
        five, hour = message.get("cache_write_5m", 0), message.get("cache_write_1h", 0)
        if write != five + hour:
            return None, "Cache-write duration breakdown unavailable"
        amount = (message["input_tokens"] * tier.input + message["output_tokens"] * tier.output +
                  message.get("cache_read_input_tokens", 0) * tier.cache_read +
                  five * tier.cache_write_5m + hour * tier.cache_write_1h)
        if message.get("speed") == "fast":
            if tier.fast_multiplier is None:
                return None, "Fast-mode price unavailable"
            amount *= tier.fast_multiplier
        if message.get("inference_geo") == "us":
            amount *= 1.1
        elif message.get("inference_geo") not in {None, "global"}:
            return None, "Inference-region price unavailable"
        total += amount / 1_000_000
    return round(total, 10), None
