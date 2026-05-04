from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from basis_estimator import BasisEstimator
from binance_signal_engine import BinanceSignalConfig
from bot_orchestrator import MinimalBotOrchestrator
from exit_armory import ExitArmory
from exit_policy import ExitPolicyConfig
from fast_order_submitter import FastOrderTemplate
from hot_path_engine import HotPathEngine
from order_tracker import LocalOrderTracker
from runtime_state import MinimalRuntimeState
from signal_decision import SignalDecisionConfig
from template_armory import ArmoryConfig, TemplateArmory


class _Submitter(Protocol):
    async def submit(self, template: FastOrderTemplate) -> dict:
        ...


BuildTemplate = Callable[..., Awaitable[FastOrderTemplate]]


@dataclass(frozen=True, slots=True)
class RuntimeWiringConfig:
    owner: str
    max_quote_age_ns: int = 250_000_000
    max_concurrent_positions: int = 3


@dataclass(frozen=True, slots=True)
class MinimalRuntime:
    state: MinimalRuntimeState
    tracker: LocalOrderTracker
    hot_path: HotPathEngine
    entry_armory: TemplateArmory
    exit_armory: ExitArmory
    orchestrator: MinimalBotOrchestrator
    basis_estimator: BasisEstimator | None


def _with_owner(build_template: BuildTemplate, owner: str) -> BuildTemplate:
    async def _build(**kwargs) -> FastOrderTemplate:
        kwargs.setdefault("owner", owner)
        return await build_template(**kwargs)

    return _build


def build_runtime(
    cfg: RuntimeWiringConfig,
    *,
    state: MinimalRuntimeState,
    submitter: _Submitter,
    build_template: BuildTemplate,
    entry_cfg: ArmoryConfig,
    exit_cfg: ExitPolicyConfig,
    signal_cfg: BinanceSignalConfig,
    decision_cfg: SignalDecisionConfig,
    now_s: Callable[[], float],
    now_ns: Callable[[], int],
    basis_estimator: BasisEstimator | None = None,
) -> MinimalRuntime:
    tracker = LocalOrderTracker(current_run_only=True)
    hot_path = HotPathEngine(
        submitter=submitter,
        tracker=tracker,
        now_ns=now_ns,
        max_quote_age_ns=cfg.max_quote_age_ns,
        max_concurrent_positions=cfg.max_concurrent_positions,
    )
    builder = _with_owner(build_template, cfg.owner)
    entry_armory = TemplateArmory(
        cfg=entry_cfg,
        engine=hot_path,
        build_template=builder,
        now_ns=now_ns,
    )
    exit_armory = ExitArmory(
        engine=hot_path,
        build_template=builder,
        owner=cfg.owner,
        max_quote_age_ns=cfg.max_quote_age_ns,
    )
    orchestrator = MinimalBotOrchestrator(
        state=state,
        armory=entry_armory,
        hot_path=hot_path,
        signal_cfg=signal_cfg,
        decision_cfg=decision_cfg,
        now_s=now_s,
        basis_estimator=basis_estimator,
    )
    orchestrator.configure_exit_policy(exit_cfg, exit_armory=exit_armory, tracker=tracker)
    return MinimalRuntime(
        state=state,
        tracker=tracker,
        hot_path=hot_path,
        entry_armory=entry_armory,
        exit_armory=exit_armory,
        orchestrator=orchestrator,
        basis_estimator=basis_estimator,
    )
