# Binance Perpetual Futures Trading Bot

币安永续合约量化交易机器人，支持模拟交易/实盘交易。

## 功能特性

- **多周期K线分析**：5m / 30m / 1h / 24h 数据采集
- **智能选币**：基于波动率、量价关系筛选交易机会
- **双向交易**：支持做多、做空操作
- **止损机制**：自动1.5%止损保护
- **模拟交易**：支持虚拟资金测试，无需真实账户
- **Portfolio Margin 支持**：自动适配统一账户

## 快速开始

### 安装依赖

```bash
pip install requests certifi
```

### 配置 API Key

**方式一：环境变量**
```bash
# Linux/macOS
export BINANCE_API_KEY="your_api_key"
export BINANCE_API_SECRET="your_api_secret"

# Windows PowerShell
$env:BINANCE_API_KEY="your_api_key"
$env:BINANCE_API_SECRET="your_api_secret"
```

**方式二：创建配置文件**
```bash
# 在项目目录创建 .env 文件
echo "BINANCE_API_KEY=your_api_key" > .env
echo "BINANCE_API_SECRET=your_api_secret" >> .env
```

### 启动模拟交易

```bash
# 方式一：环境变量
SIMULATE=true python trader.py status

# 方式二：PowerShell
$env:SIMULATE="true"
python trader.py status
```

## 使用命令

### 扫描交易机会

```bash
# 统一波动率扫描（推荐）
python trader.py scan-all --top 10 --min-vol 5.0 --klines 10

# 扫描做空候选
python trader.py scan-short --min-change 10

# 扫描做多候选
python trader.py scan-long --min-change -10 --max-change -3
```

### 账户操作

```bash
# 查看账户状态
python trader.py status

# 查看市场数据
python trader.py market --symbol BTCUSDT
```

### 交易执行

```bash
# 开多仓
python trader.py open-long 10 --symbol BTCUSDT --leverage 10

# 开空仓
python trader.py open-short 10 --symbol BTCUSDT --leverage 10

# 平多仓
python trader.py close-long --symbol BTCUSDT

# 平空仓
python trader.py close-short --symbol BTCUSDT

# 部分平仓（50%）
python trader.py close-long --symbol BTCUSDT --percent 50
```

### LLM 分析

```bash
# 分析开仓机会
python trader.py llm-open --symbol BTCUSDT

# 分析持仓决策
python trader.py llm-hold --symbol BTCUSDT
```

## 项目结构

```
trading_program/
├── trader.py          # 核心交易程序
├── STRATEGY.md        # 交易策略文档
├── SKILL.md           # 使用技巧文档
├── blacklist.json     # 币种黑名单
├── .sim_state.json    # 模拟账户状态（自动生成）
└── memory/            # 历史交易记录
```

## 安全警告

- **切勿将 API Secret 提交到公开仓库**
- 建议创建只读 API Key 用于交易
- 启用 IP 白名单限制
- 大额账户建议开启二次验证
- 生产环境请使用独立的交易账户

## 免责声明

本项目仅供学习和研究使用。加密货币交易存在极高风险，可能导致资金损失。请根据自身风险承受能力谨慎决策，作者不对任何交易损失承担责任。

## 技术栈

- Python 3.x
- Binance Futures API
- requests
- certifi (SSL 证书)
