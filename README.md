# sts2_auto

杀戮尖塔 2（Slay the Spire 2）自动化爬塔 —— 从最小可跑闭环开始。

当前版本只做一件事：**用真实游戏引擎 headless 跑完整对局，并用规则/启发式策略做决策，输出可复现的胜率统计。**

## 状态（v0.1）

| 模块 | 内容 | 状态 |
|---|---|---|
| 接入层 | [sts2-cli](https://github.com/wuhao21/sts2-cli) JSON stdin/stdout（真实引擎，IL patch 后 headless 运行） | 完成 |
| 策略 | rules：斩杀 → 按意图格挡 → 价值出牌；地图/奖励/休息/事件规则 | 完成 |
| 基线 | random：随机出牌/选路，用于配对比较 | 完成 |
| 评估 | 固定种子批量跑、胜率、平均层数、Act 到达率、逐决策 JSONL 日志 | 完成 |
| 搜索 / RL / 真实游戏桥接 | 见路线图 | 待做 |

> 环境版本：**StS2 v0.107.1（Steam public，build 23811903）**。
> 版本号来自游戏目录的 release_info.json；Steam 切分支后务必重跑 sts2-cli 的 setup，否则
> headless 引擎仍会用旧 DLL。sts2-cli 官方验证的是 v0.111.0，所以 0.107.1 需要打一个小补丁（见下）。

## 快速开始

前置：

1. Steam 安装《杀戮尖塔 2》；
2. .NET 9 SDK（本机装在 %USERPROFILE%/.dotnet9 也能被自动发现）；
3. Python 3.9+。

~~~bash
# 1) 准备 headless 引擎（只需一次；游戏更新后重跑）
git clone https://github.com/wuhao21/sts2-cli.git
cd sts2-cli
#    sts2-cli main 面向 v0.111.0；在 v0.107.1 上需要先应用兼容补丁
git apply ../sts2_auto/patches/sts2-cli-v0.107.1.patch
./setup.sh "E:/SteamLibrary/steamapps/common/Slay the Spire 2"

# 2) 跑一局
cd sts2_auto
python -m sts2_auto run --runs 1

# 3) 跑 20 个固定种子并保存日志/汇总
python -m sts2_auto run --runs 20 --policy rules --out runs/rules_v1

# 4) 与随机基线对比（同种子）
python -m sts2_auto run --runs 20 --policy random --out runs/random_v1
~~~

补丁很小，只改 RunSimulator.cs 两处：

1. `AssemblyInfo.Init()` 在 0.107 不存在 → 改成反射调用（0.111 有就调，没有就跳过）；
2. `CreatureCmd.Damage(...)` 的 5 参重载在 0.107 不存在 → 改用两边都有的 4 参重载。

如果 sts2-cli 不在默认搜索路径，设置环境变量：

~~~powershell
$env:STS2_CLI_DIR = "C:/path/to/sts2-cli"
$env:STS2_DOTNET  = "C:/Users/Administrator/.dotnet9/dotnet.exe"   # 可选
~~~

## 基线结果（v0.107.1，Ironclad A0，种子 dev_000–dev_019）

| 策略 | 胜率 | 平均层数 | 死在第 17 层（Act 1 Boss） | 逐种子对比 |
|---|---|---|---|---|
| rules | 0/20 | **13.7** | 6 | 17 胜 / 1 负 / 2 平 |
| random | 0/20 | 9.0 | 2 | — |

结论很明确：**规则 bot 已经显著强于随机，但还过不了 Act 1 Boss，胜率仍是 0。**
这符合调研里的预期——第一版的目标是「跑通闭环 + 可复现评估」，不是胜率。
当前最大瓶颈是 Boss 战（6/20 死在第 17 层），下一步优先做战斗搜索和药水策略。

## 输出

每次批量运行会在 --out 目录生成：

- seed.jsonl：逐步决策记录（决策点、动作、参数、理由、分数；加 --full-state 可存完整状态）
- summary.json：所有 run 的结果与汇总

控制台会打印胜率、平均层数、Act 到达率和每局结果。退出码：有 run 出错时为 1。

## 架构

~~~text
python -m sts2_auto run
        |
        v
runner.play_run()          固定种子、循环决策、卡死恢复、日志
        |  state (dict)
        v
policy.RulePolicy.act()    纯函数式决策，不直接碰引擎
        |  Action(name, args, reason, score)
        v
env.HeadlessEnv            一个引擎进程 = 一局；JSON 行协议、超时、清理
        |
        v
Sts2Headless (C#) -> sts2.dll（真实游戏引擎，IL patched）
~~~

设计约束：

1. **接入层与决策层分离**：策略只接收状态字典、返回动作字典，方便以后换成搜索/RL/LLM，也方便单测。
2. **只发合法动作**：战斗只考虑 can_play == true 且费用足够的牌；每步动作前引擎还会再校验一次。
3. **失败要可诊断**：每个动作带 reason 和 score；卡死时用状态指纹检测并强制推进。
4. **可复现**：固定种子 + 固定策略版本；评估时同种子配对比较。

## 规则策略做了什么

战斗（policy/rules.py::_combat_play）：

1. 只枚举可打出的牌与该打的敌人；
2. 斩杀线优先：damage >= hp + block 时给极高权重；
3. 按敌人意图计算本回合真实 incoming damage，格挡只按「需要的量」计价，溢出格挡价值很低；
4. 伤害 / 抽牌 / 能量 / 力量敏捷 / 易伤虚弱等按权重打分，Power 牌在前 3 回合加分、残血时不抢节奏；
5. 没有值得打的牌就结束回合（不会为了清能量乱打）。

非战斗：

| 决策 | v0.1 规则 |
|---|---|
| 地图 | 低血量加权重休息，高血量才考虑精英，金币多偏好商店 |
| 卡牌奖励 | 稀有度 + 类型 + 数值打分；卡组够厚时跳过低分牌 |
| 休息 | HP < 60% 回血，否则升级 |
| 事件 | 文本启发式：删牌/升级/最大生命加分，掉血/诅咒减分 |
| 商店 | 暂时直接离开（下一步再做删牌与遗物估值） |

## 路线图

- **v0.2 战斗**：1 步前瞻（模拟每个动作后的状态）、药水使用、目标选择改进（威胁度而非残血）。
- **v0.3 战斗搜索**：枚举回合内出牌序列 + 1–3 回合 beam search，叶节点估值（HP/敌方 HP/powers）。
- **v0.4 整局**：商店删牌/遗物、事件表、休息与升级的价值评估、地图路线 beam。
- **v0.5 真机**：接 STS2MCP / CharTyr 之类的桥接，把同一套策略搬到真实游戏（接入层替换，策略层不动）。
- **可选**：LLM 只做高层规划/事件；RL 先复现 Act 1 战斗。

## 目录

~~~text
sts2_auto/
  env.py                 # sts2-cli 进程与 JSON 协议
  runner.py              # 单局驱动 + 批量评估 + 汇总
  cli.py                 # python -m sts2_auto run ...
  policy/base.py         # Action / Policy 接口
  policy/rules.py        # 规则 + 启发式策略
  policy/random_policy.py
docs/protocol.md         # 决策点与动作协议（按真实日志整理）
tests/                   # 纯状态单测，不需要启动游戏
~~~

## 备注

- sts2-cli 会把游戏 DLL 复制到自己的 lib/ 并打 IL patch；不要把游戏 DLL / 反编译代码提交到本仓库。
- 单人自动化风险低，但联机使用会影响队友；不要在联机/竞技场景默认开启。
- 当前成绩只是 baseline，用于验证闭环；不要拿单场战斗胜率当通关率。
