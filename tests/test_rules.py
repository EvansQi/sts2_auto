"""Pure-state unit tests for the rule policy (no engine needed)."""

import unittest

from sts2_auto.policy.rules import RulePolicy, incoming_damage
from sts2_auto.runner import recovery_action, retry_action, state_key
from sts2_auto.policy.base import Action


def player(hp=80, max_hp=80, block=0, gold=99, deck=10):
    return {"hp": hp, "max_hp": max_hp, "block": block, "gold": gold,
            "deck_size": deck, "relics": [], "potions": []}


def card(index, name="Strike", cost=1, ctype="Attack", damage=None, block=None,
         target="AnyEnemy", can_play=True, damage_by_target=None):
    stats = {}
    if damage is not None:
        stats["damage"] = damage
    if block is not None:
        stats["block"] = block
    out = {"index": index, "id": "CARD." + name.upper().replace(" ", "_"),
           "name": name, "cost": cost, "type": ctype, "rarity": "Common",
           "can_play": can_play, "target_type": target, "stats": stats}
    if damage_by_target is not None:
        out["damage_by_target"] = damage_by_target
    return out


def enemy(index=0, hp=30, block=0, intents=None):
    return {"index": index, "name": "Enemy", "hp": hp, "max_hp": hp, "block": block,
            "intents": intents or [], "intends_attack": bool(intents)}


def combat(hand, enemies=None, energy=3, round_no=1, hp=80, block=0):
    return {"decision": "combat_play", "round": round_no, "energy": energy,
            "hand": hand, "enemies": enemies or [enemy()], "player": player(hp=hp, block=block),
            "player_powers": None, "draw_pile_count": 5, "discard_pile_count": 0}


class TestIncomingDamage(unittest.TestCase):
    def test_single_hit(self):
        self.assertEqual(incoming_damage({"enemies": [enemy(intents=[{"type": "Attack", "damage": 9}])]}), 9)

    def test_multi_hit_uses_total(self):
        e = enemy(intents=[{"type": "Attack", "damage": 3, "hits": 3, "total_damage": 9}])
        self.assertEqual(incoming_damage({"enemies": [e]}), 9)

    def test_ignores_non_attack_and_dead(self):
        e = enemy(intents=[{"type": "Debuff"}])
        dead = enemy(index=1, hp=0, intents=[{"type": "Attack", "damage": 20}])
        self.assertEqual(incoming_damage({"enemies": [e, dead]}), 0)


class TestCombatPolicy(unittest.TestCase):
    def test_lethal_preferred(self):
        state = combat(
            hand=[card(0, "Defend", ctype="Skill", block=5, target="Self"),
                  card(1, "Strike", damage=10)],
            enemies=[enemy(hp=8)],
        )
        action = RulePolicy().act(state)
        self.assertEqual(action.name, "play_card")
        self.assertEqual(action.args["card_index"], 1)
        self.assertIn("LETHAL", action.reason)

    def test_blocks_against_incoming(self):
        state = combat(
            hand=[card(0, "Strike", damage=6),
                  card(1, "Defend", ctype="Skill", block=5, target="Self")],
            enemies=[enemy(hp=40, intents=[{"type": "Attack", "damage": 9}])],
        )
        action = RulePolicy().act(state)
        self.assertEqual(action.args["card_index"], 1)

    def test_ends_turn_without_playable(self):
        state = combat(hand=[card(0, "Strike", can_play=False)], energy=2)
        action = RulePolicy().act(state)
        self.assertEqual(action.name, "end_turn")

    def test_ignores_unaffordable_card(self):
        state = combat(hand=[card(0, "Bash", cost=2, damage=8)], energy=1)
        self.assertEqual(RulePolicy().act(state).name, "end_turn")

    def test_power_deprioritised_when_low(self):
        state = combat(
            hand=[card(0, "Power", ctype="Power", target="Self"),
                  card(1, "Defend", ctype="Skill", block=5, target="Self")],
            enemies=[enemy(hp=40, intents=[{"type": "Attack", "damage": 12}])],
            hp=20,
        )
        self.assertEqual(RulePolicy().act(state).args["card_index"], 1)


