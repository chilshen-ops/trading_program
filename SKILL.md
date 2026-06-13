# SKILL.md — trader.py 工具框架
> 你是资深的加密货币交易员，自己完成交易，进步，迭代。
> **本文档只描述工具怎么用 + 程序硬约束，不教 LLM 怎么做交易决策。**
> 决策（开/平/仓位/止损）由 LLM 自由决定，参考 `lg.md` 市场语言词典。

---

## 0. 运行环境

- **工作目录**：`/Volumes/data/openclaw-workspace/trading_program/`
- **trader.py**：币安 PM 统一账户，papi 接口（fapi 无权限）
- **API Key/Secret**：trader.py 同目录 `.binance_api` + `.binance_secret`
- **限速器**：Binance 6000w/min（程序自动限速，重启后从文件恢复）
- **MEMORY 文件**：`memory/YYYY-MM-DD.md`（每日一篇，cron 自动 append）

---

## 1. 16 个命令清单

| # | 命令 | 作用 | 何时用 |
|---|------|------|--------|
| 1 | `status` | 账户 + 持仓 + IC 状态 + 无止损报警 | **第一步必调** |
| 2 | `market` | 5 尺度 K 线 + 形态 token + 原始 volume | 精读某个币种 |
| 3 | `scan-all` | 候选池 + 5m 摘要（v6 原始 volume）| 空仓扫货 |
| 4 | `trade-stats` | 30 天实盘 PnL + 胜率 + 5% 地板穿透 | 看历史业绩 |
| 5 | `ic-weights` | 方向 IC / 幅度 IC / Rank IC + L 档降级 | 看模型健康度 |
| 6 | `open-long` | 开多（自动设 3% 紧止损）| LLM 决定开仓 |
| 7 | `open-short` | 开空（自动设 3% 紧止损）| LLM 决定开仓 |
| 8 | `close-long` | 平多 | LLM 决定平仓 |
| 9 | `close-short` | 平空 | LLM 决定平仓 |
| 10 | `replace-order` | 改条件单（默认 `current × 0.98` / `× 1.02`）| LLM 调止损 |
| 11 | `cancel-conditionals` | 取消所有未触发条件单 | LLM 重置 |
| 12 | `check-ladder` | 持仓巡检（浮盈档位 + peak 锁档 + 建议）| 步骤 1a 必调 |
| 13 | `analyze-position` | 持仓 §14 4 问脚本化（趋势/动能/量价 + 判定）| 步骤 1b 必调 |
| 14 | `sync-sim` | sim 模式同步本地 journal | sim 模式 |
| 15 | `verify-sim` | sim 模式验证 journal 完整性 | sim 模式 |
| 16 | `verify-memory` | 验证 memory 是否按 §10.0 必填标题填 (5/5) | **步骤 4 后置必调** |

---

## 2. 命令详解

### 2.1 `status`

```bash
python3 trader.py status
```

**输出**：账户总资产、余额、浮盈、持仓列表、IC 状态、L 档（**仅展示，不影响开仓**）。

### 2.2 `market`

```bash
market --symbol BTCUSDT [--interval 1d] [--interval 4h] [--interval 1h] [--interval 15m] [--interval 5m] [--kline-last N] [--format classic|kronos]
```

| 参数 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `--symbol` | ✅ | - | **必须带 USDT 后缀**（如 `BTCUSDT`）。裸 base 也接受，但禁止传 `TAU` 这种"自动补出来会撞车"的名字 |
| `--kline-last` | 否 | 20 | 每个周期输出最近 N 根 K 线 |
| `--interval` | 否 | `30m 1h` | 可选 `1m/5m/15m/30m/1h/4h/1d`，可多次指定 |
| `--format` | 否 | `classic` | `classic` 原始数字 / `kronos` 离散 token + 三视角提示 |

**多尺度推荐（必传 5 尺度）**：
```bash
--interval 1d --interval 4h --interval 1h --interval 15m --interval 5m
```

