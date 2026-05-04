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
    max_quote_age_us: int = 250_000
    min_tte_us: int = 2_000_000
    min_strength: float = 3.0
    min_edge: float = 0.05
    # Legacy heuristic fallback (used when prob model returns prob_unavailable
    # AND prob_use_legacy is true).
    strength_price_scale: float = 0.03
    # Probabilistic scaling — Brownian barrier-cross with order-flow tilts.
    # P_yes = Phi((microprice - strike + gamma*move + alpha*OFI + beta*imbalance*sigma)
    #             / (sigma_scale * sigma_px * sqrt(tte_s)))
    prob_alpha_ofi: float = 0.0
    prob_beta_imb: float = 0.0
    prob_gamma_move: float = 0.5
    prob_sigma_scale: float = 1.5
    prob_sigma_floor_usd: float = 2.0
    prob_floor: float = 0.02
    prob_ceil: float = 0.98
    min_prob: float = 0.55
    max_tte_us: int = 600_000_000
    use_legacy_fair: bool = False


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
    if cfg.min_ask > 0.0 and ask < cfg.min_ask:
        return SignalDecision("NO_BUY", "ask_below_limit", side=side, token_id=token_id)
    if cfg.max_ask > 0.0 and ask > cfg.max_ask:
        return SignalDecision("NO_BUY", "ask_above_limit", side=side, token_id=token_id)
    if signal.strength < cfg.min_strength:
        return SignalDecision("NO_BUY", "weak_signal", side=side, token_id=token_id)

    if cfg.use_legacy_fair:
        fair = _strength_to_fair(signal.strength, cfg.strength_price_scale)
        edge = fair - ask
        min_edge = _effective_min_edge(cfg, ask=ask)
        if edge < min_edge:
            return SignalDecision("NO_BUY", "edge_below_min", side=side, token_id=token_id, edge=edge)
        return SignalDecision("BUY", "edge_ok_legacy", side=side, token_id=token_id, edge=edge)

    p_yes = _bs_prob_yes(signal, cfg, tte_us)
    if p_yes is None:
        return SignalDecision("NO_BUY", "prob_unavailable", side=side, token_id=token_id)

    if side == "YES":
        side_prob = p_yes
    else:
        side_prob = 1.0 - p_yes
    edge = side_prob - ask
    min_edge = _effective_min_edge(cfg, ask=ask)
    if edge < min_edge:
        return SignalDecision("NO_BUY", "edge_below_min", side=side, token_id=token_id, edge=edge)
    # Absolute probability floor applies only to expensive tokens (ask >= 0.50).
    # Cheap tokens (ask < 0.50) are edge-only — P can be low (0.31) but if
    # edge is strong (0.17), the expected return justifies the trade.
    # SF1 (Dubach 2026, Table 1) supports spread-aware scrutiny via
    # _effective_min_edge above, not a universal absolute-probability floor.
    if ask >= 0.50 and side_prob < cfg.min_prob:
        return SignalDecision("NO_BUY", "prob_below_floor", side=side, token_id=token_id, edge=edge)
    return SignalDecision("BUY", "edge_ok", side=side, token_id=token_id, edge=edge)


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    padded = " " + value.replace("-", " ").replace("_", " ") + " "
    return any((" " + needle + " ") in padded for needle in needles)


def _strength_to_fair(strength: float, scale: float) -> float:
    fair = 0.50 + max(0.0, strength) * max(0.0, scale)
    if fair > 0.99:
        return 0.99
    return fair


def _phi(z: float) -> float:
    """Standard normal CDF using math.erf — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# Median half-spread per mid-price decile, derived from Dubach (2026) SF1
# Table 1.  Converted from median quoted bps to probability-price units:
#   half_spread = mid * bps / 10000 / 2
# These are structural properties of Polymarket's CLOB V1 order book
# (2026-02-28 to 2026-03-27, 600 markets), not strategy parameters.
_SPREAD_FLOOR_DECILES: list[float] = [
    0.0045,  # [0.00, 0.10)
    0.0100,  # [0.10, 0.20)
    0.0094,  # [0.20, 0.30)
    0.0452,  # [0.30, 0.40)
    0.0090,  # [0.40, 0.50)
    0.0110,  # [0.50, 0.60)
    0.0144,  # [0.60, 0.70)
    0.0159,  # [0.70, 0.80)
    0.0094,  # [0.80, 0.90)
    0.0025,  # [0.90, 1.00]
]


def _paper_spread_floor(ask: float) -> float:
    """Median half-spread cost for the price decile containing *ask*.

    At current min_edge=0.05 this floor is non-binding for all deciles
    (max median half-spread is 0.0452 in [0.30,0.40)).  The floor exists
    so that lowering min_edge cannot silently drop below the venue's
    structural spread cost.
    """
    if ask <= 0.0:
        return 0.0
    idx = min(int(ask * 10), 9)
    return _SPREAD_FLOOR_DECILES[idx]


def _effective_min_edge(cfg: SignalDecisionConfig, *, ask: float) -> float:
    return max(float(cfg.min_edge), _paper_spread_floor(ask))


def _bs_prob_yes(
    signal: BinanceSignal,
    cfg: SignalDecisionConfig,
    tte_us: int,
) -> float | None:
    """Brownian barrier-cross probability that microprice closes >= strike at expiry.

    P_yes = Phi(drift_eff / sigma_eff)
      drift_eff = (microprice - strike) + gamma*move + alpha*ofi + beta*imbalance*sigma_px
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
        + float(cfg.prob_alpha_ofi) * float(signal.ofi)
        + float(cfg.prob_beta_imb) * float(signal.imbalance) * sigma_px
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
