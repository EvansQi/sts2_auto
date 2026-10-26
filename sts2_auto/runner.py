"""Run driver + batch evaluation.

A run is: start_run -> loop(decision -> policy.act -> engine.act) -> game_over.
Each run gets a fresh engine process for isolation, plus an optional JSONL log
with every decision, action, reason and score.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .env import EnvError, HeadlessEnv
from .policy.base import Action, Policy


@dataclass
class RunResult:
    seed: str
    character: str
    ascension: int
    victory: bool = False
    completed: bool = False
    floor: int = 0
    act: int = 0
    hp: int = 0
    max_hp: int = 0
    steps: int = 0
    error: str = ""
    duration_s: float = 0.0
    log_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def state_key(state: Dict[str, Any]) -> tuple:
    """Fingerprint used to detect no-progress loops."""
    p = state.get("player") or {}
    hand = tuple(c.get("id") for c in (state.get("hand") or []))
    enemies = tuple(
        (e.get("index"), e.get("hp"), e.get("block"))
        for e in (state.get("enemies") or [])
    )
    return (
        state.get("decision"),
        state.get("round"),
        p.get("hp"),
        p.get("block"),
        state.get("energy"),
        hand,
        enemies,
    )


def retry_action(state: Dict[str, Any], failed: Action) -> Action:
    """Pick a different action after the engine rejected the previous one."""
    if state.get("decision") == "event_choice" and failed.name == "choose_option":
        tried = failed.args.get("option_index")
        for opt in state.get("options") or []:
            if opt.get("is_locked"):
                continue
            if opt.get("index") != tried:
                return Action("choose_option", {"option_index": opt["index"]},
                              "retry after engine error")
    return recovery_action(state)


def recovery_action(state: Dict[str, Any]) -> Action:
    """Last-resort action when the policy keeps returning to the same state."""
    decision = state.get("decision")
    if decision == "combat_play":
        return Action("end_turn", reason="stuck recovery")
    if decision == "map_select":
        choices = state.get("choices") or []
        if choices:
            c = choices[0]
            return Action("select_map_node", {"col": c.get("col", 0), "row": c.get("row", 1)},
                          "stuck recovery")
        return Action("select_map_node", {"col": 0, "row": 1}, "stuck recovery")
    if decision == "card_reward":
        return Action("skip_card_reward", reason="stuck recovery")
    if decision == "card_select":
        return Action("skip_select", reason="stuck recovery")
    if decision == "bundle_select":
        return Action("select_bundle", {"bundle_index": 0}, "stuck recovery")
    return Action("leave_room", reason="stuck recovery")


def play_run(
    seed: str,
    character: str = "Ironclad",
    ascension: int = 0,
    policy: Optional[Policy] = None,
    env: Optional[HeadlessEnv] = None,
    max_steps: int = 3000,
    log_path: Optional[str | Path] = None,
    full_state: bool = False,
    max_repeats: int = 5,
    sts2_cli: Optional[str] = None,
    dotnet: Optional[str] = None,
) -> RunResult:
    if policy is None:
        from .policy.rules import RulePolicy
        policy = RulePolicy()
    policy.reset()

    own_env = env is None
    if env is None:
        env = HeadlessEnv(sts2_cli=sts2_cli, dotnet=dotnet)

    result = RunResult(seed=seed, character=character, ascension=ascension)
    log_file = open(log_path, "w", encoding="utf-8") if log_path else None
    started = time.time()
    steps = 0
    last_key = None
    repeat = 0

    try:
        state = env.start_run(character=character, seed=seed, ascension=ascension)
        while steps < max_steps:
            if state.get("decision") == "game_over":
                result.completed = True
                result.victory = bool(state.get("victory"))
                result.floor = int(state.get("floor", 0) or 0)
                result.act = int(state.get("act", 0) or 0)
                p = state.get("player") or {}
                result.hp = int(p.get("hp", 0) or 0)
                result.max_hp = int(p.get("max_hp", 0) or 0)
                break

            key = state_key(state)
            if key == last_key:
                repeat += 1
            else:
                repeat = 0
                last_key = key
            if repeat >= max_repeats:
                action = recovery_action(state)
            else:
                action = policy.act(state)
            if action is None:
                raise RuntimeError("policy returned None for %r" % state.get("decision"))

            if log_file is not None:
                record: Dict[str, Any] = {
                    "step": steps,
                    "decision": state.get("decision"),
                    "action": action.name,
                    "args": action.args,
                    "reason": action.reason,
                    "score": action.score,
                }
                if full_state:
                    record["state"] = state
                log_file.write(json.dumps(record, ensure_ascii=False) + "\n")

            steps += 1
            try:
                state = env.act(action.name, **action.args)
            except EnvError as exc:
                if log_file is not None:
                    log_file.write(json.dumps({
                        "step": steps, "error": str(exc),
                        "failed_action": action.name, "failed_args": action.args,
                    }, ensure_ascii=False) + "\n")
                retry = retry_action(state, action)
                state = env.act(retry.name, **retry.args)
        else:
            result.error = "max_steps"
    except Exception as exc:  # keep the batch going, surface the failure
        result.error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        if log_file is not None:
            log_file.close()
        if own_env:
            env.close()
        result.steps = steps
        result.duration_s = time.time() - started
        result.log_path = str(log_path or "")
    return result


@dataclass
class BatchSummary:
    runs: int = 0
    wins: int = 0
    completed: int = 0
    errors: int = 0
    win_rate: float = 0.0
    avg_floor: float = 0.0
    act_reach: Dict[int, int] = field(default_factory=dict)
    results: List[RunResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runs": self.runs,
            "wins": self.wins,
            "completed": self.completed,
            "errors": self.errors,
            "win_rate": self.win_rate,
            "avg_floor": self.avg_floor,
            "act_reach": {str(k): v for k, v in self.act_reach.items()},
            "results": [r.to_dict() for r in self.results],
        }


def run_batch(
    seeds: Iterable[str],
    character: str = "Ironclad",
    ascension: int = 0,
    policy_factory=None,
    out_dir: Optional[str | Path] = None,
    max_steps: int = 3000,
    full_state: bool = False,
    verbose: bool = True,
    sts2_cli: Optional[str] = None,
    dotnet: Optional[str] = None,
) -> BatchSummary:
    seeds = list(seeds)
    if policy_factory is None:
        from .policy.rules import RulePolicy
        policy_factory = lambda seed: RulePolicy()  # noqa: E731

    out = Path(out_dir) if out_dir else None
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)

    summary = BatchSummary(runs=len(seeds))
    for i, seed in enumerate(seeds, 1):
        policy = policy_factory(seed)
        log_path = out / (seed + ".jsonl") if out is not None else None
        if verbose:
            print("[%d/%d] seed=%s ... " % (i, len(seeds), seed), end="", flush=True)
        result = play_run(
            seed=seed,
            character=character,
            ascension=ascension,
            policy=policy,
            max_steps=max_steps,
            log_path=log_path,
            full_state=full_state,
            sts2_cli=sts2_cli,
            dotnet=dotnet,
        )
        summary.results.append(result)
        if result.victory:
            summary.wins += 1
        if result.completed:
            summary.completed += 1
        if result.error:
            summary.errors += 1
        for act in range(1, 4):
            if result.act >= act:
                summary.act_reach[act] = summary.act_reach.get(act, 0) + 1
        if verbose:
            status = "WIN " if result.victory else ("LOSS" if result.completed else "ERR ")
            extra = (" (%s)" % result.error) if result.error else ""
            print("%s act=%d floor=%d hp=%d/%d steps=%d %.1fs%s"
                  % (status, result.act, result.floor, result.hp, result.max_hp,
                     result.steps, result.duration_s, extra))

    finished = [r for r in summary.results if r.completed]
    summary.win_rate = summary.wins / len(seeds) if seeds else 0.0
    summary.avg_floor = (
        sum(r.floor for r in finished) / len(finished) if finished else 0.0
    )
    summary.act_reach = dict(sorted(summary.act_reach.items()))

    if out is not None:
        (out / "summary.json").write_text(
            json.dumps(summary.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return summary


def format_summary(summary: BatchSummary) -> str:
    lines = []
    lines.append("=" * 56)
    lines.append("RESULT  runs=%d  wins=%d  win_rate=%.1f%%  errors=%d"
                 % (summary.runs, summary.wins, summary.win_rate * 100, summary.errors))
    lines.append("avg floor (completed runs) = %.1f" % summary.avg_floor)
    if summary.act_reach:
        lines.append("act reach: " + "  ".join(
            "Act%d=%d" % (k, v) for k, v in summary.act_reach.items()))
    for r in summary.results:
        status = "WIN " if r.victory else ("LOSS" if r.completed else "ERR ")
        lines.append("  %s seed=%-12s act=%d floor=%-3d hp=%3d/%-3d steps=%-4d %5.1fs %s"
                     % (status, r.seed, r.act, r.floor, r.hp, r.max_hp, r.steps,
                        r.duration_s, r.error))
    lines.append("=" * 56)
    return "\n".join(lines)