**为什么 5m 必传**：Kronos 论文 arXiv:2508.02739 用 5m K 线（288 根/天 vs 1h 仅 24 根，信息量 12 倍）。1h 静默时段不靠 5m 会判"横盘"误判。

**输出**：header 格式 `## {iv} (X of Y bars)`，真实反映窗口大小（v6 修复）。

### 2.3 `scan-all`

```bash
scan-all --top N --klines N --min-vol F --max-vol-24h F --max-chg-24h F --kline-detail
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--top` | 30 | Top N 候选 |
| `--min-vol` | 0.5 | 最低 1h 波动率（%）|
| `--max-vol-24h` | 50.0 | 24h 波动率上限（%）|
| `--max-chg-24h` | 20.0 | 24h 涨跌幅上限（%）|
| `--kline-detail` | 关 | 打印 1d × 5 + 4h × 5 + 1h × 5 + 15m × 5 + 5m × 12 内嵌 K 线 |

**5m 摘要行（v6 2026-06-08）**：
```
1. SAGAUSDT   1h-vol=0.15% 24h-vol=6.25% 24h-chg=+0.51% price=0.01376
   └─ 5m×12: ➡️ chg=-0.64% 涨6/跌6 vol=456703,3720215,454008,1660499,...
```
- **vol=原始12个数字**（Kronos 论文 volume 是 optional，v6 改回原始数据）
- **量比/atrpct 不再算**（避免工程化加工，LLM 自己看 vol 数字判断放量/缩量）

**kline-detail 内嵌**（v4 2026-06-08）：每币额外打 1d × 5 + 4h × 5 + 1h × 5 + 15m × 5 + 5m × 12 五尺度，cron 一次扫描拿全数据。

### 2.4 `trade-stats`

```bash
python3 trader.py trade-stats --days=30
```

**输出**：实盘 PnL + 胜率 + 5% 地板穿透统计 + LONG/SHORT 分胜率。30 天分页（币安单窗口 ≤ 7 天限制）。

### 2.5 `ic-weights`

```bash
python3 trader.py ic-weights
```

**输出**：
- 方向 IC / 幅度 IC / Rank IC（最近 20 笔）
- LONG / SHORT 分胜率
- **L 档降级**：L0/L1/L2/L3 + 仓位系数（**仅作参考，程序自动调减，不拒交易**）
- 动态权重表（1d/4h/1h/15m）—— LLM 可自由用其他权重

### 2.6 `open-long` / `open-short`

