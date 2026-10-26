"""Random baseline policy (mirrors sts2-cli's play_full_run.py)."""

from __future__ import annotations

import random
from typing import Any, Dict

from .base import Action, Policy


class RandomPolicy(Policy):
    name = "random"

    def __init__(self, seed: str = "0") -> None:
        self.rng = random.Random(seed)

    def act(self, state: Dict[str, Any]) -> Action:
        decision = state.get("decision", "")

        if decision == "combat_play":
            hand = state.get("hand") or []
            enemy_indices = [e.get("index", i) for i, e in enumerate(state.get("enemies") or [])
                             if e.get("hp", 0) > 0]
            playable = [c for c in hand if c.get("can_play")]
            if playable:
                card = self.rng.choice(playable)
                args = {"card_index": card["index"]}
                if card.get("target_type") == "AnyEnemy" and enemy_indices:
                    args["target_index"] = self.rng.choice(enemy_indices)
                return Action("play_card", args, "random card")
            return Action("end_turn", reason="no playable cards")

        if decision == "map_select":
            choices = state.get("choices") or []
            if choices:
                c = self.rng.choice(choices)
                return Action("select_map_node", {"col": c["col"], "row": c["row"]}, "random node")
            return Action("select_map_node", {"col": 0, "row": 1}, "fallback node")

        if decision == "card_reward":
            cards = state.get("cards") or []
            if cards:
                return Action("select_card_reward", {"card_index": 0}, "random reward")
            return Action("skip_card_reward", reason="no cards")

        if decision == "event_choice":
            options = [o for o in (state.get("options") or []) if not o.get("is_locked")]
            if options:
                return Action("choose_option", {"option_index": options[0]["index"]}, "first option")
            return Action("leave_room", reason="no options")

        if decision == "rest_site":
            options = [o for o in (state.get("options") or []) if o.get("is_enabled", True)]
            if options:
                return Action("choose_option", {"option_index": options[0]["index"]}, "first rest option")
            return Action("leave_room", reason="no rest options")

        if decision == "shop":
            return Action("leave_room", reason="random: never shop")

        if decision == "card_select":
            cards = state.get("cards") or []
            if cards:
                return Action("select_cards", {"indices": "0"}, "first card")
            return Action("skip_select", reason="no cards")

        if decision == "bundle_select":
            return Action("select_bundle", {"bundle_index": 0}, "first bundle")

        if decision == "unknown":
            return Action("proceed", reason="unknown screen")

        return Action("proceed", reason="fallback for " + decision)
