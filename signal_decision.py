from __future__ import annotations

from dataclasses import dataclass

from binance_signal_engine import BinanceSignal


_UP_WORDS = ("up", "yes", "above", "higher")
_DOWN_WORDS = ("down", "no", "below", "lower")


@dataclass(frozen=True, slots=True)
class MarketSignalContract:
    yes_token_id: str
    no_token_id: str
    yes_label: str
    no_label: str

    @property
    def is_valid(self) -> bool:
        yes = self.yes_label.strip().lower()
        no = self.no_label.strip().lower()
        if not self.yes_token_id or not self.no_token_id or self.yes_token_id == self.no_token_id:
            return False
        return _contains_any(yes, _UP_WORDS) and _contains_any(no, _DOWN_WORDS)

    def token_for_signal(self, side: str) -> str:
        if not self.is_valid:
            return ""
        key = side.upper()
        if key == "YES":
            return self.yes_token_id
        if key == "NO":
            return self.no_token_id
        return ""


@dataclass(frozen=True, slots=True)
class SignalDecisionConfig:
    max_ask: float
    max_quote_age_us: int = 250_000
    min_tte_us: int = 2_000_000
    min_strength: float = 3.0
    min_edge: float = 0.0
    strength_price_scale: float = 0.03


@dataclass(frozen=True, slots=True)
class SignalDecision:
    action: str
    reason: str
    side: str = ""
    token_id: str = ""
    edge: float = 0.0


def decide_buy(
    signal: BinanceSignal | None,
    contract: MarketSignalContract,
    cfg: SignalDecisionConfig,
    *,
    ask: float,
    quote_age_us: int,
    tte_us: int,
) -> SignalDecision:
    if signal is None:
        return SignalDecision("NO_BUY", "no_signal")
    side = signal.side.upper()
    token_id = contract.token_for_signal(side)
    if not token_id:
        return SignalDecision("NO_BUY", "invalid_contract", side=side)
    if tte_us < cfg.min_tte_us:
        return SignalDecision("NO_BUY", "near_expiry", side=side, token_id=token_id)
    if quote_age_us < 0 or quote_age_us > cfg.max_quote_age_us:
        return SignalDecision("NO_BUY", "quote_stale", side=side, token_id=token_id)
    if ask <= 0.0:
        return SignalDecision("NO_BUY", "invalid_ask", side=side, token_id=token_id)
    if cfg.max_ask > 0.0 and ask > cfg.max_ask:
        return SignalDecision("NO_BUY", "ask_above_limit", side=side, token_id=token_id)
    if signal.strength < cfg.min_strength:
        return SignalDecision("NO_BUY", "weak_signal", side=side, token_id=token_id)

    fair = _strength_to_fair(signal.strength, cfg.strength_price_scale)
    edge = fair - ask
    if edge < cfg.min_edge:
        return SignalDecision("NO_BUY", "edge_below_min", side=side, token_id=token_id, edge=edge)
    return SignalDecision("BUY", "edge_ok", side=side, token_id=token_id, edge=edge)


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    padded = " " + value.replace("-", " ").replace("_", " ") + " "
    return any((" " + needle + " ") in padded for needle in needles)


def _strength_to_fair(strength: float, scale: float) -> float:
    fair = 0.50 + max(0.0, strength) * max(0.0, scale)
    if fair > 0.99:
        return 0.99
    return fair