```bash
python3 trader.py open-long  --symbol BTCUSDT [--margin N] [--leverage N] [--stop-pct F] [--trigger N]
python3 trader.py open-short --symbol BTCUSDT [--margin N] [--leverage N] [--stop-pct F] [--trigger N]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `margin` (位置参数) | **1.0** | 保证金（USDT），**小于 1 也补到 1**（最小可开仓）|
| `--leverage` | 10 | 杠杆 |
| `--stop-pct` | - | ❌ **未实装**（v3 简化后不需要）|
| `--trigger` | 不传 | 条件单触发价；不传 = 默认 `current × 0.97`（多）/ `× 1.03`（空）= 3% 紧止损 |

**止损逻辑**（v4 2026-06-09 简化）：
- 开仓成功后**程序自动设止损**，LLM 无需手动
- 止损 = `entry × 0.97` (多) / `entry × 1.03` (空) = 3% 紧
- **❌ 不设止盈**（用户原话"不要止赢, 只要止损就好"）— LLM 自由决定平仓时机

**止损保底**（v5 2026-06-09）：
- 开仓后 3 次重试设止损
- 3 次全失败 → **程序自动平仓**（避免"无止损持仓"风险）

**保证金保底**（程序硬约束）：
- LLM 不传 margin → 默认 1.0
- LLM 传 0.5 → 救到 1.0
- L1 × 0.5 后 < 1 → 救到 1.0
- L2/L3 max(margin × 0.X, 1.0)
- **任何状态都保证 margin ≥ 1**，避免开仓失败

**程序层硬约束**（与 LLM 决策无关）：
- 保证金 < 1 → 补到 1（保底 1 USDT 开仓）
- notional < 5 → 调高杠杆（**5 USDT 是币安 USDⓈ-M 最小名义价值**，不是最小保证金）
  - 举例：1 USDT 保证金 × 10x 杠杆 = **10 USDT notional** ≥ 5 ✅
  - 举例：1 USDT 保证金 × 5x 杠杆 = 5 USDT notional ✅（刚好够）
  - 所以 **1 USDT 保证金就够开仓**，不需要等资金 ≥ 5
- 同 symbol 已有同方向持仓 → 拒绝
- L1 降级 → 保证金自动 × 0.5
- L2 降级 → 保证金自动 × 0.25
- L3 降级 → 保证金自动 × 0.1
- **不拒交易**（lg.md 11.1 原则）

**⚠️ 资金下限常见误解**（cron 必读）：
- ❌ 错：总资产 < 5 USDT → 不能开仓
- ✅ 对：保证金 ≥ 1 USDT + 杠杆调到 notional ≥ 5 → 可以开仓
- 当前 $3.36 资产 → 默认 margin=1 USDT, 用 5x~20x 杠杆, 完全可以交易
- "资金少"不是不开仓的理由, 杠杆就是为小资金设计的

### 2.7 `close-long` / `close-short`

```bash
python3 trader.py close-long  --symbol BTCUSDT [--qty N]   # 不传 qty = 全平
python3 trader.py close-short --symbol BTCUSDT [--qty N]
```

### 2.8 `replace-order`

```bash
python3 trader.py replace-order --symbol BTCUSDT --side LONG [--trigger N] [--qty N]
```

- 不传 `--trigger` → 默认 `current × 0.98`（多）/ `× 1.02`（空）
- 传 `--trigger` → LLM 自己算的具体价格
- `--qty` 不传 = 当前持仓数量

### 2.9 `cancel-conditionals`

```bash
python3 trader.py cancel-conditionals --symbol BTCUSDT   # 取消某币种
python3 trader.py cancel-conditionals                    # 取消所有
```

### 2.10 `check-ladder`

```bash
python3 trader.py check-ladder
```

**输出**：所有持仓的浮盈档位 + 阶梯建议 + peak 锁档（**只上不下 v6**）。
- 心理锁档表：未到 / 保本 / 锁 1% / 锁 2% / ...（LLM 决策参考）
- 实际 trigger：`max(历史 peak, current × 0.98)`（LONG 场景）— 浮盈跌回去**不返档**
- peak 持久化：`.ladder_peak.json`（跨 cron 进程有效）

**触发条件**：浮盈 ≥ 2% 才动；< 2% 保留开仓时挂的 3% 紧止损。
**LLM 必走**：cron 步骤 1a，调一次拿所有持仓建议。

### 2.11 `analyze-position`

```bash
python3 trader.py analyze-position --symbol XYZ
```

**输出**：单个持仓的 §14 4 问脚本化分析（前 3 问自动算 + Q4 LLM 自己答 + 判定）。

```
Q1 趋势走完?  [走完/持续/不明]  + 证据
Q2 动能衰退?  [衰退/正常/不明]  + 证据
Q3 量价背离?  [背离/配合/正常]  + 证据
Q4 反向支撑?  (LLM 列 3 个理由 + 强度)

判定: 主动平仓 / 锁档 / 持有 / 止损等待
```

**判定逻辑**：
- `weak_count ≥ 3 且 浮盈 > 0` → 主动平仓
- `weak_count ≥ 2 且 浮盈 > 0` → 锁档
- `浮盈 < -3%` → 止损等待
- 其他 → 持有

**LLM 必走**：cron 步骤 1b，对每个持仓调一次。

### 2.12 `sync-sim` / `verify-sim`

仅 sim 模式用。实盘不写本地 journal（币安服务端是权威）。

### 2.13 `verify-memory`

```bash
python3 trader.py verify-memory
# 或指定日期:
python3 trader.py verify-memory --date 2026-06-10
```

**输出**：
```
🔍 2026-06-10 memory 验证 (lg.md §10.0.1)
文件: /Volumes/data/openclaw-workspace/trading_program/memory/2026-06-10.md
大小: 1019 字节