class TestNonCombat(unittest.TestCase):
    def test_rest_beats_elite_at_low_hp(self):
        state = {"decision": "map_select", "player": player(hp=18, max_hp=80),
                 "choices": [{"col": 0, "row": 2, "type": "Elite"},
                             {"col": 1, "row": 2, "type": "RestSite"}]}
        action = RulePolicy().act(state)
        self.assertEqual(action.args["col"], 1)

    def test_skip_low_value_reward_on_fat_deck(self):
        state = {"decision": "card_reward", "can_skip": True,
                 "player": player(deck=30),
                 "cards": [{"index": 0, "name": "Clash", "type": "Attack",
                            "rarity": "Common", "cost": 1, "stats": {"damage": 3}}]}
        self.assertEqual(RulePolicy().act(state).name, "skip_card_reward")

    def test_take_rare_reward(self):
        state = {"decision": "card_reward", "can_skip": True,
                 "player": player(deck=30),
                 "cards": [{"index": 0, "name": "Demon Form", "type": "Power",
                            "rarity": "Rare", "cost": 3, "stats": {}}]}
        self.assertEqual(RulePolicy().act(state).name, "select_card_reward")

    def test_heal_at_low_hp(self):
        state = {"decision": "rest_site", "player": player(hp=10, max_hp=80),
                 "options": [{"index": 0, "option_id": "SMITH", "is_enabled": True},
                             {"index": 1, "option_id": "HEAL", "is_enabled": True}]}
        self.assertEqual(RulePolicy().act(state).args["option_index"], 1)

    def test_smith_at_full_hp(self):
        state = {"decision": "rest_site", "player": player(hp=80, max_hp=80),
                 "options": [{"index": 0, "option_id": "HEAL", "is_enabled": True},
                             {"index": 1, "option_id": "SMITH", "is_enabled": True}]}
        self.assertEqual(RulePolicy().act(state).args["option_index"], 1)

    def test_avoids_big_max_hp_loss(self):
        state = {"decision": "event_choice", "event_name": "Tablet of Truth",
                 "player": player(hp=80, max_hp=80),
                 "options": [
                     {"index": 0, "title": "Lose Everything",
                      "description": "Lose {DecipherMaxHpLoss} Max HP. Upgrade ALL of your cards.",
                      "vars": {"DecipherMaxHpLoss": 34}, "is_locked": False},
                     {"index": 1, "title": "Give Up",
                      "description": "Stop reading the text and leave.",
                      "vars": {}, "is_locked": False}]}
        self.assertEqual(RulePolicy().act(state).args["option_index"], 1)

    def test_repeatable_event_exits_on_second_visit(self):
        state = {"decision": "event_choice", "event_name": "Tablet of Truth",
                 "player": player(hp=80, max_hp=80),
                 "options": [
                     {"index": 0, "title": "Continue Deciphering",
                      "description": "Lose {DecipherMaxHpLoss} Max HP. Upgrade a random card.",
                      "vars": {"DecipherMaxHpLoss": 6}, "is_locked": False},
                     {"index": 1, "title": "Give Up",
                      "description": "Stop reading the text and leave.",
                      "vars": {}, "is_locked": False}]}
        policy = RulePolicy()
        policy._event_visits["Tablet of Truth"] = 1
        self.assertEqual(policy.act(state).args["option_index"], 1)


class TestRunnerHelpers(unittest.TestCase):
    def test_state_key_changes_with_hp(self):
        a = combat(hand=[], enemies=[enemy(hp=30)])
        b = combat(hand=[], enemies=[enemy(hp=20)])
        self.assertNotEqual(state_key(a), state_key(b))

    def test_retry_picks_another_event_option(self):
        state = {"decision": "event_choice",
                 "options": [{"index": 0, "is_locked": False},
                             {"index": 1, "is_locked": False}]}
        retry = retry_action(state, Action("choose_option", {"option_index": 1}))
        self.assertEqual(retry.args["option_index"], 0)

    def test_recovery_in_combat_ends_turn(self):
        self.assertEqual(recovery_action({"decision": "combat_play"}).name, "end_turn")


if __name__ == "__main__":
    unittest.main()
