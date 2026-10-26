"""Command line entry point: python -m sts2_auto run ..."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .env import EnvError, find_dotnet, find_sts2_cli
from .policy.random_policy import RandomPolicy
from .policy.rules import RulePolicy
from .runner import format_summary, run_batch

POLICIES = {
    "rules": lambda seed: RulePolicy(),
    "random": lambda seed: RandomPolicy(seed),
}
CHARACTERS = ["Ironclad", "Silent", "Defect", "Regent", "Necrobinder"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sts2-auto",
        description="Run a headless Slay the Spire 2 agent and report win rate.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="play N runs and print a summary")
    run.add_argument("--runs", type=int, default=1, help="number of seeds (default 1)")
    run.add_argument("--character", default="Ironclad", choices=CHARACTERS)
    run.add_argument("--ascension", type=int, default=0)
    run.add_argument("--policy", default="rules", choices=sorted(POLICIES))
    run.add_argument("--seed-prefix", default="dev")
    run.add_argument("--start", type=int, default=0, help="first seed index")
    run.add_argument("--seeds", default="", help="comma-separated explicit seeds")
    run.add_argument("--out", default="runs", help="output dir for logs/summary")
    run.add_argument("--max-steps", type=int, default=3000)
    run.add_argument("--full-state", action="store_true", help="log the full state each step")
    run.add_argument("--quiet", action="store_true")
    run.add_argument("--sts2-cli", default=None, help="path to the sts2-cli checkout")
    run.add_argument("--dotnet", default=None, help="path to a .NET 9+ dotnet executable")
    return parser


def seeds_from_args(args) -> list:
    if args.seeds:
        return [s.strip() for s in args.seeds.split(",") if s.strip()]
    return ["%s_%03d" % (args.seed_prefix, args.start + i) for i in range(args.runs)]


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        if args.runs <= 0 and not args.seeds:
            parser.error("--runs must be positive")
        try:
            cli_dir = find_sts2_cli(args.sts2_cli)
            dotnet = find_dotnet(args.dotnet)
        except EnvError as exc:
            print("setup error: %s" % exc, file=sys.stderr)
            return 2
        if not args.quiet:
            print("engine : %s" % cli_dir)
            print("dotnet : %s" % dotnet)
            print("policy : %s  character=%s  ascension=%d"
                  % (args.policy, args.character, args.ascension))

        seeds = seeds_from_args(args)
        summary = run_batch(
            seeds=seeds,
            character=args.character,
            ascension=args.ascension,
            policy_factory=POLICIES[args.policy],
            out_dir=args.out,
            max_steps=args.max_steps,
            full_state=args.full_state,
            verbose=not args.quiet,
            sts2_cli=args.sts2_cli,
            dotnet=args.dotnet,
        )
        print(format_summary(summary))
        if not args.quiet:
            print("logs: %s" % Path(args.out).resolve())
        return 0 if summary.errors == 0 else 1

    parser.error("unknown command")
    return 2