必填标题 (5 个):
  ❌ 缺  ### 持仓巡检（步骤 1a + 1b）
  ❌ 缺  ### 候选池（步骤 2
  ❌ 缺  ### 决策（步骤 3）
  ❌ 缺  ### 持仓 4 问分析
  ❌ 缺  ### Bug / 异常

结果: 0/5 必填标题在
```

**5/5 = 通过**，< 5/5 = 报警 → LLM 重写 memory。

**LLM 必走**：cron 步骤 4 写完 memory 后**立即调一次**。

---

## 3. cron 标准循环（4 步：含持仓巡检 v6）

**第一步永远是"持仓巡检"**——两步联动：
- **1a 阶梯巡检**（lg.md §7 v6 被动保护）：调 `check-ladder` → 到档就 `replace-order`，**只上不下 + peak 持久化**
- **1b 持仓分析**（lg.md §14 主动预判）：对每个持仓调 `analyze-position` → 4 问脚本化 + 判定 → 决定主动平仓 / 锁档 / 不动

### 1a 阶梯巡检（lg.md §7 v6 只上不下）

**lg.md §7 阶梯表**（主流程）：
- **2% 保本** → `trigger = entry`（不亏钱）
- **3% 锁 1%** → `trigger = current × 0.98` (LONG) / `× 1.02` (SHORT)
- **4% 锁 2%** → 同上 trigger
- **5% 锁 3%** → 同上 trigger
- **6%+ 锁 N%** → 同上 trigger
- **< 2%** → 不动（沿用开仓时挂的 3% 紧止损）

**v6 "只上不下" 逻辑**：
- LONG: `final_trigger = max(历史 peak, current × 0.98)` — 浮盈跌回去**不返档**
- SHORT: `final_trigger = min(历史 peak, current × 1.02)` — 浮盈跌回去**不返档**
- peak 持久化：`.ladder_peak.json`（跨 cron 进程有效）
- 只有当本轮 target 优于历史 peak 时才调 `replace-order`

### 1b 持仓分析（lg.md §14 主动预判）✅ 必走

**4 个必问的预测问题**（程序脚本化前 3 问 + LLM 答 Q4）：
1. **当前趋势是否在走完**？（程序算：1d/4h 高点是否不再抬高）
2. **动能是否衰退**？（程序算：C-O 缩窄 + 影线变长）
3. **量价是否配合**？（程序算：价高量低 = 背离）
4. **反向视角反驳是否强**？（LLM 列举 3 个"为什么现在不该平"的理由）

**判定框架**：
- 4 个问题 3+ 个"趋势走完" → **主动平仓**（调 `close-long/short`）
- 浮盈 ≥ 3% + 阶梯锁档 + 动能衰退 → **先锁档再观察**
- 4 个都"趋势还在" → 继续持有
- 浮亏 + 止损未穿 + 信号未变 → 继续持有
- 浮亏 + 止损已穿 → 等止损触发（程序自动）
- 浮亏 + 止损未穿 + 3+ 个"趋势反转" → **主动平仓**（不等止损）

### 完整步骤

```bash
# 步骤 1a: 阶梯巡检（lg.md §7 v6 被动保护）✅ 强制
python3 trader.py status                       # 看持仓列表 + 无止损报警
python3 trader.py check-ladder                 # 看每个持仓浮盈档位 + peak 锁档 + 建议
# 到档就调: replace-order --symbol=X --side=SELL/BUY --trigger=<建议>

# 步骤 1b: 持仓分析（lg.md §14 主动预判）✅ 必走 — 对每个持仓调
python3 trader.py analyze-position --symbol=X
# 拿 4 问脚本化结果 + 答 Q4 + 判定 → 决定 close / replace / 不动

# 步骤 2: 拿数据（只看候选池，状态已步骤 1 拿过）
python3 trader.py ic-weights
python3 trader.py scan-all --top=20 --kline-detail --min-vol=0.5 --max-vol-24h=20 --max-chg-24h=10

