# SKILL.md — 执行框架

> **本文件 = 执行框架（只管做什么、按什么顺序）**
> **策略逻辑全部在 lg.md**，由 LLM 阅读后自行判断，框架不嵌入任何策略内容。

---

## 🔄 实盘 / 模拟切换

| 模式 | 环境变量 |
|------|----------|
| 模拟 | `SIMULATE=true` |
| 实盘 | 不设置 |

---

## 📁 核心文件

| 文件 | 作用 |
|------|------|
| `trader.py` | 交易执行程序 |
| `.sim_state.json` | 模拟账户状态 |
| `blacklist.json` | 禁止交易币种 |
| `lg.md` | **策略逻辑**（入场/持仓/平仓/仓位，由 LLM 自行按 lg.md 判断） |
| `memory/*.md` | 预判记录 + 交易记录 |

---

## 📋 命令速查

```bash
cd ~/.openclaw/workspace/trading_program

# 账户
python3 trader.py status

# 扫描
python3 trader.py scan-all --top 20 --min-vol 3.0   # 空仓时扩大扫描（--klines默认等于--top）

# 市场数据
python3 trader.py market --symbol=XXX

# LLM 分析（调用外部模型）
python3 trader.py llm-hold --symbol=XXX
python3 trader.py llm-open --symbol=XXX

# 开仓（保证金是位置参数，不带 --）
python3 trader.py open-long X --symbol=XXX --leverage=10
python3 trader.py open-short X --symbol=XXX --leverage=10

# 平仓
python3 trader.py close-long --symbol=XXX
python3 trader.py close-short --symbol=XXX
python3 trader.py cancel-conditionals --symbol=XXX
```

---

## ⚙️ 执行框架（5步骤）

### 步骤1：获取数据

**1.1 账户状态**
```bash
python3 trader.py status
```
记录：总资产、持仓数、持仓币种、浮亏

**1.2 判断场景**
```
持仓数 == 0  →  场景 = 空仓
持仓数 > 0   →  场景 = 持仓巡检
```

**1.3 扫描 / 获取数据**
```
场景 = 空仓       →  scan-all --top 20 --min-vol 3.0（klines默认等于top，扩大范围找机会）
场景 = 持仓巡检   →  market --symbol=XXX（持仓币种补数据）
```

**1.4 读取 blacklist**
```bash
cat blacklist.json
```

**1.5 读取 memory（上轮预判准确率参考）**
```bash
cat memory/*.md
```
- 读取监控币种
- 读取上轮预判准确率
---

### 步骤2：加载策略章节

> 加载哪章由步骤1.2的场景决定。策略逻辑全在 lg.md，框架不解释。

| 场景 | 加载章节 |
|------|---------|
| 空仓 | lg.md 第二章（入场信号）、第三章（开仓标准）|
| 持仓巡检 | lg.md 第四章（持仓管理）、第五章（平仓标准）|

---

### 步骤3：LLM 分析决策

> **策略判断由 LLM 按 lg.md 自行完成**，框架只提供数据和环境。

**空仓时：**
LLM 按 lg.md 第二章判断每个候选币种，给出：
- 方向（做多/做空/观望）
- 入场区间、止损位、止盈位
- 保证金、杠杆
- 理由

**持仓巡检时：**
LLM 按 lg.md 第四章逐项检查持仓，给出：
- 持仓决策（持有/平仓/调整止损）
- 理由

**无合格信号时：** 选一个币种记录监控结论，空仓观望。

---

### 步骤4：执行

**4.1 检查 blacklist**
确认目标币种不在黑名单。

**4.2 执行操作**
```
LLM 结论 = 做多     →  open-long
LLM 结论 = 做空     →  open-short
LLM 结论 = 观望     →  不下单
LLM 结论 = 平仓     →  close-long / close-short
```

**4.3 取消条件单**
```bash
python3 trader.py cancel-conditionals --symbol=XXX
```

---

### 步骤5：记录 + 汇报

**5.1 写入 memory**

写入 `memory/YYYY-MM-DD.md`：
- 币种、方向、开仓价、平仓价、保证金、杠杆
- 开仓时间、平仓时间、盈亏金额、盈亏率%
- 入场时预判（LLM 步骤3 的完整输出）
- 预判验证：实际走势 vs 预判
- 预判准确度：完全准确 / 方向对但幅度错 / 方向错 / 时机对但没拿住
- 交易定性：依据成立的正常交易 / 预判有偏差的试错
- 下次改进点

空仓观望时：
- 监控币种 + 不入场原因 + 预判方向

**5.2 汇报**

持仓时必须汇报：
```
币种：XXX | 方向：XXX | 持仓：XXX枚 @XXX
当前价：XXX | 盈亏率：XXX% | 绝对盈亏：$XXX
距止损：XXX% | 趋势预判：[涨/跌/震荡]
本轮动作：[无动作/已平仓/已开仓]
```

---

### 📋 全流程 CheckList

```
步骤1：获取数据
  ☐ 1.1 账户状态
  ☐ 1.2 判断场景（空仓/持仓）
  ☐ 1.3 扫描候选 / market持仓币
  ☐ 1.4 读 blacklist
  ☐ 1.5 读 memory（可选）
步骤2：加载策略章节
  ☐ 加载对应 lg.md 章节
步骤3：LLM 分析决策
  ☐ LLM 按 lg.md 自行判断
  ☐ 输出结论（方向+操作）
步骤4：执行
  ☐ 确认不在黑名单
  ☐ 执行对应操作
  ☐ 取消条件单
步骤5：记录 + 汇报
  ☐ 写 memory
  ☐ 汇报结果
全部完成 ✅
```

---

## ⚠️ 重要约束

- 实盘账号需要有真实 USDT 余额（不设置 `SIMULATE`）
- API Key 从 `~/.openclaw/workspace/.binance_api` 和 `.binance_secret` 读取
- 严禁模拟执行或虚构数据
- 全权托管，不需要确认，全自动执行
- **框架不嵌入任何策略逻辑**，策略全在 lg.md

---

## 🏦 统一账户（PM）

- `trader.py` 自动检测账户类型并路由
- PM 账户使用 `papi/v1/um/order` 接口
- ⚠️ PM 账户无法通过 API 查询活跃条件单，条件单通过 `.conditional_orders.json` 追踪