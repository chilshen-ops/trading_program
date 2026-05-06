---
name: trading-skill
description: Binance 永续合约量化交易
---

# 交易框架 v9.2 (2026-05-04)

**对齐策略** | 核心：无指标，纯量价博弈，赚1亿usdt

## ⚠️ 核心原则

| 角色 | 职责 |
|------|------|
| **程序(trader.py)** | 获取K线数据、账户状态、执行交易（自动1.5%止损+自动止盈） |
| **LLM(我)** | 分析原始OHLCV数据、做出决策、判断入场/出场,全托管，全自动 |

程序不包含任何交易逻辑，所有判断由LLM根据原始K线数据做出。
**策略禁令：严禁使用MACD/RSI/均线/布林带/ATR等任何技术指标**

---

## 🔄 实盘 / 模拟交易切换

| 模式 | 环境变量 | 说明 |
|------|----------|------|
| **实盘** | 不设置 `SIMULATE` | 连接币安真实账户 |
| **模拟** | `SIMULATE=true` | 虚拟资金，不真实下单 |

---

## 📁 核心文件

| 文件 | 作用 |
|------|------|
| `STRATEGY.md` | 策略文件（最高优先级） |
| `trader.py` | 执行程序（v18.0） |
| `.sim_state.json` | 模拟账户余额+持仓+止损单持久化 |
| `blacklist.json` | 黑名单（permanent_delist + coins） |
| `memory/` | 历史教训库 |

---

## 📋 交易命令

```bash
cd ~/.openclaw/workspace/trading_program

# =============================================
# 扫描候选币种（推荐配置）
# =============================================
# 10个候选约30秒，5个候选约16秒
python3 trader.py scan-all --top 10 --min-vol 5.0 --klines 10

# =============================================
# 账户操作
# =============================================
python3 trader.py status                          # 账户状态
python3 trader.py market --symbol=BTCUSDT       # 市场数据

python3 trader.py llm-hold --symbol=XXX          # LLM分析持仓
python3 trader.py llm-open --symbol=XXX          # LLM分析开仓

# =============================================
# 交易执行
# =============================================
python3 trader.py open-long 10 --symbol=XXX --leverage=10    # 开多
python3 trader.py open-short 10 --symbol=XXX --leverage=10    # 开空
python3 trader.py close-long --symbol=XXX                      # 平多
python3 trader.py close-long --symbol=XXX --percent=50              # 平多(减仓50%)
python3 trader.py close-short --symbol=XXX                     # 平空
python3 trader.py close-short --symbol=XXX --percent=50            # 平空(减仓50%)
```

---

## 🔍 选币流程

```
全量 ticker (559个)
  → 排除 blacklist.json 黑名单
  → 排除 24h成交额 < 500万U
  → 排除 24h波动率 < min_vol%（默认5%）
  → 按 24h成交量 降序取 Top N（--klines 控制）
  → 批量获取 5m/30m/1h/24h K线
  → 喂给 LLM 做量价博弈分析
```

**K线权重：5m:40% | 30m:30% | 1h:20% | 24h:10%**

**K线输出格式**：
```
2026-05-04 02:30 | 2.445500 | 2.477900 | 2.424500 | 2.430300 | 2057342 USDT
```

---

## ⚙️ 策略执行流程 （6步骤，不可以跳过-铁律）

### 步骤1：读取策略
读取 `STRATEGY.md` 按策略规则执行。
读取 `memory/` 历史教训库，调整策略,持仓币种预测走势修正
读取后需回复 已读
如果有什么调整需回复 已调整

### 步骤2：获取币种
```bash
python3 trader.py scan-all --top 10 --min-vol 5.0 --klines 10
```
查看记忆有没有要求扩大范围

### 步骤3：检查+执行
持仓为空 → 执行步骤2：
- 开仓→根据总资产，胜率，仓位比例，计算出合理的仓位比例(20-80%)，杠杠倍数(1-10)，然后执行开仓（开多开空都要计算）
- 观望→如果没有可交易的币种(无信号)则扩大 `--top --klines ` 阈值，范围10-50
持仓不为空 → 执行步骤4持仓管理

- 程序自动设置 1.5% 止损（开仓时）



### 步骤4：持仓管理

- 浮亏超 1.5% → 程序自动止损
- 浮盈≥1% → 移动止损到保本
- 浮盈≥2% → 移动止损到保本+1%
- 浮盈≥3% → 移动止损到保本+2%
以此类推，确保止损一直小于浮盈1%
- 出现反向信号 → LLM判断是否平仓
- 判断当前走势是否破坏原有逻辑。如果逻辑未变，坚持持有；如果逻辑破坏，立即平仓。

### 步骤5：总结教训
平仓后写入 `memory/YYYY-MM-DD.md`：
- 交易对、方向、入场价格
- 持仓币种预测走势 vs 实际走势，差在哪里
- 下次如何优化
- 币种扫描参数 --top 和 --klines 10-50 两个参数同步调整，用于下周期

### 步骤6：规范详细汇报（每次交易后必报）
1. 持仓状态
2. 交易结果（盈亏金额）
3. 交易原因（基于哪些K线数据）
4. 总结
5. 总资产

---

## ⚠️ 重要约束

- 实盘账号需要有真实 USDT 余额（不设置 `SIMULATE`）
- API Key 从 `~/.openclaw/workspace/.binance_api` 和 `.binance_secret` 读取

---

## 🏦 统一账户（PM_2）交易用法

- `trader.py` 自动检测账户类型并路由，无需手动切换
- 统一账户使用 `papi/v1/um/order` 接口（单向持仓）
- 普通账户使用 `fapi/v1/order` 接口（双向持仓 positionSide=LONG/SHORT）

---

## 📝 策略优化规则

1. **模拟爆仓后**：优化策略，然后重置账户
2. **单笔亏损后**：检查是否需要优化策略