# 步骤 3: LLM 读 lg.md §7/§10/§14 看市场 + 自己决定开/平/不动

# 步骤 4: 写 memory（lg.md §10.0 统一模板, 无交易也要写）
```

**为什么步骤 1a + 1b 都要走**：
- §7 阶梯是"被动保本"（防白赚的利润丢光）
- §14 预测是"主动平仓"（趋势反转 / 动能耗尽 / 量价背离）
- **只走 §7 = 错过主动平仓信号**（参见 2026-06-09 INUSDT -10% 事故）

**trigger 计算**：
- ≥ 3% 档位一律 `current × 0.98` (LONG) / `× 1.02` (SHORT) — `replace-order` 不传 `--trigger` 时默认值
- 2% 保本档 `trigger = entry` — 需 LLM 显式 `--trigger=entry`
- < 2% 不动
- **v6 终极保护**：实际 trigger = `max(历史 peak, target)` — 浮盈跌回不返档

**memory 模板**（lg.md §10.0 完整版 - LLM 必须按字面填，列名都不能改）：

```markdown
## YYYY-MM-DD HH:MM Cron 执行汇报

### Cron 总览
| 字段 | 值 |
|------|-----|
| 账户余额 | $X.XX |
| 浮盈 | $+/-X.XX |
| 持仓数 | N 笔 |
| IC 状态 | L0/L1/L2/L3 (仓位系数 X) |
| Cron 步骤 | 1a✅ 1b✅ 2✅ 3✅ 4✅ |

### 持仓巡检（步骤 1a + 1b）
| 币种 | 方向 | 数量 | 入场 | 现价 | 浮盈% | 阶梯档 | peak 锁档 | 1b 判定 |
|------|------|------|------|------|-------|--------|----------|--------|
| XYZ | LONG | N | $X | $X | +/-X% | 锁X%/保本/未到 | 锁 $X | 主动平仓/锁档/持有/止损等待 |

### 候选池（步骤 2, scan-all Top 10）
| 排名 | 币种 | 1h-vol | 24h-vol | 24h-chg | 价格 | 5m 摘要 (chg / 涨N跌N / vol=原始 12 数字) |
|------|------|--------|---------|---------|------|--------------------------------------------|
| 1 | XYZ | X% | X% | +/-X% | $X | chg X% 涨N/跌N vol=a,b,c,d,e,f,g,h,i,j,k,l |

### 决策（步骤 3）
- **动作**: 开仓 XYZ / 平仓 XYZ / 替换止损 / 不动
- **理由**: 1-2 句
- **反向视角** (3 条必填):
  1. "为什么开/平?" → 反驳或支撑
  2. "为什么开/平?" → 反驳或支撑
  3. "为什么开/平?" → 反驳或支撑
- **关键信号** (1-3 条): 5 尺度 + 量价

### 持仓 4 问分析（每个持仓必答，步骤 1b）
**XYZ LONG @ $X**:
| # | 问题 | 信号 | 证据 |
|---|------|------|------|
| 1 | 趋势走完? | 走完/持续/不明 | (从 analyze-position 复制) |
| 2 | 动能衰退? | 衰退/正常/不明 | (从 analyze-position 复制) |
| 3 | 量价背离? | 背离/配合/正常 | (从 analyze-position 复制) |
| 4 | 反向支撑? | (LLM 列 3 个) | "1. ...; 2. ...; 3. ..." |
| 判定 | 主动平仓/锁档/持有/止损等待 | weak_count=X/3 | (从 analyze-position 复制) |

