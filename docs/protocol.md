# sts2-cli JSON 协议（决策点与动作）

本文档按 v0.107.1 的真实日志整理（与 v0.111.0 的公共字段一致）；字段名全部是 snake_case。

## 顶层

引擎启动后先输出一行：

~~~json
{"type": "ready", "version": "0.2.0"}
~~~

之后每条命令返回一个 **decision**：

~~~json
{"type": "decision", "decision": "combat_play", "context": {"act": 1, "floor": 2, "room_type": "Monster"}}
~~~

context 在大多数决策点都有：act、act_name、floor、room_type、boss。

## 命令

~~~json
{"cmd": "start_run", "character": "Ironclad", "seed": "dev_000", "ascension": 0, "lang": "en"}
{"cmd": "action", "action": "play_card", "args": {"card_index": 0, "target_index": 0}}
{"cmd": "quit"}
~~~

## 决策点

### map_select

~~~json
{"decision": "map_select", "choices": [{"col": 1, "row": 1, "type": "Monster"}],
 "player": {"hp": 80, "max_hp": 80, "gold": 99}, "act": 1, "floor": 1}
~~~

动作：select_map_node + {"col": ..., "row": ...}。

### combat_play

~~~json
{
  "decision": "combat_play", "round": 1, "energy": 3, "max_energy": 3,
  "hand": [{"index": 0, "id": "CARD.STRIKE_IRONCLAD", "name": "Strike", "cost": 1,
            "type": "Attack", "rarity": "Basic", "can_play": true,
            "target_type": "AnyEnemy", "stats": {"damage": 6},
            "damage_by_target": [{"target_index": 0, "name": "Shrinker Beetle", "damage": 6}]}],
  "enemies": [{"index": 0, "name": "Shrinker Beetle", "hp": 39, "max_hp": 39,
               "block": 0, "intents": [{"type": "DebuffStrong"}], "intends_attack": false}],
  "player": {"hp": 80, "max_hp": 80, "block": 0, "gold": 99, "deck_size": 10},
  "player_powers": null, "draw_pile_count": 5, "discard_pile_count": 0
}
~~~

攻击意图：

~~~json
{"type": "Attack", "damage": 5, "hits": 2, "total_damage": 10}
~~~

> damage 是每段伤害，total_damage 是总伤害；单段攻击只有 damage。
> 计算 incoming 时优先用 total_damage。

动作：

- play_card + {"card_index": i}；需要目标时再加 {"target_index": j}（仅 target_type == "AnyEnemy" 需要）。
- end_turn
- use_potion + {"potion_index": i}（可选目标 target_index）

### card_reward

~~~json
{"decision": "card_reward", "can_skip": true, "gold_earned": 12,
 "cards": [{"index": 0, "name": "Iron Wave", "type": "Attack", "rarity": "Common",
            "cost": 1, "stats": {"damage": 5, "block": 5}}]}
~~~

动作：select_card_reward + {"card_index": i} 或 skip_card_reward。

### rest_site

~~~json
{"decision": "rest_site", "options": [{"index": 0, "option_id": "HEAL", "is_enabled": true}]}
~~~

动作：choose_option + {"option_index": i}；也可 leave_room。

### event_choice

~~~json
{"decision": "event_choice", "event_name": "Neow",
 "options": [{"index": 0, "title": "...", "description": "...", "is_locked": false}]}
~~~

动作：choose_option + {"option_index": i} 或 leave_room。

### shop

~~~json
{"decision": "shop", "cards": [], "relics": [], "potions": [],
 "card_removal_cost": 75, "player": {"gold": 120}}
~~~

动作：buy_card / buy_relic / buy_potion / remove_card / leave_room。

### card_select / bundle_select

多选界面：select_cards + {"indices": "0,1"}，或 skip_select。
奖励 bundle：select_bundle + {"bundle_index": i}。

### game_over

~~~json
{"decision": "game_over", "victory": false, "act": 1, "floor": 9,
 "player": {"hp": 0, "max_hp": 85}}
~~~

## 常见坑

1. 不用 can_play 过滤会发出非法动作；引擎会返回 {"type": "error"}。
2. 多段攻击不要用 damage * hits 再叠加一次 total_damage。
3. 战斗状态没有顶层 act/floor，要读 context。
4. 一局结束后直接 quit；批量评估时每局开新进程最稳。
