# 规则策略设计（v0.1）

本文说明 sts2_auto 当前的决策层：它看什么状态、怎么打分、为什么这样取舍、哪里一定会出错。
权重和阈值都按代码里的实际数值写，方便对照修改。

## 1. 目标与边界

v0.1 只追求三件事：

1. **闭环**：从 start_run 到 game_over 不崩、不卡死，能完整跑完一局；
2. **可解释**：每个动作都有 reason 和 score，失败局可以逐步回放；
3. **可复现**：固定种子 + 固定权重，同版本下结果一致。

它不追求高胜率，也不做搜索、学习、药水规划、商店经济、卡组协同。这些是 v0.2 之后的事。

策略是纯函数：

~~~text
state(dict) -> Action(name, args, reason, score)
~~~

策略不直接碰引擎，也不修改状态；动作由 runner 发给引擎，引擎返回下一个决策点。
因此策略可以脱离游戏做单元测试（tests/test_rules.py 有 18 个用例）。

## 2. 决策流水线

~~~text
读取 state
  |
  +-- combat_play   -> 战斗打分（第 3 节）
  +-- map_select    -> 地图评分（第 4 节）
  +-- card_reward   -> 卡牌价值 + 跳牌阈值（第 5 节）
  +-- rest_site     -> 回血 / 升级（第 6 节）
  +-- event_choice  -> 文本 + vars 风险评分（第 7 节）
  +-- shop          -> 直接离开（第 8 节）
  +-- card_select   -> 选前 N 张（第 8 节）
  +-- bundle_select -> 选第 0 个（第 8 节）
  +-- unknown       -> proceed
  |
  v
Action
~~~

每个决策点只做一件事，不做跨决策点的长期规划。策略只保留一份记忆：
_event_visits（同一个事件访问了几次），用来识别可重复事件陷阱。

## 3. 战斗策略

### 3.1 输入

战斗状态里实际用到的字段：

| 字段 | 用途 |
|---|---|
| hand[].can_play / cost | 过滤非法、付不起的牌 |
| hand[].target_type | 只有 AnyEnemy 需要选目标 |
| hand[].stats.damage / damage_by_target | 对每个目标的实际伤害（含力量、易伤） |
| hand[].stats.block | 格挡值 |
| hand[].stats.cards / energy / strength / vulnerable 等 | 抽牌、能量、buff/debuff |
| hand[].type | Attack / Skill / Power |
| enemies[].hp / block | 有效血量 = hp + block |
| enemies[].intents | 本回合 incoming damage |
| player.hp / max_hp / block | 生存状态 |
| round | 前 3 回合给 Power 加权 |

incoming damage 的算法（incoming_damage）：

- 优先用 intent.total_damage；
- 没有 total_damage 时用 damage；
- 有 hits 且没有 total_damage 时用 damage * hits；
- 只统计存活敌人。

> 多段攻击在 0.107 / 0.111 的协议里是 damage=每段、hits=段数、total_damage=总伤。
> 用 total_damage 可以避免「总伤 × 段数」的重复计算。

### 3.2 合法动作

不枚举「所有动作」，只枚举当前值得考虑的动作：

1. 只保留 can_play == true 且 cost <= energy 的牌；
2. target_type == AnyEnemy 的牌，对每个存活敌人各生成一个候选；
3. 其他牌只生成一个候选（不传 target_index）；
4. 没有候选就 end_turn。

这样不会发出非法动作，也避免把非法动作送进打分函数。

### 3.3 战斗打分公式

对每个候选 (card, target) 计算：

~~~text
score = 0

# 伤害
有效伤害 = min(实际伤害, 目标 hp + block)
score += 2.0 * 有效伤害
score += 0.25 * 溢出伤害
若 实际伤害 >= hp + block：score += 7（单敌人再 +3）

# 格挡
需要格挡 = max(0, incoming - 当前 block)
若 need > 0: score += 2.6 * min(block, need) + 0.35 * 溢出格挡
若 need == 0: score += 0.15 * block

# 自伤（Bloodletting 等）
score -= 0.8 * hp_loss
若 hp <= hp_loss + 5: score -= 25

# 抽牌 / 能量 / 星星
cards +0.6/张，draw +0.9/张，energy +1.4/点，stars +0.8/点

# buff / debuff
strength +2.2/层，dexterity +1.8/层，focus +2.2/层
vulnerable +1.5/层，weak +1.4/层，poison +1.3/层，thorns +1.0/层

# 牌型先验
Power：round <= 3 时 +3.5，否则 +0.8；hp < 35% 且有 incoming 时 -3.0
Skill 且没有 block / 抽牌 / 能量：+0.2

# 其他
Exhaust：-0.2
0 费：+0.4
费用惩罚：-0.15 * cost
~~~

最后取分数最高的候选；如果最高分 < -10，本回合结束（避免自杀式出牌）。
否则即使分数不高也会出牌，因为「留着能量不用」通常更差。

### 3.4 为什么是这些权重

- **伤害 2.0 / 格挡 2.6（只算需要的部分）**：HP 是整局资源，挡掉真实伤害略优于打伤害；
  但溢出格挡只值 0.35，因为格挡回合结束就清空，多挡等于白花能量。