### Bug / 异常
- 无 / [描述]
```

**强制约束**：
- **不能改列名** (LLM 不能把 "5m 摘要" 改成 "5m 状态", 不能把 "1h-vol" 改成 "1d 趋势")
- **Q1/Q2/Q3 + 判定必须从 `analyze-position` 命令拿**, 禁止 LLM 拍脑袋
- **反向视角强制 3 条** (决策表必填项, 管开/平/不动都要列)
- **数字 + 描述**, 不写"严禁/禁止/不能/必须"

**强制校验（步骤 4 后置）✅**：

```bash
python3 trader.py verify-memory
```

输出 0/5 必填标题 = LLM 漏填 → 重写；5/5 = 通过。**5 个必填标题**（lg.md §10.0.1）：
1. `### Cron 总览` (v2 2026-06-10 新加, 之前漏)
2. `### 持仓巡检（步骤 1a + 1b）`
3. `### 候选池（步骤 2, scan-all Top 10）`
4. `### 决策（步骤 3）`
5. `### 持仓 4 问分析（每个持仓必答，步骤 1b）`
6. `### Bug / 异常`

**反例（2026-06-10 cron 输出 0/6 必填 5/6）**：
- 用"## 1. 账户状态巡检"而不是"### Cron 总览"
- 持仓巡检表只 6 列，少 数量/入场/现价/阶梯档/peak 锁档 5 列
- 候选池表改成"1d 趋势/4h 动能/1h 量价/5m 状态"（**改了我的列名**）
- 决策缺"3 条反向视角"
- 4 问是 LLM 自己拍的（没调 analyze-position）

**没有 5 步、没有 checklist、没有 9 项必检项**——除"持仓巡检 + 持仓分析"外 LLM 自由判断。

---

## 4. `--symbol` 严谨用法

| 写错 | 实际 | 建议 |
|------|------|------|
| `--symbol=TAU` | 拼成 `TAUUSDT`（base=TAU）但 LLM 可能想要 `TAUSDT`（base=TA）| **必须带 USDT 后缀**：`--symbol=TAUSDT` |
| `--symbol=BTC` | 自动补 `BTCUSDT` ✅ | OK |
| `--symbol=BTCUSDT` | ✅ | 推荐 |

**禁止**写 `TAU` 这种"自动补会撞车"的名字（币安有 TAUSDT 和 TAUUSDT 两个交易对，差 1 字符）。

---

## 5. 程序层硬约束（与策略无关，LLM 无需关心）

| 行为 | 说明 |
|------|------|
| 限速器 | Binance 6000w/min，程序自动 acquire + 持久化跨进程 |
| 余额/持仓/订单合法性 | 不合法时明确报错，不静默吞异常 |
| 重复开仓 | 同 symbol 已有同方向持仓 → 拒绝 |
| IC 仓位调减 | L1/L2/L3 状态自动 × 0.5/0.25/0.1 |
| 止损 set 失败 | 3 次重试 → 失败明确报错 + 建议 close |

**这些是程序级，不影响 LLM 决策**。LLM 决定"开不开仓"，程序决定"开了之后保证金够不够"。

---

## 6. Bug 状态表（已修才列）

> **本表只列"已修但容易复发"的 bug，避免 LLM 重复报已修 bug**。
> 留空 = 最近没复发 bug。

| Bug | 修法 | 验证方法 |
|-----|------|----------|
| （空）| | |

LLM 发现新 bug → 写入 `memory/YYYY-MM-DD.md` 并报告用户，**不直接改 trader.py / SKILL.md / lg.md**。

---

## 7. 📌 总结

| 文件 | 角色 | 谁写 |
|------|------|------|
| **trader.py** | 执行层（拉数据 + 调仓 + 下单）| 用户/我 |
| **SKILL.md** | 工具框架（14 命令 + 程序约束）| 用户/我 |
| **lg.md** | 市场语言词典（5 尺度 + 3 视角 + 量价状态）| 用户/我 |
| **memory/YYYY-MM-DD.md** | LLM 的记忆（每日决策历史）| LLM（cron 自动写）|

**LLM 的工作流**：
1. 调 `status` + `ic-weights` 看自己状态
2. 调 `scan-all` 看候选池
3. 对感兴趣的币调 `market` 看 5 尺度
4. 读 `lg.md` 查市场语言
5. **自己决定**开不开仓、仓位多少、止损在哪
6. 调对应 `open-*` / `close-*` / `replace-order`
7. 写 `memory/YYYY-MM-DD.md`

**没有任何"必须按 X 规则做"**——LLM 自由发挥，程序只负责执行和数据。
