"""Calibrated signal-model loader.

The minimal bot refuses to enter live trading mode unless a calibrated model
file is present and its parameters override the heuristic defaults. The file
format is JSON, schema-versioned, and produced by the calibration pipeline
described in `minimal/docs/CALIBRATION.md`.

This module owns:

- The on-disk schema (`SCHEMA_VERSION`).
- Strict parsing with explicit error messages so an operator can diagnose a
  bad file at deploy time, before live capital is at risk.
- Application of the parsed parameters onto SignalDecisionConfig (entry
  policy) and BinanceSignalConfig (signal-engine thresholds), returning new
  immutable instances.
- Capture of provenance (dataset hash, fit timestamp, sample count, holdout
  metrics) for log lines and audit.

The runtime never *infers* values from the file. If a section is missing it
is treated as "no override"; if a section is present it must be complete.
This avoids silently-partial calibration overriding only some parameters.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from binance_signal_engine import BinanceSignalConfig
from signal_decision import SignalDecisionConfig


SCHEMA_VERSION = 1


class CalibratedModelError(RuntimeError):
    """Raised when a calibrated model file is missing, malformed, or out of date."""


@dataclass(frozen=True, slots=True)
class CalibrationProvenance:
    fit_at: str
    dataset_hash: str
    sample_count: int
    holdout_auc: float
    holdout_brier: float
    holdout_net_edge_bps: float
    notes: str = ""


@dataclass(frozen=True, slots=True)
class DecisionOverrides:
    min_strength: float | None = None
    min_edge: float | None = None
    strength_price_scale: float | None = None
    max_quote_age_us: int | None = None
    min_tte_us: int | None = None
    max_ask: float | None = None


@dataclass(frozen=True, slots=True)
class SignalEngineOverrides:
    min_abs_move: float | None = None
    min_abs_ofi: float | None = None
    min_imbalance: float | None = None
    max_spread: float | None = None
    min_window_us: int | None = None
    max_window_us: int | None = None
    signal_cooldown_us: int | None = None
    cooldown_side_agnostic: bool | None = None


@dataclass(frozen=True, slots=True)
class CalibratedSignalModel:
    schema_version: int
    provenance: CalibrationProvenance
    decision: DecisionOverrides
    signal_engine: SignalEngineOverrides

    def apply_to_decision(self, cfg: SignalDecisionConfig) -> SignalDecisionConfig:
        ov = self.decision
        overrides: dict[str, Any] = {}
        if ov.max_ask is not None:
            overrides["max_ask"] = float(ov.max_ask)
        if ov.max_quote_age_us is not None:
            overrides["max_quote_age_us"] = int(ov.max_quote_age_us)
        if ov.min_tte_us is not None:
            overrides["min_tte_us"] = int(ov.min_tte_us)
        if ov.min_strength is not None:
            overrides["min_strength"] = float(ov.min_strength)
        if ov.min_edge is not None:
            overrides["min_edge"] = float(ov.min_edge)
        if ov.strength_price_scale is not None:
            overrides["strength_price_scale"] = float(ov.strength_price_scale)
        return dataclasses.replace(cfg, **overrides)

    def apply_to_signal_engine(self, cfg: BinanceSignalConfig) -> BinanceSignalConfig:
        ov = self.signal_engine
        return dataclasses.replace(
            cfg,
            min_abs_move=cfg.min_abs_move if ov.min_abs_move is None else float(ov.min_abs_move),
            min_abs_ofi=cfg.min_abs_ofi if ov.min_abs_ofi is None else float(ov.min_abs_ofi),
            min_imbalance=cfg.min_imbalance if ov.min_imbalance is None else float(ov.min_imbalance),
            max_spread=cfg.max_spread if ov.max_spread is None else float(ov.max_spread),
            min_window_us=cfg.min_window_us if ov.min_window_us is None else int(ov.min_window_us),
            max_window_us=cfg.max_window_us if ov.max_window_us is None else int(ov.max_window_us),
            signal_cooldown_us=(
                cfg.signal_cooldown_us if ov.signal_cooldown_us is None else int(ov.signal_cooldown_us)
            ),
            cooldown_side_agnostic=(
                cfg.cooldown_side_agnostic
                if ov.cooldown_side_agnostic is None
                else bool(ov.cooldown_side_agnostic)
            ),
        )

    def summary_line(self) -> str:
        p = self.provenance
        return (
            f"calibrated_model schema=v{self.schema_version} fit_at={p.fit_at} "
            f"dataset={p.dataset_hash[:12]} samples={p.sample_count} "
            f"auc={p.holdout_auc:.3f} brier={p.holdout_brier:.4f} "
            f"net_edge_bps={p.holdout_net_edge_bps:.1f}"
        )


def load_calibrated_model(path: Path) -> CalibratedSignalModel:
    """Parse a calibrated model file. Raises CalibratedModelError on any issue."""
    if not path.is_file():
        raise CalibratedModelError(
            f"Missing calibrated signal model at {path}. See minimal/docs/CALIBRATION.md "
            f"for how to produce one. To run cold plumbing tests without a model, set "
            f"MINIMAL_REQUIRE_CALIBRATED_MODEL=false."
        )

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CalibratedModelError(f"Failed to read {path}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CalibratedModelError(f"Calibrated model at {path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise CalibratedModelError(f"Calibrated model at {path} must be a JSON object")

    schema_version = _require_int(payload, "schema_version", path)
    if schema_version != SCHEMA_VERSION:
        raise CalibratedModelError(
            f"Calibrated model at {path} has schema_version={schema_version}; "
            f"runtime supports {SCHEMA_VERSION}. Re-fit with the current pipeline."
        )

    provenance = _parse_provenance(payload.get("provenance"), path)
    decision = _parse_decision(payload.get("decision"), path)
    signal_engine = _parse_signal_engine(payload.get("signal_engine"), path)

    if (
        decision == DecisionOverrides()
        and signal_engine == SignalEngineOverrides()
    ):
        raise CalibratedModelError(
            f"Calibrated model at {path} contains no parameter overrides. "
            f"At least one of `decision` or `signal_engine` must override real values."
        )

    return CalibratedSignalModel(
        schema_version=SCHEMA_VERSION,
        provenance=provenance,
        decision=decision,
        signal_engine=signal_engine,
    )


def _parse_provenance(raw: Any, path: Path) -> CalibrationProvenance:
    if not isinstance(raw, dict):
        raise CalibratedModelError(f"Calibrated model at {path} is missing `provenance` block")
    return CalibrationProvenance(
        fit_at=_require_str(raw, "fit_at", path),
        dataset_hash=_require_str(raw, "dataset_hash", path),
        sample_count=_require_int(raw, "sample_count", path),
        holdout_auc=_require_float(raw, "holdout_auc", path),
        holdout_brier=_require_float(raw, "holdout_brier", path),
        holdout_net_edge_bps=_require_float(raw, "holdout_net_edge_bps", path),
        notes=str(raw.get("notes", "") or ""),
    )


_DECISION_FIELDS = {
    "min_strength": float,
    "min_edge": float,
    "strength_price_scale": float,
    "max_quote_age_us": int,
    "min_tte_us": int,
    "max_ask": float,
}


def _parse_decision(raw: Any, path: Path) -> DecisionOverrides:
    if raw is None:
        return DecisionOverrides()
    if not isinstance(raw, dict):
        raise CalibratedModelError(f"Calibrated model `decision` block at {path} must be an object")
    kwargs: dict[str, Any] = {}
    for key, caster in _DECISION_FIELDS.items():
        if key not in raw:
            continue
        kwargs[key] = _cast(raw[key], caster, key, path)
    return DecisionOverrides(**kwargs)


_SIGNAL_FIELDS = {
    "min_abs_move": float,
    "min_abs_ofi": float,
    "min_imbalance": float,
    "max_spread": float,
    "min_window_us": int,
    "max_window_us": int,
    "signal_cooldown_us": int,
    "cooldown_side_agnostic": bool,
}


def _parse_signal_engine(raw: Any, path: Path) -> SignalEngineOverrides:
    if raw is None:
        return SignalEngineOverrides()
    if not isinstance(raw, dict):
        raise CalibratedModelError(f"Calibrated model `signal_engine` block at {path} must be an object")
    kwargs: dict[str, Any] = {}
    for key, caster in _SIGNAL_FIELDS.items():
        if key not in raw:
            continue
        kwargs[key] = _cast(raw[key], caster, key, path)
    return SignalEngineOverrides(**kwargs)


def _require_str(raw: dict[str, Any], key: str, path: Path) -> str:
    val = raw.get(key)
    if not isinstance(val, str) or not val.strip():
        raise CalibratedModelError(f"Calibrated model at {path} missing string `{key}`")
    return val.strip()


def _require_int(raw: dict[str, Any], key: str, path: Path) -> int:
    val = raw.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        raise CalibratedModelError(f"Calibrated model at {path} missing int `{key}`")
    return val


def _require_float(raw: dict[str, Any], key: str, path: Path) -> float:
    val = raw.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise CalibratedModelError(f"Calibrated model at {path} missing number `{key}`")
    return float(val)


def _cast(value: Any, caster: type, key: str, path: Path) -> Any:
    if caster is bool:
        if isinstance(value, bool):
            return value
        raise CalibratedModelError(f"Calibrated model at {path}: `{key}` must be bool")
    if caster is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CalibratedModelError(f"Calibrated model at {path}: `{key}` must be int")
        return value
    if caster is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CalibratedModelError(f"Calibrated model at {path}: `{key}` must be number")
        return float(value)
    raise CalibratedModelError(f"Calibrated model at {path}: unsupported type for `{key}`")
