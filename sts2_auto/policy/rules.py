"""Rule + heuristic policy for a full StS2 run.

Design goals (v1):
  * never emit an illegal action - only cards with can_play are considered;
  * combat: lethal check -> block what is actually incoming -> value plays;
  * non-combat: simple, explainable preferences;
  * every action carries a reason/score so runs can be replayed and debugged.

This is deliberately a baseline. Search/learning comes later.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import Action, Policy

# --- state helpers -------------------------------------------------------


def player(state: Dict[str, Any]) -> Dict[str, Any]:
    return state.get("player") or {}


def hp(state: Dict[str, Any]) -> int:
    return int(player(state).get("hp", 0) or 0)


def max_hp(state: Dict[str, Any]) -> int:
    return max(1, int(player(state).get("max_hp", 1) or 1))


def hp_ratio(state: Dict[str, Any]) -> float:
    return hp(state) / max_hp(state)


def gold(state: Dict[str, Any]) -> int:
    return int(player(state).get("gold", 0) or 0)


def deck_size(state: Dict[str, Any]) -> int:
    return int(player(state).get("deck_size", 0) or 0)


def alive_enemies(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [e for e in (state.get("enemies") or []) if int(e.get("hp", 0) or 0) > 0]


def incoming_damage(state: Dict[str, Any]) -> int:
    """Total damage the enemies intend to deal this turn."""
    total = 0
    for enemy in alive_enemies(state):
        for intent in enemy.get("intents") or []:
            if intent.get("total_damage") is not None:
                total += int(intent["total_damage"] or 0)
            elif intent.get("damage") is not None:
                total += int(intent["damage"] or 0) * int(intent.get("hits", 1) or 1)
    return total


def card_damage(card: Dict[str, Any], target_index: Optional[int]) -> int:
    """Damage this card deals to one target (or summed for multi-target)."""
    stats = card.get("stats") or {}
    rows = card.get("damage_by_target") or []
    if target_index is not None:
        for row in rows:
            if row.get("target_index") == target_index:
                return int(row.get("damage", 0) or 0)
    if rows and target_index is None:
        return sum(int(r.get("damage", 0) or 0) for r in rows)
    return int(stats.get("damage", 0) or 0)


def card_block(card: Dict[str, Any]) -> int:
    return int((card.get("stats") or {}).get("block", 0) or 0)


def needs_target(card: Dict[str, Any]) -> bool:
    return card.get("target_type") == "AnyEnemy"


# --- policy --------------------------------------------------------------

_RARITY_SCORE = {"Rare": 3.0, "Uncommon": 2.0, "Common": 1.0, "Basic": 0.4}
_TYPE_SCORE = {"Power": 1.6, "Attack": 0.7, "Skill": 0.6, "Status": -4.0, "Curse": -5.0}

_MAP_SCORE = {
    "RestSite": 2.5,
    "Rest": 2.5,
    "Elite": 2.0,
    "Treasure": 2.2,
    "Shop": 1.8,
    "Event": 1.2,
    "Unknown": 1.1,
    "Monster": 1.0,
    "Boss": 50.0,
}


class RulePolicy(Policy):
    name = "rules"

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._event_visits = {}

    def reset(self) -> None:
        self._event_visits = {}

    # -- dispatch ---------------------------------------------------------

    def act(self, state: Dict[str, Any]) -> Action:
        decision = state.get("decision", "")
        handler = getattr(self, "_" + decision, None)
        if handler is None:
            return Action("proceed", reason="no handler for " + decision)
        return handler(state)

    # -- combat -----------------------------------------------------------

    def _combat_play(self, state: Dict[str, Any]) -> Action:
        hand = state.get("hand") or []
        energy = int(state.get("energy", 0) or 0)
        playable = [c for c in hand if c.get("can_play") and int(c.get("cost", 0) or 0) <= energy]
        if not playable:
            return Action("end_turn", reason="no playable card")

        block_now = int(player(state).get("block", 0) or 0)
        need = max(0, incoming_damage(state) - block_now)
        round_no = int(state.get("round", 0) or 0)

        best: Optional[Action] = None
        for card in playable:
            targets = self._targets(card, state)
            if not targets:
                continue
            for target in targets:
                score, reason = self._score_card(state, card, target, need, round_no)
                if best is None or score > (best.score or -1e9):
                    args = {"card_index": card["index"]}
                    if target is not None:
                        args["target_index"] = target
                    best = Action("play_card", args, reason, score)

        if best is None:
            return Action("end_turn", reason="nothing worth playing")
        if (best.score or 0) < -10.0:
            return Action("end_turn", reason="all plays dangerous (best %.2f)" % (best.score or 0))
        return best

    def _targets(self, card: Dict[str, Any], state: Dict[str, Any]) -> List[Optional[int]]:
        enemies = alive_enemies(state)
        if not enemies:
            return []
        if needs_target(card):
            return [e.get("index", i) for i, e in enumerate(enemies)]
        return [None]

    def _score_card(self, state, card, target, need, round_no):
        stats = card.get("stats") or {}
        ctype = card.get("type", "")
        cost = int(card.get("cost", 0) or 0)
        enemies = alive_enemies(state)

        score = 0.0
        bits = []

        # damage
        if target is not None:
            target_enemy = next((e for e in enemies if e.get("index") == target), None)
            ehp = int(target_enemy.get("hp", 0) or 0) + int(target_enemy.get("block", 0) or 0) if target_enemy else 0
            dmg = card_damage(card, target)
            effective = min(dmg, ehp)
            score += 2.0 * effective + 0.25 * max(0, dmg - ehp)
            bits.append("dmg=%d" % dmg)
            if dmg >= ehp and ehp > 0:
                score += 7.0 + (3.0 if len(enemies) == 1 else 0.0)
                bits.append("LETHAL")
        elif ctype == "Attack":
            dmg = sum(card_damage(card, e.get("index", i)) for i, e in enumerate(enemies))
            score += 1.6 * dmg
            bits.append("aoe=%d" % dmg)

        # block (only worth what is actually incoming)
        blk = card_block(card)
        if blk:
            if need > 0:
                score += 2.6 * min(blk, need) + 0.35 * max(0, blk - need)
            else:
                score += 0.15 * blk  # block expires; don't burn energy on it
            bits.append("block=%d/need=%d" % (blk, need))

        # self-damage cards (Bloodletting etc.) are lethal at low HP
        hp_loss = 0
        for key, val in stats.items():
            if "hp" in key and ("loss" in key or "cost" in key):
                hp_loss = max(hp_loss, int(val or 0))
        if hp_loss:
            score -= 0.8 * hp_loss
            if hp(state) <= hp_loss + 5:
                score -= 25.0
            bits.append("hp_loss=%d" % hp_loss)

        # draw / energy / buffs / debuffs
        for key, weight in (("cards", 0.6), ("draw", 0.9), ("energy", 1.4),
                            ("stars", 0.8)):
            val = int(stats.get(key, 0) or 0)
            if val:
                score += weight * val
                bits.append("%s=%d" % (key, val))
        for key, weight in (("strength", 2.2), ("dexterity", 1.8), ("focus", 2.2),
                            ("vulnerable", 1.5), ("weak", 1.4), ("poison", 1.3),
                            ("thorns", 1.0)):
            val = int(stats.get(key, 0) or 0)
            if val:
                score += weight * val
                bits.append("%s=%d" % (key, val))

        # card type priors
        if ctype == "Power":
            score += 3.5 if round_no <= 3 else 0.8
            if hp_ratio(state) < 0.35 and need > 0:
                score -= 3.0
            bits.append("power")
        elif ctype == "Skill" and not blk and not any(stats.get(k) for k in ("cards", "draw", "energy")):
            score += 0.2

        if "Exhaust" in (card.get("keywords") or []):
            score -= 0.2
        if cost == 0:
            score += 0.4
        score -= 0.15 * cost

        reason = "%s [%s] %s" % (card.get("name", "?"), card.get("id", "?"), " ".join(bits))
        return score, reason

    # -- map --------------------------------------------------------------

    def _map_select(self, state: Dict[str, Any]) -> Action:
        choices = state.get("choices") or []
        if not choices:
            return Action("select_map_node", {"col": 0, "row": 1}, "no choices; fallback")
        ratio = hp_ratio(state)
        scored = []
        for c in choices:
            ctype = c.get("type", "Monster")
            score = _MAP_SCORE.get(ctype, 1.0)
            if ctype in ("RestSite", "Rest") and ratio < 0.55:
                score += 3.0
            if ctype == "Elite":
                if ratio < 0.7 or deck_size(state) < 12:
                    score -= 3.0
                else:
                    score += 1.0
            if ctype == "Shop" and gold(state) >= 150:
                score += 1.2
            if ctype == "Event" and ratio < 0.4:
                score -= 0.8
            scored.append((score, c))
        scored.sort(key=lambda pair: (-pair[0], pair[1].get("col", 0)))
        best_score, best = scored[0]
        return Action(
            "select_map_node",
            {"col": best["col"], "row": best["row"]},
            "map %s score=%.2f hp=%.0f%%" % (best.get("type"), best_score, ratio * 100),
            best_score,
        )

    # -- rewards / rest / shop / events ----------------------------------

    def _card_value(self, card: Dict[str, Any]) -> float:
        stats = card.get("stats") or {}
        score = _RARITY_SCORE.get(card.get("rarity", "Common"), 1.0)
        score += _TYPE_SCORE.get(card.get("type", "Skill"), 0.5)
        score += 0.04 * (int(stats.get("damage", 0) or 0) + int(stats.get("block", 0) or 0))
        if int(card.get("cost", 0) or 0) == 0:
            score += 0.4
        if int(card.get("cost", 0) or 0) >= 3:
            score -= 0.2
        return score

    def _card_reward(self, state: Dict[str, Any]) -> Action:
        cards = state.get("cards") or []
        can_skip = state.get("can_skip", True)
        if not cards:
            return Action("skip_card_reward", reason="no cards offered")
        scored = sorted(((self._card_value(c), c) for c in cards),
                        key=lambda pair: -pair[0])
        best_score, best = scored[0]
        deck = deck_size(state)
        # Thin decks want quality; fat decks should stop adding average cards.
        threshold = 1.8 if deck < 12 else 2.2
        if can_skip and best_score < threshold and deck >= 12:
            return Action("skip_card_reward", reason="best %.2f below %.2f" % (best_score, threshold), score=best_score)
        return Action("select_card_reward", {"card_index": best["index"]},
                      "take %s %.2f" % (best.get("name"), best_score), best_score)

    def _rest_site(self, state: Dict[str, Any]) -> Action:
        options = [o for o in (state.get("options") or []) if o.get("is_enabled", True)]
        if not options:
            return Action("leave_room", reason="no rest options")
        heal = next((o for o in options if "HEAL" in str(o.get("option_id", "")).upper()), None)
        smith = next((o for o in options if "SMITH" in str(o.get("option_id", "")).upper()), None)
        ratio = hp_ratio(state)
        if ratio < 0.6 and heal is not None:
            return Action("choose_option", {"option_index": heal["index"]}, "heal at %.0f%% hp" % (ratio * 100))
        if smith is not None:
            return Action("choose_option", {"option_index": smith["index"]}, "smith at %.0f%% hp" % (ratio * 100))
        if heal is not None:
            return Action("choose_option", {"option_index": heal["index"]}, "heal fallback")
        return Action("choose_option", {"option_index": options[0]["index"]}, "first option")

    @staticmethod
    def _event_hp_loss(opt: Dict[str, Any]) -> int:
        """Max HP the option would take away, read from its vars."""
        text = " ".join(str(opt.get(k, "")) for k in ("title", "description")).lower()
        if "lose" not in text:
            return 0
        loss = 0
        for key, val in (opt.get("vars") or {}).items():
            if "hp" in key.lower() and isinstance(val, (int, float)):
                loss = max(loss, int(val))
        return loss

    def _event_choice(self, state: Dict[str, Any]) -> Action:
        options = [o for o in (state.get("options") or []) if not o.get("is_locked")]
        if not options:
            return Action("leave_room", reason="no unlocked event option")
        ratio = hp_ratio(state)
        maxhp = max_hp(state)
        event_name = state.get("event_name") or "?"
        visits = self._event_visits.get(event_name, 0)
        exit_words = ("give up", "leave", "stop", "refuse", "walk away", "back out")

        best = None
        for opt in options:
            text = " ".join(str(opt.get(k, "")) for k in ("title", "description")).lower()
            score = 0.0
            if any(w in text for w in exit_words):
                score += 0.6 + (2.0 if ratio < 0.35 else 0.0)
            if "remove" in text:
                score += 2.0
            if "upgrade" in text:
                score += 1.5
            if "max hp" in text and "lose" not in text:
                score += 1.0
            if "heal" in text and "lose" not in text:
                score += 0.8 if ratio < 0.6 else 0.2
            if "relic" in text:
                score += 0.8
            if "curse" in text:
                score -= 2.5
            if "gold" in text and "lose" in text:
                score -= 0.5

            loss = self._event_hp_loss(opt)
            if loss:
                score -= 2.0 + 6.0 * (loss / float(maxhp))
                if loss > 5:
                    score -= 3.0
            if visits > 0:
                # repeatable events (Tablet of Truth etc.) are a trap: take the exit
                if any(w in text for w in exit_words):
                    score += 4.0
                elif any(w in text for w in ("continue", "keep", "again", "further")):
                    score -= 4.0

            if best is None or score > best[0]:
                best = (score, opt)
        assert best is not None
        score, opt = best
        self._event_visits[event_name] = visits + 1
        return Action("choose_option", {"option_index": opt["index"]},
                      "event %s option=%s score=%.1f visit=%d" % (event_name, opt.get("title"), score, visits + 1),
                      score)

    def _shop(self, state: Dict[str, Any]) -> Action:
        # v1: never buy. Card removal / relic policy comes with run-level planning.
        return Action("leave_room", reason="shop skipped (v1)")

    # -- generic screens --------------------------------------------------

    def _card_select(self, state: Dict[str, Any]) -> Action:
        cards = state.get("cards") or []
        if not cards:
            return Action("skip_select", reason="no cards to select")
        count = 1
        for key in ("min_cards", "num_cards", "count", "select_count"):
            if state.get(key) is not None:
                count = int(state[key])
                break
        count = max(0, min(count, len(cards)))
        indices = ",".join(str(c.get("index", i)) for i, c in enumerate(cards[:count]))
        return Action("select_cards", {"indices": indices}, "select first %d" % count)

    def _bundle_select(self, state: Dict[str, Any]) -> Action:
        return Action("select_bundle", {"bundle_index": 0}, "first bundle")

    def _unknown(self, state: Dict[str, Any]) -> Action:
        return Action("proceed", reason="unknown screen")

    def _game_over(self, state: Dict[str, Any]) -> Action:
        return Action("proceed", reason="should not be called")
