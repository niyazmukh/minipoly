from __future__ import annotations

import math
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
    min_ask: float = 0.0
    entry_slippage: float = 0.0
    max_quote_age_us: int = 250_000
    min_tte_us: int = 2_000_000
    min_edge: float = 0.05
    # Probabilistic scaling — Brownian barrier-cross.
    # P_yes = Phi((microprice - strike + gamma*move)
    #             / (sigma_scale * sigma_px * sqrt(tte_s)))
    prob_gamma_move: float = 0.5
    prob_sigma_scale: float = 1.5
    prob_sigma_floor_usd: float = 2.0
    prob_floor: float = 0.02
    prob_ceil: float = 0.98
    max_tte_us: int = 600_000_000


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
    bid: float = 0.0,
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
    executable_ask = ask + max(0.0, cfg.entry_slippage)
    if cfg.min_ask > 0.0 and executable_ask < cfg.min_ask:
        return SignalDecision("NO_BUY", "ask_below_limit", side=side, token_id=token_id)
    if cfg.max_ask > 0.0 and executable_ask > cfg.max_ask:
        return SignalDecision("NO_BUY", "ask_above_limit", side=side, token_id=token_id)

    p_yes = _bs_prob_yes(signal, cfg, tte_us)
    if p_yes is None:
        return SignalDecision("NO_BUY", "prob_unavailable", side=side, token_id=token_id)

    if side == "YES":
        side_prob = p_yes
    else:
        side_prob = 1.0 - p_yes
    edge = side_prob - executable_ask
    min_edge = _effective_min_edge(cfg, ask=executable_ask, bid=bid)
    if edge < min_edge:
        return SignalDecision("NO_BUY", "edge_below_min", side=side, token_id=token_id, edge=edge)
    return SignalDecision("BUY", "edge_ok", side=side, token_id=token_id, edge=edge)


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    padded = " " + value.replace("-", " ").replace("_", " ") + " "
    return any((" " + needle + " ") in padded for needle in needles)


def _phi(z: float) -> float:
    """Standard normal CDF using math.erf — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _effective_min_edge(cfg: SignalDecisionConfig, *, ask: float, bid: float = 0.0) -> float:
    live_spread = max(0.0, ask - bid) if bid > 0.0 else 0.0
    return max(float(cfg.min_edge), live_spread)


def _bs_prob_yes(
    signal: BinanceSignal,
    cfg: SignalDecisionConfig,
    tte_us: int,
) -> float | None:
    """Brownian barrier-cross probability that microprice closes >= strike at expiry.

    P_yes = Phi(drift_eff / sigma_eff)
      drift_eff = (microprice - strike) + gamma*move
      sigma_eff = sigma_scale * sigma_px * sqrt(tte_s)

    Returns None on missing/degenerate inputs. Caller treats None as
    "prob_unavailable" and fails closed.
    """
    if signal.microprice <= 0.0 or signal.strike <= 0.0:
        return None
    if tte_us <= 0 or tte_us > cfg.max_tte_us:
        return None
    sigma_px = max(float(signal.sigma_px), float(cfg.prob_sigma_floor_usd))
    if sigma_px <= 0.0:
        return None
    move_from_strike = float(signal.microprice) - float(signal.strike)
    drift = (
        move_from_strike
        + float(cfg.prob_gamma_move) * float(signal.move)
    )
    tte_s = float(tte_us) / 1_000_000.0
    sigma_eff = float(cfg.prob_sigma_scale) * sigma_px * math.sqrt(tte_s)
    if sigma_eff <= 1e-9:
        return None
    z = drift / sigma_eff
    if not math.isfinite(z):
        return None
    p = _phi(z)
    if not math.isfinite(p):
        return None
    floor = max(0.0, min(1.0, float(cfg.prob_floor)))
    ceil = max(floor, min(1.0, float(cfg.prob_ceil)))
    if p < floor:
        p = floor
    elif p > ceil:
        p = ceil
    return p