- **斩杀 +7（单敌人 +3）**：杀掉一个敌人等于永久移除它之后的输出，不只是省这一回合的血。
- **Power 前 3 回合 +3.5**：能力牌越早越赚；但残血时 -3，避免「上能力然后被打死」。
- **自伤牌 -25 的硬阈值**：Bloodletting 在 5 HP 时打出就是自杀，必须有硬约束。
- **0 费 +0.4**：0 费牌机会成本低，但不是无条件优先。
- **费用 -0.15**：轻惩罚，避免为了打高费牌而放弃两张更高效的牌。

### 3.5 目标选择

目标不是固定「打第一个」，而是对每个敌人算一遍分数后取最大：

- 能斩杀的目标额外 +7，所以优先收残血；
- 打不死时，伤害按有效血量截断，打高血量敌人不会因为「溢出」虚高；
- 没有单独考虑「谁威胁最大」——这是 v0.2 要补的（例如优先打高 intent 伤害的敌人）。

### 3.6 什么时候结束回合

满足任一条件：

- 没有可打出的牌（费用不够 / 全部 can_play=false）；
- 所有候选分数都 < -10（例如只剩自伤牌且血量危险）。

策略不会为了「把能量花完」而打 0 价值的牌；但 need=0 时打 Defend 仍有 0.15×block 的正分，
所以通常还是会打掉——这是有意的，因为回合结束能量本来就浪费。

### 3.7 已知局限

1. **纯贪心，无前瞻**：每张牌只看当前状态，不模拟「打完这张之后下张打什么」；
   典型错误是为了上 Bash 的易伤而少挡一次致命伤害。
2. **没有药水**：player.potions 完全没读，Boss 战攒的药水全部浪费。
3. **没有回合内序列规划**：出牌顺序靠单步分数，可能错过「先抽牌再斩杀」的组合。
4. **没有能量预算**：不会为下回合保留能量（当前也没有保留能量的机制）。
5. **AOE / 多目标**：只对 AnyEnemy 逐目标枚举，没有专门处理打全体的牌。
6. **Boss 阶段转换**：不识别 Boss 的 phase，不会为下一阶段留资源。

## 4. 地图

对每个可选节点算分，取最高（同分按列号小优先）：

| 节点 | 基础分 | 修正 |
|---|---:|---|
| RestSite | 2.5 | HP < 55% 时 +3.0 |
| Elite | 2.0 | HP < 70% 或卡组 < 12 张时 -3.0；否则 +1.0 |
| Treasure | 2.2 | 无 |
| Shop | 1.8 | 金币 >= 150 时 +1.2 |
| Event | 1.2 | HP < 40% 时 -0.8 |
| Unknown | 1.1 | 无 |
| Monster | 1.0 | 无 |
| Boss | 50 | 无 |

设计意图：

- 低血量时休息的权重明显高于一切（2.5 + 3.0 = 5.5），避免残血贪精英；
- 精英不是不能打，而是要求 HP > 70% 且卡组 >= 12 张；
- 商店只在钱够的时候才值得绕路；
- Unknown 略高于普通战斗，因为事件期望收益通常更好，但低血量时会被 Event 修正拉低。

> 0.107 的节点类型是 RestSite（不是 Rest），代码同时兼容两者。

## 5. 卡牌奖励

卡牌价值：

~~~text
value = 稀有度分 + 类型分 + 0.04 * (damage + block)
        + 0.4（0 费）
        - 0.2（费用 >= 3）

稀有度：Rare 3.0 / Uncommon 2.0 / Common 1.0 / Basic 0.4
类型：Power 1.6 / Attack 0.7 / Skill 0.6 / Status -4.0 / Curse -5.0
~~~

选择逻辑：

- 卡组 < 12 张：总是拿分最高的牌（尽快建立卡组强度）；
- 卡组 >= 12 张：如果最高分 < 2.2 且允许跳过，就跳过；
- Boss 奖励等 can_skip == false 的场合不跳过。

问题：没有卡组协同（力量 / 易伤 / 消耗流），也没有费用曲线；看到单卡质量高但不搭的牌会拿。
当前只是防止卡组无限膨胀的粗糙规则。

## 6. 休息

1. 只考虑 is_enabled == true 的选项；
2. HP < 60% 且存在回血选项 -> 回血；
3. 否则存在升级选项 -> 升级；
4. 否则回血；再否则选第一个。

60% 是拍脑袋的阈值，没有按 Boss 距离 / 当前 Act 调整；药水、遗物（如皇家枕头）也没纳入。

## 7. 事件

事件没有结构化效果，只有 title / description / vars。策略用文本启发式 + vars 数值：

| 信号 | 分数 |
|---|---:|
| 离开 / 放弃类选项（give up / leave / stop / refuse / walk away / back out） | +0.6，HP < 35% 时再 +2.0 |
| remove | +2.0 |
| upgrade | +1.5 |
| 获得 max hp（且不含 lose） | +1.0 |
| heal（且不含 lose） | HP < 60% 时 +0.8，否则 +0.2 |
| relic | +0.8 |
| curse | -2.5 |
| 失去金币 | -0.5 |
| 失去 HP / Max HP（从 vars 读损失值 L） | -2.0 - 6.0 * (L / max_hp)，L > 5 再 -3.0 |

**重复事件陷阱**：同一个 event_name 被访问过之后（visits > 0）：

- 退出类选项 +4.0；
- continue / keep / again / further 类选项 -4.0。

这条规则来自实战 bug：Tablet of Truth 会连续提供「失去 3 -> 6 -> 12 -> 24 -> 34 最大生命，
升级随机牌」的选项。旧版本只看文本，把升级当成正收益，一路选到最大生命只剩 1，
下一场战斗必死。现在第一次访问就可能选择退出，第二次访问强烈倾向退出。

局限：事件文本是英文（lang=en），中文事件 / 新事件可能匹配不到关键词；vars 命名也不统一。

## 8. 商店、多选、未知

| 决策 | 当前行为 | 原因 |
|---|---|---|
| shop | leave_room | 还没有删牌 / 遗物 / 药水的价值模型，乱买不如不买 |
| card_select | 选前 N 张 | 不知道任务要求时的最保守选择；N 从 min_cards / num_cards / count / select_count 读，默认 1 |
| bundle_select | 选第 0 个 | 同上 |
| unknown | proceed | 未知界面的通用推进 |
| 无 handler | proceed | 防止新决策类型直接崩掉 |

## 9. 安全与恢复

1. **只发合法动作**：战斗侧过滤 can_play 与费用；引擎侧还会再校验一次。
2. **卡死检测**：runner 对状态做指纹（decision / round / hp / block / energy / 手牌 id / 敌人 hp+block），
   连续 5 次相同就强制推进（战斗结束回合、地图选第一个、奖励跳过、其他离开）。
3. **引擎报错重试**：动作返回 type=error 时，事件界面会换一个未锁选项重试；其他界面走恢复动作。
   重试仍失败才判本局失败。
4. **超时**：单次命令 90 秒；超过即判失败并关闭进程。
5. **每局独立进程**：避免上一局状态泄漏到下一局。

## 10. 伪代码

~~~python
def act(state):
    handler = handlers.get(state["decision"])
    if handler is None:
        return Action("proceed")

    if state["decision"] == "combat_play":
        playable = [c for c in state["hand"]
                    if c["can_play"] and c["cost"] <= state["energy"]]
        if not playable:
            return Action("end_turn")

        need = max(0, incoming_damage(state) - state["player"]["block"])
        best = None
        for card in playable:
            for target in targets(card, state["enemies"]):
                score = score_card(state, card, target, need, state["round"])
                best = max(best, (score, card, target))
        if best.score < -10:
            return Action("end_turn")
        return Action("play_card", {"card_index": best.card.index,
                                    "target_index": best.target})

    return handler(state)
~~~

## 11. 参数总表

| 参数 | 值 | 位置 |
|---|---:|---|
| 有效伤害权重 | 2.0 | _score_card |
| 溢出伤害权重 | 0.25 | _score_card |
| 斩杀奖励 | +7（单敌人 +3） | _score_card |
| 需要格挡权重 | 2.6 | _score_card |
| 溢出格挡权重 | 0.35 | _score_card |
| 无 incoming 时格挡权重 | 0.15 | _score_card |
| 自伤惩罚 | 0.8/点，致命 -25 | _score_card |
| Power 早期奖励 | +3.5（round <= 3） | _score_card |
| Power 残血惩罚 | -3.0（hp < 35%） | _score_card |
| 结束回合阈值 | 最高分 < -10 | _combat_play |
| 地图休息加成 | +3.0（hp < 55%） | _map_select |
| 精英惩罚 | -3.0（hp < 70% 或 deck < 12） | _map_select |
| 跳牌阈值 | 2.2（deck >= 12） | _card_reward |
| 休息阈值 | 60% | _rest_site |
| 事件重复惩罚 | 退出 +4.0 / 继续 -4.0 | _event_choice |
| 卡死阈值 | 连续 5 次 | runner.play_run |

## 12. 下一步怎么改

按预期收益排序：

1. **1 步前瞻**（v0.2）：对每个候选动作，把动作发给引擎、读回新状态、用新状态重新评估
   （HP、敌方剩余 HP、powers、手牌质量）。引擎每步一次往返约 1–2 ms，单回合 3 能量 × 5 张牌
   的枚举完全可接受，能直接修掉「为了上易伤不挡刀」这类错误。
2. **药水**：player.potions 已有 index / name / description / target_type，按治疗 / 格挡 / 伤害
   三类在低血或 Boss 战使用；动作是 use_potion + potion_index。
3. **目标威胁度**：把敌人 intent 伤害加进目标选择，优先打掉下回合威胁最大的敌人。
4. **回合内序列搜索**（v0.3）：枚举出牌序列 + 1–3 回合 beam search，叶节点估值
   HP + 敌方 HP + 力量 / 易伤层数 + 药水价值。
5. **卡组协同**：维护力量 / 易伤 / 消耗等标签，奖励里加入与现有卡组的协同分；
   商店再解锁删牌与关键遗物。
