# ========== v10 核心原则 (2026-05-20) ==========
# 1. 严禁使用指标 (RSI/MACD/布林带/MA) — 指标是滞后的谎言
# 2. 只看价格行为: K线力度、成交量、关键位置博弈
# 3. 仓位: 单笔保证金 <= 20% 余额，波动大时 <= 10%
# 4. 止损: 入场价 ±5% (固定) 或 ±1.5倍ATR (波动调整)
# 5. 止盈: 由阶梯止损(zs.md)管理，开仓时不设置
# 6. 黑名单: 仅在扫描时过滤，开仓时不拦截
# 7. 交易日志记录到 journal.json
# 8. Cooldown: 连亏2笔强制休息1小时
# 9. 保本止损: 浮盈>=1%移止损到成本价
# ========== v10 END ==========


import os
import sys
import time
import argparse
import json
import hmac
import hashlib
from datetime import datetime
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

# Fix SSL: use certifi CA bundle BEFORE requests import
import certifi
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()

try:
    import requests
except ImportError:
    print("Error: requests module not installed. Run: pip install requests")
    sys.exit(1)

# Monkey-patch requests to always use certifi CA bundle
_old_send = requests.adapters.HTTPAdapter.send

def _patched_send(self, request, *args, **kwargs):
    kwargs.setdefault('verify', certifi.where())
    return _old_send(self, request, *args, **kwargs)

requests.adapters.HTTPAdapter.send = _patched_send

# ========== 配置 ==========
API_KEY_FILE = os.path.join(os.path.dirname(__file__), '.binance_api')
API_SECRET_FILE = os.path.join(os.path.dirname(__file__), '.binance_secret')

def _read_key(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except:
        return ''

API_KEY = _read_key(API_KEY_FILE)
API_SECRET = _read_key(API_SECRET_FILE)
if not API_KEY or not API_SECRET:
    print("Error: API keys not found in .binance_api / .binance_secret", file=sys.stderr)
    sys.exit(1)
FAPI_URL = "https://fapi.binance.com"
PAPI_URL = "https://papi.binance.com"
SIMULATE = os.environ.get('SIMULATE', 'false').lower() == 'true'

# ========== 模拟交易状态持久化 ==========

SIM_STATE_FILE = os.path.join(os.path.dirname(__file__), '.sim_state.json')
_CONDITIONAL_ORDERS_FILE = os.path.join(os.path.dirname(__file__), '.conditional_orders.json')
_LADDER_PEAKS_FILE = os.path.join(os.path.dirname(__file__), '.ladder_peak.json')
_JOURNAL_FILE = os.path.join(os.path.dirname(__file__), 'journal.json')
_LADDER_PEAK_FILE = os.path.join(os.path.dirname(__file__), '.ladder_peak.json')  # 阶梯最高浮盈持久化 (2026-06-05)

# Module-level simulation state
_sim_balance = 1000.0
# positions 结构: {symbol: [{qty, entry_price, leverage, margin, side}, ...]}
# 同一币种可能有多层持仓（多空分开存），用 list 管理
_sim_positions: Dict[str, List[Dict]] = {}

# ========== v10 交易日志 ==========
def _load_journal() -> List[Dict]:
    if os.path.exists(_JOURNAL_FILE):
        try:
            with open(_JOURNAL_FILE) as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except:
            pass
    return []

def _save_trade(trade: Dict, simulated: bool = False):
    """追加单笔交易到 journal.json
    
    Args:
        trade: 交易记录 dict
        simulated: 是否为模拟交易（默认 False，即实盘）
    """
    journal = _load_journal()
    journal.append({**trade, 'simulated': simulated, 'ts': datetime.now().isoformat()})
    try:
        with open(_JOURNAL_FILE, 'w') as f:
            json.dump(journal, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[JOURNAL] ❌ 保存失败({_JOURNAL_FILE}): {e}", file=sys.stderr)


# ========== symbol 解析(消歧 auto-append) ==========
# Bug Fix: --symbol=TAU 自动补 USDT 后缀会变 TAUUSDT(错的)，
#          实际应让用户传 --symbol=TA 解析为 TAUSDT (base=TA)。
#          解决: 用 exchangeInfo 缓存做精确消歧 ——
#          1) 若原值已以 USDT 结尾: 校验币安存在
#          2) 若原值不带 USDT: 优先 XUSDT (base=X), 其次兜底原字符串
_SYMBOL_CACHE: set = set()
_SYMBOL_CACHE_TS: float = 0.0
_SYMBOL_CACHE_TTL = 3600  # 1 小时

def _refresh_symbol_cache() -> bool:
    """从 exchangeInfo 刷新币安 USDT 永续合约交易对集合。失败返回 False。"""
    global _SYMBOL_CACHE, _SYMBOL_CACHE_TS
    try:
        r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/exchangeInfo", endpoint='exchangeInfo', timeout=15)
        data = r.json()
        if not isinstance(data, dict) or 'symbols' not in data:
            return False
        cache = set()
        for s in data['symbols']:
            if s.get('status') == 'TRADING' and s.get('quoteAsset') == 'USDT' and s.get('contractType') == 'PERPETUAL':
                cache.add(s['symbol'])
        _SYMBOL_CACHE = cache
        _SYMBOL_CACHE_TS = time.time()
        return True
    except Exception as e:
        print(f"[SYMBOL] ⚠️ exchangeInfo 刷新失败: {e}", file=sys.stderr)
        return False

def _resolve_symbol(raw: str) -> Optional[str]:
    """解析用户传入的 symbol 字符串,返回币安 USDT 永续合约标准 symbol。

    规则:
      - 'BTC' / 'btc'           → 'BTCUSDT' (常规 base 补后缀)
      - 'BTCUSDT' / 'btcusdt'   → 'BTCUSDT' (原样,只校验存在)
      - 'TAU'                   → 'TAUUSDT' (如果存在),否则 'TAUSDT' (如果存在),否则 None
      - 'TA'                    → 'TAUSDT' (唯一候选,base=TA)
      - 'TAUSDT'                → 'TAUSDT' (原样)
      - 'BTCUSDT' 不存在         → None (不静默回退)

    Returns:
        解析后的标准 symbol (大写), 解析失败返回 None。
    """
    if not raw:
        return None
    s = raw.strip().upper()
    if not s.endswith('USDT'):
        candidate = s + 'USDT'
    else:
        candidate = s
    # 缓存
    if not _SYMBOL_CACHE or (time.time() - _SYMBOL_CACHE_TS) > _SYMBOL_CACHE_TTL:
        if not _refresh_symbol_cache():
            # 缓存失败: 退而求其次,只在用户已写 USDT 时接受;否则报错
            if s.endswith('USDT'):
                return s
            return None
    # 1) 候选存在 → 用候选
    if candidate in _SYMBOL_CACHE:
        return candidate
    # 2) 候选不存在但用户传的是 base (无 USDT 后缀) → 报错让用户显式指定
    #    (过去会静默拼成错误结果,如 TAU→TAUUSDT)
    if not s.endswith('USDT'):
        print(f"❌ {raw} 解析失败: {candidate} 在币安 USDT 永续合约中不存在。请显式传 --symbol=<完整USDT名>。", file=sys.stderr)
    else:
        print(f"❌ {raw} 不在币安 USDT 永续合约列表中(可能已下架或拼写错误)。", file=sys.stderr)
    return None


def _verify_sim_state(balance: float, positions: Dict) -> bool:
    """验证 sim_state 与 journal 重构值的一致性（诊断用）
    Bug #29 Fix: 起点 balance 从 sim_state.json 读(不是 hardcode 1000)
    """
    journal = _load_journal()
    # 从 sim_state.json 读 starting_balance 字段(用于倒推起点)
    starting_balance = 1000.0
    try:
        with open(SIM_STATE_FILE) as f:
            state = json.load(f)
        starting_balance = state.get('starting_balance', 1000.0)
    except Exception:
        pass
    calc_balance = starting_balance
    calc_positions = {}  # symbol -> list of margin infos
    for t in journal:
        action = t['action']
        symbol = t['symbol']
        margin = t.get('margin', 0)
        qty = t.get('qty', 0)
        entry = t.get('entry', 0)
        pnl = t.get('pnl', 0)
        side = t.get('side', 'LONG')
        if action == 'OPEN':
            fee = qty * entry * 0.001
            calc_balance -= (margin + fee)
            if symbol not in calc_positions:
                calc_positions[symbol] = []
            calc_positions[symbol].append({'margin': margin, 'qty': qty, 'entry_price': entry, 'side': side})
        elif action == 'CLOSE':
            calc_balance += pnl
            if symbol in calc_positions and calc_positions[symbol]:
                calc_positions[symbol].pop()

    open_symbols = {s for s, stacks in calc_positions.items() if stacks}
    sim_symbols = set(positions.keys())
    balance_match = abs(calc_balance - balance) < 0.01
    positions_match = open_symbols == sim_symbols

    if not balance_match or not positions_match:
        print(f"[SIM STATE] ⚠️ 状态不一致!")
        print(f"  journal重构 balance={calc_balance:.4f} | .sim_state balance={balance:.4f}")
        print(f"  journal未平仓={open_symbols} | .sim_state持仓={sim_symbols}")
        return False
    print(f"[SIM STATE] ✅ 状态一致 (balance={calc_balance:.4f}, positions={list(sim_symbols)})")
    return True


def _load_sim_state():
    """从文件加载模拟状态

    Returns:
        tuple: (balance, positions) - 总是返回 tuple,加载失败时记录错误而非静默返回默认
    """
    if os.path.exists(SIM_STATE_FILE):
        try:
            with open(SIM_STATE_FILE, 'r') as f:
                state = json.load(f)
            global _sim_balance, _sim_positions
            _sim_balance = state.get('balance', 1000.0)
            # 兼容旧格式: positions 可能还是 {symbol: {...}} 需要转成 {symbol: [...]}
            raw_positions = state.get('positions', {})
            converted = {}
            for sym, val in raw_positions.items():
                if isinstance(val, list):
                    converted[sym] = val
                elif isinstance(val, dict):
                    # 旧格式单个持仓，转成 list
                    converted[sym] = [val]
            _sim_positions = converted
            return (_sim_balance, _sim_positions)
        except json.JSONDecodeError as e:
            print(f"[SIM STATE] ❌ JSON解析失败({SIM_STATE_FILE}): {e}", file=sys.stderr)
        except Exception as e:
            print(f"[SIM STATE] ❌ 加载失败({SIM_STATE_FILE}): {e}", file=sys.stderr)
    return (1000.0, {})

def _save_sim_state(balance, positions):
    """保存模拟状态到文件（原子写入 + 写后验证，防止并发覆盖和数据丢失）"""
    global _sim_balance, _sim_positions
    _sim_balance = balance
    _sim_positions = positions
    tmp_file = SIM_STATE_FILE + '.tmp'
    try:
        with open(tmp_file, 'w') as f:
            json.dump({'balance': balance, 'positions': positions}, f)
        # Bug 1 Fix: 写完后验证内容是否正确写入，防止部分写入导致数据丢失
        with open(tmp_file, 'r') as f:
            verified = json.load(f)
        if abs(verified['balance'] - balance) > 1e-9 or verified['positions'] != positions:
            raise Exception("验证失败: 写入内容与内存不一致")
        os.replace(tmp_file, SIM_STATE_FILE)
    except Exception as e:
        print(f"[SIM STATE] ❌ 保存失败({SIM_STATE_FILE}): {e}", file=sys.stderr)
        # 尝试回退: 写一个带有时间戳的错误标记，便于排查
        try:
            err_file = SIM_STATE_FILE + f'.save_err_{int(time.time())}'
            with open(err_file, 'w') as f:
                json.dump({'balance': balance, 'positions': positions, 'save_error': str(e)}, f)
        except:
            pass


def _load_conditional_orders() -> Dict:
    """从文件加载活跃条件单追踪"""
    if os.path.exists(_CONDITIONAL_ORDERS_FILE):
        try:
            with open(_CONDITIONAL_ORDERS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_conditional_orders(orders: Dict):
    """保存条件单追踪到文件"""
    try:
        with open(_CONDITIONAL_ORDERS_FILE, 'w') as f:
            json.dump(orders, f, indent=2)
    except:
        pass


def _load_ladder_peaks() -> Dict:
    """加载阶梯"只上不下"的 peak trigger 记录
    结构: {"SYMBOL": {"LONG": trigger_price, "SHORT": trigger_price}}
    LONG: 只记录历史最高的 trigger(锁档后下不来)
    SHORT: 只记录历史最低的 trigger
    """
    if os.path.exists(_LADDER_PEAKS_FILE):
        try:
            with open(_LADDER_PEAKS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_ladder_peaks(peaks: Dict):
    """保存阶梯 peak 记录"""
    try:
        with open(_LADDER_PEAKS_FILE, 'w') as f:
            json.dump(peaks, f, indent=2)
    except Exception:
        pass


# ========== PM 账户检测(纯 requests,无第三方客户端依赖)==========
_is_pm_account = None


def _papi_get_account(retries: int = 3) -> Dict:
    """直接用 requests 发 papi 签名请求获取账户信息(不依赖 BinanceClient),带重试"""
    last_err = None
    for attempt in range(retries):
        try:
            ts = str(int(time.time() * 1000))
            recv = '5000'
            query = f"timestamp={ts}&recvWindow={recv}"
            signature = hmac.new(
                API_SECRET.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            url = f"{PAPI_URL}/papi/v1/um/account?{query}&signature={signature}"
            headers = {"X-MBX-APIKEY": API_KEY}
            r = _rl_request('GET', url, endpoint='um/account', headers=headers, timeout=15)
            if r.status_code != 200:
                raise Exception(f"papi account Error {r.status_code}: {r.text}")
            return r.json()
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1 * (attempt + 1))  # 1s, 2s backoff
    raise last_err


_pm_check_retries = 0

def is_portfolio_margin() -> bool:
    """检测是否为 Portfolio Margin 账户(检查 tradeGroupId 字段)
    遇到异常时不缓存结果,持续重试(避免网络抖动导致永久误判)"""
    global _is_pm_account, _pm_check_retries
    if _is_pm_account is True:
        return True
    try:
        acct = _papi_get_account()
        if "tradeGroupId" in acct or "assets" in acct:
            _is_pm_account = True
            _pm_check_retries = 0
            print(f"[PM] 检测到 Portfolio Margin 账户 (tradeGroupId={acct.get('tradeGroupId')})", file=sys.stderr)
        else:
            _is_pm_account = False
    except Exception:
        _pm_check_retries += 1
        if _pm_check_retries >= 3:
            _is_pm_account = False
        else:
            pass
    return _is_pm_account is True

# ========== Binance API 封装(PAPI 版)==========
class BinanceTrader:
    def __init__(self):
        self.api_key = API_KEY
        self.api_secret = API_SECRET
        self.fapi_url = FAPI_URL
        self.papi_url = PAPI_URL
        self._papi = None  # kept for compat, unused for signing

    def _sign(self, params: Dict) -> str:
        """fapi 签名"""
        import hmac, hashlib
        parts = [f"{k}={v}" for k, v in sorted(params.items())]
        return hmac.new(self.api_secret.encode(), '&'.join(parts).encode(), hashlib.sha256).hexdigest()

    def _fapi_request(self, method: str, endpoint: str, params: Dict = None, signed: bool = False) -> Dict:
        """fapi 公共请求(市场数据)
        2026-06-04 修复: 改走 _rl_request 自动限速 + 429/418 退避
        """
        headers = {'X-MBX-APIKEY': self.api_key}
        if params is None:
            params = {}
        url = f"{self.fapi_url}{endpoint}"
        # endpoint 如 /fapi/v1/ticker/24hr → ticker/24hr
        ep = endpoint.lstrip('/').split('/', 1)[-1] if '/' in endpoint else endpoint
        r = _rl_request(method, url, endpoint=ep, headers=headers, params=params, timeout=15)
        if r.status_code != 200:
            raise Exception(f"fapi Error {r.status_code}: {r.text}")
        return r.json()

    def _papi_sign(self, params: Dict) -> str:
        """PAPI 签名"""
        params['timestamp'] = str(int(time.time() * 1000))
        params['recvWindow'] = 5000
        query = '&'.join([f'{k}={v}' for k, v in sorted(params.items())])
        return query + '&signature=' + hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()

    def _papi_request(self, method: str, endpoint: str, params: Dict = None, retries: int = 3) -> Dict:
        """PAPI 签名请求 (PM 统一账户),带重试 - 每次重试都重新签名
        2026-06-04 修复: 改走 _rl_request 自动限速 + 429/418 退避
        """
        headers = {'X-MBX-APIKEY': self.api_key}
        if params is None:
            params = {}
        last_err = None
        for attempt in range(retries):
            # ⭐ 每次重试都重新签名(时间戳必须最新)
            signed_params = self._papi_sign(params.copy())
            url = f"{self.papi_url}{endpoint}?{signed_params}"
            try:
                # endpoint 用 path 第一段(如 /papi/v1/um/order → um_order)
                ep = endpoint.split('/')[-1] if '/' in endpoint else endpoint
                r = _rl_request(method, url, endpoint=ep, headers=headers, timeout=15)
                if r.status_code != 200:
                    raise Exception(f"papi Error {r.status_code}: {r.text}")
                return r.json()
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(1 * (attempt + 1))
        raise last_err

    # ---- 市场数据(fapi 公开端点)----
    def get_price(self, symbol: str) -> float:
        """获取当前价格(优先 papi,降级 fapi,再降级 klines)"""
        # 1. 优先 papi (PM账户可用)
        try:
            r = _rl_request('GET', f"{self.papi_url}/papi/v1/um/ticker/price", endpoint='um/ticker/price', params={'symbol': symbol})
            data = r.json()
            if isinstance(data, dict) and 'price' in data:
                return float(data['price'])
        except Exception:
            pass
        # 2. fapi 降级
        try:
            r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/ticker/price", endpoint='ticker/price', params={'symbol': symbol})
            data = r.json()
            if isinstance(data, dict) and 'price' in data:
                return float(data['price'])
        except Exception:
            pass
        # 3. 最终降级:从 klines 取最新收盘价
        try:
            r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/klines", endpoint='klines', params={'symbol': symbol, 'interval': '1m', 'limit': 1})
            klines = r.json()
            if klines:
                return float(klines[-1][4])  # 收盘价
        except Exception:
            pass
        raise Exception(f"无法获取 {symbol} 价格(所有端点均失败)")

    def get_ticker(self, symbol: str) -> Dict:
        r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={'symbol': symbol})
        return r.json()

    def set_stop_loss(self, symbol: str, side: str, quantity: float, trigger_price: float) -> Dict:
        """PM账户设置止损条件单 (STOP_MARKET - 触发后市价平仓)

        side: 'SELL'=平多, 'BUY'=平空
        trigger_price: 触发价格(跌破此价触发)
        """
        if SIMULATE:
            # Bug Fix: sim 路径不调 API，仅写文件追踪 + 返响应
            fake_algo_id = int(time.time() * 1_000_000) % 10**12
            orders = _load_conditional_orders()
            if symbol not in orders:
                orders[symbol] = {}
            orders[symbol][side] = fake_algo_id
            _save_conditional_orders(orders)
            print(f"[SIMULATE] 止损已设置 {symbol} {side} @ {trigger_price} algo_id={fake_algo_id}", file=sys.stderr)
            return {'algoId': fake_algo_id, 'status': 'NEW', 'triggerPrice': str(trigger_price)}
        result = self._papi_request('POST', '/papi/v1/um/algo/order', {
            'symbol': symbol,
            'side': side,
            'algoType': 'CONDITIONAL',
            'type': 'STOP_MARKET',
            'quantity': str(quantity),
            'triggerPrice': str(trigger_price),
            'reduceOnly': 'true',
        })
        # 保存algo_id到文件追踪
        new_algo_id = result.get('algoId')
        if new_algo_id:
            orders = _load_conditional_orders()
            if symbol not in orders:
                orders[symbol] = {}
            orders[symbol][side] = new_algo_id
            _save_conditional_orders(orders)
        return result

    def set_stop_loss_limit(self, symbol: str, side: str, quantity: float,
                            trigger_price: float, order_price: float) -> Dict:
        """PM账户设置止损条件单 (STOP - 触发后下限价单平仓)

        side: 'SELL'=平多, 'BUY'=平空
        trigger_price: 触发价格(跌破此价触发)
        order_price: 下单价格(通常低于触发价)
        """
        return self._papi_request('POST', '/papi/v1/um/algo/order', {
            'symbol': symbol,
            'side': side,
            'algoType': 'CONDITIONAL',
            'type': 'STOP',
            'quantity': str(quantity),
            'stopPrice': str(trigger_price),
            'price': str(order_price),
            'reduceOnly': 'true',
        })

    def set_take_profit(self, symbol: str, side: str, quantity: float, trigger_price: float) -> Dict:
        """PM账户设置止盈条件单 (TAKE_PROFIT_MARKET - 触发后市价平仓)

        side: 'SELL'=平多, 'BUY'=平空
        trigger_price: 触发价格(涨到此价触发)
        """
        result = self._papi_request('POST', '/papi/v1/um/algo/order', {
            'symbol': symbol,
            'side': side,
            'algoType': 'CONDITIONAL',
            'type': 'TAKE_PROFIT_MARKET',
            'quantity': str(quantity),
            'triggerPrice': str(trigger_price),
            'reduceOnly': 'true',
        })
        # 保存algo_id到文件追踪
        new_algo_id = result.get('algoId')
        if new_algo_id:
            orders = _load_conditional_orders()
            if symbol not in orders:
                orders[symbol] = {}
            orders[symbol][side] = new_algo_id
            _save_conditional_orders(orders)
        return result

    def set_take_profit_limit(self, symbol: str, side: str, quantity: float,
                              trigger_price: float, order_price: float) -> Dict:
        """PM账户设置止盈条件单 (TAKE_PROFIT - 触发后上限价单平仓)

        side: 'SELL'=平多, 'BUY'=平空
        trigger_price: 触发价格(涨到此价触发)
        order_price: 下单价格(通常等于或略低于触发价)
        """
        return self._papi_request('POST', '/papi/v1/um/algo/order', {
            'symbol': symbol,
            'side': side,
            'algoType': 'CONDITIONAL',
            'type': 'TAKE_PROFIT',
            'quantity': str(quantity),
            'stopPrice': str(trigger_price),
            'price': str(order_price),
            'reduceOnly': 'true',
        })

    def get_conditional_orders(self, symbol: str = None) -> List[Dict]:
        """查询PM账户活跃条件单(PM账户不支持此接口,始终返回空列表)
        
        PM账户 /papi/v1/um/conditional/openOrders 返回 404,无法通过API查询活跃条件单。
        使用 .conditional_orders.json 文件追踪活跃条件单(下单时自动保存,cancel时删除)。
        如需查询真实活跃状态,请通过 exchangeInfo 或经纪商后台确认。
        """
        return []

    def _place_conditional_order(self, symbol: str, side: str, quantity: int,
                                  order_type: str, trigger_price: float,
                                  algo_id: int = None) -> Dict:
        """下条件单并追踪 algo_id（跳过 ATR 计算,直接用传入的 trigger_price）
        Args:
            algo_id: 旧条件单 ID（传入则先取消）
        Returns: papi 返回的 dict
        """
        # 1. 查找并取消旧单
        if algo_id is None:
            orders = _load_conditional_orders()
            algo_id = orders.get(symbol, {}).get(side)
        if algo_id is not None:
            try:
                self.cancel_conditional_order(symbol, algo_id)
                print(f"[replace] ✅ 已取消旧条件单 algo_id={algo_id}")
            except Exception as e:
                print(f"[replace] ⚠️ 取消旧单失败(可能已触发): {e}")

        # 2. 下新单
        result = self._papi_request('POST', '/papi/v1/um/algo/order', {
            'symbol': symbol,
            'side': side,
            'algoType': 'CONDITIONAL',
            'type': order_type,
            'quantity': str(quantity),
            'triggerPrice': str(trigger_price),
            'reduceOnly': 'true',
        })

        # 3. 保存新 algo_id
        new_algo_id = result.get('algoId')
        if new_algo_id:
            orders = _load_conditional_orders()
            if symbol not in orders:
                orders[symbol] = {}
            orders[symbol][side] = new_algo_id
            _save_conditional_orders(orders)
            print(f"[replace] ✅ 新条件单已设置 algo_id={new_algo_id} trigger={trigger_price}")
        return result

    def replace_conditional_order(self, symbol: str, side: str, quantity: int,
                                   order_type: str = 'STOP_MARKET',
                                   algo_id: int = None,
                                   entry_price: float = None) -> Dict:
        """下新的条件单,自动追踪algo_id

        流程:1)从文件查找旧algo_id 2)尝试取消旧单 3)计算触发价(基于 ATR) 4)下新单 5)保存新algo_id

        触发价计算（对齐 lg.md 7.1 ATR 公式）：
          - 调 _get_atr_and_volatility 拿 ATR(14) + atrpct
          - 调 _calculate_stop_loss(entry_price, atr, atrpct, side) 拿 sl_pct
          - sl_pct = max(atrpct × 1.5, 5%)（最少 5% 地板）
          - 多仓止损: trigger = entry × (1 - sl_pct/100)
          - 空仓止损: trigger = entry × (1 + sl_pct/100)
          - 进场价为 None 时，退到现价×(1 ± sl_pct/100) 作为兜底

        Args:
            symbol: 币种,如 NILUSDT
            side: 'SELL'=平多仓/'BUY'=平空仓
            quantity: 数量(整数)
            order_type: 'STOP_MARKET'(止损) / 'TAKE_PROFIT_MARKET'(止盈)
            algo_id: 可选,直接指定旧条件单ID
            entry_price: 入场价(用于计算止损空间),为 None 时用现价退到
        """
        # 1. 查找旧algo_id(从文件追踪)
        orders = _load_conditional_orders()
        old_algo_id = algo_id
        if not old_algo_id:
            old_algo_id = orders.get(symbol, {}).get(side)

        # 2. 尝试取消旧单
        if old_algo_id is not None:
            try:
                self.cancel_conditional_order(symbol, old_algo_id)
                print(f"[replace] ✅ 已取消旧条件单 algo_id={old_algo_id}")
            except Exception as e:
                print(f"[replace] ⚠️ 取消旧单失败(可能已触发): {e}")

        # 3. 计算触发价(2026-06-05 v3 简化): 用现价 × 0.98 (多仓止损) / × 1.02 (空仓止损)
        # 之前阶梯档位 + peak 持久化 是过度设计 — 当前规则就是「锁 2%」
        # 利润只往上锁不能往下: 取最新价 × 0.98 已经够, 浮盈后调就是"移到现价 × 0.98"
        current_price = self.get_price(symbol)
        # 默认入场价退到 = current_price
        ref_entry = entry_price if entry_price and entry_price > 0 else current_price
        # v3 简化: 直接 current × 0.98 (SELL=多仓止损) / × 1.02 (BUY=空仓止损)
        # 阶梯档位 (lg.md 7.2) 复杂代码已删除, 改由 LLM 手动调 replace-order --trigger=N 覆盖
        if side.upper() == 'SELL':  # 平多=多仓止损
            step_lock_trigger = current_price * 0.98
        else:  # 平空=空仓止损
            step_lock_trigger = current_price * 1.02
        # 打印
        if side.upper() == 'SELL':
            upnl_pct = (current_price - ref_entry) / ref_entry * 100
        else:
            upnl_pct = (ref_entry - current_price) / ref_entry * 100
        print(f"[replace] 浮盈={upnl_pct:+.2f}% → 简化版锁 2%: current={_fmt_price(current_price, symbol)} → trigger={_fmt_price(step_lock_trigger, symbol)}")

        sl_pct = 5.0  # 默认 5% 地板
        atr_info = None
        try:
            atr_info = _get_atr_and_volatility(symbol)
            sl_data = _calculate_stop_loss(
                entry_price=ref_entry,
                atr=atr_info['atr'],
                atr_percent=atr_info['atr_percent'],
                side='LONG' if side.upper() == 'SELL' else 'SHORT',  # 平多=多仓止损,平空=空仓止损
            )
            sl_pct = sl_data['sl_percent']
        except Exception as e:
            print(f"[replace] ⚠️ ATR 计算失败,用 5% 地板: {e}", file=sys.stderr)

        if atr_info:
            print(f"[replace] ATR={atr_info['atr']:.6f} ({atr_info['atr_percent']:.2f}%) 波动={atr_info['volatility']} → sl_pct={sl_pct:.2f}%")

        # 根据方向计算触发价
        if side.upper() == 'SELL':  # 平多仓(价格跌到 trigger 时止损)
            trigger_price = _round_to_tick(ref_entry * (1 - sl_pct / 100), symbol)
        else:  # 平空仓(价格涨到 trigger 时止损)
            trigger_price = _round_to_tick(ref_entry * (1 + sl_pct / 100), symbol)
        # 阶梯止损 vs ATR 止损取较紧者(保护更多浮盈)
        if step_lock_trigger is not None:
            if side.upper() == 'SELL':  # 多仓:阶梯触发价更高 = 更紧
                trigger_price = max(trigger_price, _round_to_tick(step_lock_trigger, symbol))
            else:  # 空仓:阶梯触发价更低 = 更紧
                trigger_price = min(trigger_price, _round_to_tick(step_lock_trigger, symbol))
            print(f"[replace] 🪜 阶梯 vs ATR 取较紧: final trigger={trigger_price}")
        print(f"[replace] 入场价={_fmt_price(ref_entry, symbol)} 触发价={_fmt_price(trigger_price, symbol)} (sl_pct={sl_pct:.2f}%)")

        # 4. 下新单（复用 helper）
        return self._place_conditional_order(symbol, side, quantity, order_type, trigger_price, old_algo_id)


    def _clear_conditional_orders(self, symbol: str):
        """平仓后清除符号的所有条件单追踪"""
        orders = _load_conditional_orders()
        if symbol in orders:
            del orders[symbol]
            _save_conditional_orders(orders)
            print(f"[条件单] 已清除 {symbol} 追踪记录")

    def cancel_conditional_order(self, symbol: str, algo_id: int) -> Dict:
        """取消PM账户条件单,并清除文件追踪记录
        P0 Fix 2026-06-09: del sides[side] 之后如果 sides 变空要清 symbol, 避免 .json 出现空 dict
        """
        result = self._papi_request('DELETE', '/papi/v1/um/algo/order', {
            'symbol': symbol,
            'algoId': str(algo_id),
        })
        # 清除追踪文件中的记录
        orders = _load_conditional_orders()
        for sym, sides in orders.items():
            for side, old_id in list(sides.items()):
                if str(old_id) == str(algo_id):
                    del sides[side]
                    print(f"[cancel] ✅ 已清除追踪记录 symbol={symbol} side={side} algo_id={algo_id}")
        # 清理空 dict symbol 键 (P0 Fix)
        orders = {k: v for k, v in orders.items() if v}
        _save_conditional_orders(orders)
        return result

    def get_klines(self, symbol: str, interval: str = "30m", limit: int = 100) -> List:
        """获取K线数据(fapi优先,papi作备选)"""
        # fapi优先(国内pap访问很慢)
        try:
            r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/klines", endpoint='klines', params={'symbol': symbol, 'interval': interval, 'limit': limit})
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return data
        except Exception:
            pass
        # 降级 papi
        try:
            r = _rl_request('GET', f"{self.papi_url}/papi/v1/um/klines", endpoint='um/klines', params={'symbol': symbol, 'interval': interval, 'limit': limit})
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return data
        except Exception:
            pass
        return []

    def get_order_book(self, symbol: str, limit: int = 20) -> Dict:
        try:
            r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/depth", endpoint='depth', params={'symbol': symbol, 'limit': limit})
            return r.json()
        except Exception:
            pass
        try:
            r = _rl_request('GET', f"{self.papi_url}/papi/v1/um/depth", endpoint='um/depth', params={'symbol': symbol, 'limit': limit})
            return r.json()
        except Exception:
            return {}

    def get_mark_price(self, symbol: str) -> float:
        try:
            r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/premiumIndex", endpoint='premiumIndex', params={'symbol': symbol})
            return float(r.json()['markPrice'])
        except Exception:
            pass
        try:
            r = _rl_request('GET', f"{self.papi_url}/papi/v1/um/premiumIndex", endpoint='um/premiumIndex', params={'symbol': symbol})
            return float(r.json()['markPrice'])
        except Exception:
            raise Exception(f"get_mark_price failed for {symbol}")

    def get_funding_rate(self, symbol: str) -> Dict:
        try:
            r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/premiumIndex", endpoint='premiumIndex', params={'symbol': symbol})
            data = r.json()
            return {
                'fundingRate': float(data.get('lastFundingRate', 0)) * 100,
                'nextFundingTime': data.get('nextFundingTime', '')
            }
        except Exception:
            pass
        try:
            r = _rl_request('GET', f"{self.papi_url}/papi/v1/um/premiumIndex", endpoint='um/premiumIndex', params={'symbol': symbol})
            data = r.json()
            return {
                'fundingRate': float(data.get('lastFundingRate', 0)) * 100,
                'nextFundingTime': data.get('nextFundingTime', '')
            }
        except Exception:
            return {'fundingRate': 0, 'nextFundingTime': ''}

    def get_long_short_ratio(self, symbol: str) -> Dict:
        try:
            r = _rl_request('GET', f"{self.fapi_url}/futures/data/globalLongShortRatio", endpoint='globalLongShortRatio', params={'symbol': symbol, 'periodType': '1h', 'limit': 10})
            data = r.json()
            if data:
                latest = data[-1]
                return {
                    'longRatio': float(latest.get('longAccount', 0)) * 100,
                    'shortRatio': float(latest.get('shortAccount', 0)) * 100
                }
        except Exception:
            pass
        try:
            r = _rl_request('GET', f"{self.papi_url}/papi/v1/um/globalLongShortAccountRatio", endpoint='um/globalLongShortAccountRatio', params={'symbol': symbol, 'periodType': '1h', 'limit': 10})
            data = r.json()
            if isinstance(data, list) and data:
                latest = data[-1]
                return {
                    'longRatio': float(latest.get('longAccount', 0)) * 100,
                    'shortRatio': float(latest.get('shortAccount', 0)) * 100
                }
        except Exception:
            pass
        return {'longRatio': 50, 'shortRatio': 50}

    def get_ticker(self, symbol: str) -> Dict:
        try:
            r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={'symbol': symbol})
            return r.json()
        except Exception:
            pass
        try:
            r = _rl_request('GET', f"{self.papi_url}/papi/v1/um/ticker/24hr", endpoint='um/ticker/24hr', params={'symbol': symbol})
            return r.json()
        except Exception:
            return {}

    # ---- 账户操作(PAPI = 统一账户)----
    def get_account(self) -> Dict:
        """获取账户信息(自动选择 PAPI 或 fapi)"""
        if SIMULATE:
            return {
                'totalAvailableBalance': str(_sim_balance),
                'balances': [{'asset': 'USDT', 'free': str(_sim_balance)}],
                'positions': [{'symbol': s, 'positionAmt': str(-stacks[-1]['qty']), 'entryPrice': str(stacks[-1]['entry_price'])}
                              for s, stacks in _sim_positions.items() if stacks]
            }
        if is_portfolio_margin():
            return _papi_get_account()
        params = {'timestamp': int(time.time()*1000)}
        params['signature'] = self._sign(params)
        r = _rl_request('GET', f"{self.fapi_url}/fapi/v2/account", endpoint='account', headers={'X-MBX-APIKEY': self.api_key}, params=params)
        if r.status_code != 200:
            raise Exception(f"fapi account Error {r.status_code}: {r.text}")
        return r.json()

    def get_positions(self, symbol: str = None) -> List[Dict]:
        """获取持仓(PAPI um_position_risk)
        sim 路径：Bugg Fix: 同一 symbol 多个 LONG 堆叠层或 SHORT 堆叠层需合并为一条
        （加权平均 entry_price + 求和 qty/margin + 净 upnl），让 LLM 看到真实总持仓
        """
        if SIMULATE:
            _load_sim_state()
            result = []
            for s, stacks in _sim_positions.items():
                if symbol and s != symbol:
                    continue
                if not stacks:
                    continue
                # 拉一次市价
                try:
                    kl = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': s, 'interval': '1m', 'limit': 1}, timeout=15, verify=certifi.where()).json()
                    current = float(kl[0][4])
                except Exception:
                    current = stacks[-1].get('entry_price', 0)
                # 按 side 分组，合并各方向的 stack
                for side in ('LONG', 'SHORT'):
                    side_stacks = [p for p in stacks if p.get('side') == side]
                    if not side_stacks:
                        continue
                    total_qty = sum(p.get('qty', 0) for p in side_stacks)
                    total_margin = sum(p.get('margin', 0) for p in side_stacks)
                    if total_qty <= 0 or total_margin <= 0:
                        continue
                    # 加权平均 entry_price = Σ(qty × entry) / Σ(qty)
                    weighted_entry = sum(p.get('qty', 0) * p.get('entry_price', 0) for p in side_stacks) / total_qty
                    # 各层 leverage 加权平均
                    avg_lev = sum(p.get('qty', 0) * p.get('leverage', 1) for p in side_stacks) / total_qty
                    if side == 'SHORT':
                        upnl = (weighted_entry - current) / weighted_entry * avg_lev * total_margin
                    else:
                        upnl = (current - weighted_entry) / weighted_entry * avg_lev * total_margin
                    result.append({
                        'symbol': s,
                        'amount': total_qty,
                        'entryPrice': round(weighted_entry, 8),
                        'unrealizedProfit': round(upnl, 4),
                        'leverage': int(round(avg_lev)),
                        'positionSide': side,
                        'margin': round(total_margin, 4),
                        'layers': len(side_stacks),  # 同方向多少层
                    })
            return result
        if is_portfolio_margin():
            try:
                # 直接复用 account 数据中的 positions(避免多一次 HTTP 请求)
                acct = _papi_get_account()
                positions = acct.get('positions', []) if not symbol else [p for p in acct.get('positions', []) if p.get('symbol') == symbol]
                result = []
                for pos in positions:
                    amt = float(pos.get('positionAmt', 0))
                    if abs(amt) < 1e-9:  # 忽略 0 持仓
                        continue
                    if symbol and pos.get('symbol') != symbol:
                        continue
                    result.append({
                        'symbol': pos['symbol'],
                        'amount': abs(amt),  # 保留小数精度,平仓时再取整
                        'entryPrice': float(pos.get('entryPrice', 0)),
                        'unrealizedProfit': float(pos.get('unrealizedProfit', 0)),
                        'leverage': int(pos.get('leverage', 1)),
                        'positionSide': 'SHORT' if amt < 0 else 'LONG'
                    })
                return result
            except Exception as e:
                print(f"[PM] 获取持仓失败: {e}", file=sys.stderr)
                return []
        # fapi
        account = self.get_account()
        positions = []
        for pos in account.get('positions', []):
            if float(pos.get('positionAmt', 0)) != 0:
                if symbol is None or pos.get('symbol') == symbol:
                    positions.append({
                        'symbol': pos['symbol'],
                        'amount': float(pos['positionAmt']),
                        'entryPrice': float(pos['entryPrice']),
                        'unrealizedProfit': float(pos.get('unrealizedProfit', 0)),
                        'leverage': int(pos.get('leverage', 1)),
                        'positionSide': pos.get('positionSide', 'BOTH')
                    })
        return positions

    def get_usdt_balance(self) -> float:
        """获取USDT余额(PM 统一账户)

        PM 账户:使用 /papi/v1/balance → totalWalletBalance(统一账户总余额)
        非 PM 账户:使用标准现货 account → free balance

        Bug #14 Fix: PM 路径 3 次重试 + 退避,不再静默 return 0.0
        （旧逻辑遇到 SSL/限速时静默 return 0.0, 导致 total_assets 间歇性归零）
        """
        if SIMULATE:
            # Bug 8 Fix: 每次调用都重新从磁盘加载，确保获取最新余额
            # （不同进程/命令调用时，内存状态可能已过期）
            _load_sim_state()
            return _sim_balance  # 直接返回内存值，不触发磁盘加载
        if is_portfolio_margin():
            import urllib3
            import json as _json
            last_err = None
            for attempt in range(3):
                try:
                    ts = str(int(time.time() * 1000))
                    q = f"timestamp={ts}"
                    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
                    url = f"https://papi.binance.com/papi/v1/balance?{q}&signature={sig}"
                    pool = urllib3.PoolManager(cert_reqs='CERT_REQUIRED', ca_certs=certifi.where())
                    headers = {"X-MBX-APIKEY": API_KEY}
                    r = pool.urlopen('GET', url, headers=headers, timeout=15.0)
                    if r.status != 200:
                        raise Exception(f"papi balance status {r.status}")
                    data = _json.loads(r.data)
                    for item in data:
                        if item.get('asset') == 'USDT':
                            return float(item.get('totalWalletBalance', 0))
                    # 响应里没 USDT - 正常来说不该,但 fallthrough 不返 0
                    raise Exception("USDT not found in balance response")
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        time.sleep(1 * (attempt + 1))
            # 3 次都失败, raise 让 LLM 看到 真实错误(而不是 total_assets=0)
            raise Exception(f"get_usdt_balance PM 3次重试失败: {last_err}")
        # 非 PM 账户
        account = self.get_account()
        if 'balances' in account:
            for bal in account.get('balances', []):
                if bal.get('asset') == 'USDT':
                    return float(bal.get('free', 0))
        if 'totalAvailableBalance' in account:
            return float(account.get('totalAvailableBalance', 0))
        return 0.0

    def close_position(self, symbol: str, quantity: float = None) -> Dict:
        """平仓(通用:多头用SELL,空头用BUY)"""
        global _sim_balance, _sim_positions
        if SIMULATE and not _sim_positions:
            _load_sim_state()
        if SIMULATE:
            if symbol not in _sim_positions or not _sim_positions[symbol]:
                raise Exception(f"[SIMULATE] 无持仓: {symbol}")
            # 取出最后一层持仓（后进先出）
            pos = _sim_positions[symbol][-1]
            price = pos['entry_price']  # 用入场价
            qty = pos['qty']
            side = pos.get('side', 'LONG')
            try:
                klines = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=15).json()
                price = float(klines[0][4])
            except Exception:
                pass
            if side == 'SHORT':
                pnl = (pos['entry_price'] - price) / pos['entry_price'] * pos.get('leverage', 10) * pos.get('margin', 0)
            else:
                pnl = (price - pos['entry_price']) / pos['entry_price'] * pos.get('leverage', 10) * pos.get('margin', 0)
            close_fee = pos.get('margin', 0) * 0.001
            net_pnl = pnl - close_fee
            _sim_balance += net_pnl + pos.get('margin', 0)  # 平仓返还保证金 + 盈亏
            # pop 最后一层，不直接删除整个 symbol
            _sim_positions[symbol].pop()
            if not _sim_positions[symbol]:
                del _sim_positions[symbol]  # 清理空列表
                # Bug Fix: 全平后清理幽灵条件单追踪
                try:
                    self._clear_conditional_orders(symbol)
                except Exception:
                    pass
            _save_sim_state(_sim_balance, _sim_positions)
            _save_trade({'symbol': symbol, 'side': side, 'action': 'CLOSE', 'entry': pos['entry_price'], 'exit': price, 'qty': qty, 'pnl': net_pnl, 'leverage': pos.get('leverage', 10), 'margin': pos['margin']}, simulated=True)
            print(f"[SIMULATE] 平仓 {symbol} x{qty:.4f} @ {price}, 盈亏={net_pnl:.4f} USDT, 余额={_sim_balance:.4f}", file=sys.stderr)
            return {'orderId': 'sim_' + str(time.time()), 'symbol': symbol, 'side': 'SELL' if side == 'LONG' else 'BUY', 'origQty': str(qty), 'pnl': net_pnl, 'margin': pos['margin']}
        if is_portfolio_margin():
            positions = self.get_positions(symbol)
            if not positions:
                raise Exception(f"无持仓: {symbol}")
            pos = positions[0]
            if quantity is None:
                quantity = abs(pos['amount'])
            # 市价全平，直接用 quantity（不取整），避免名义值 < 5 USDT 被拒
            return self._papi_request('POST', '/papi/v1/um/order', {
                'symbol': symbol,
                'side': 'SELL' if pos['positionSide'] == 'LONG' else 'BUY',
                'positionSide': 'BOTH',
                'type': 'MARKET',
                'quantity': quantity
            })
        import math
        positions = self.get_positions(symbol)
        if not positions:
            raise Exception(f"无持仓: {symbol}")
        pos = positions[0]
        if quantity is None:
            quantity = abs(pos['amount'])
        qty_int = max(1, math.ceil(quantity))
        params = {
            'symbol': symbol,
            'side': 'BUY' if pos['positionSide'] == 'SHORT' else 'SELL',
            'positionSide': pos['positionSide'],
            'type': 'MARKET',
            'reduceOnly': 'true',
            'quantity': quantity,
            'timestamp': int(time.time()*1000),
        }
        params['signature'] = self._sign(params)
        r = _rl_request('POST', f"{self.fapi_url}/fapi/v1/order",
                          endpoint='um_order',
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=15)
        if r.status_code != 200:
            raise Exception(f"close_position Error {r.status_code}: {r.text}")
        return r.json()

    # ---- 交易操作(PAPI um_order)----
    def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """设置杠杆(PAPI)"""
        if SIMULATE:
            return {'leverage': leverage, 'symbol': symbol}
        if is_portfolio_margin():
            return self._papi_request('POST', '/papi/v1/um/leverage', {'symbol': symbol, 'leverage': leverage})
        params = {'symbol': symbol, 'leverage': leverage, 'timestamp': int(time.time()*1000)}
        params['signature'] = self._sign(params)
        r = _rl_request('POST', f"{self.fapi_url}/fapi/v1/leverage",
                          endpoint='um_order',
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=15)
        if r.status_code != 200:
            raise Exception(f"set_leverage Error {r.status_code}: {r.text}")
        return r.json()

# ========== BinanceTrader 交易方法(做空/做多)==========
    def open_short(self, symbol: str, quantity: float, leverage: int = 10,
                   margin: float = None) -> Dict:
        """开空仓(PAPI um_order,单向模式:side=SELL 无需positionSide)
        Args:
            quantity: 持仓数量（枚）
            leverage: 杠杆倍数
            margin: 保证金 USDT(仅 sim 路径使用,live 路径从 qty 推出)
        """
        if SIMULATE:
            global _sim_balance, _sim_positions
            # Bug Fix: 价格获取 fapi → papi 降级(SSL 拦截时避免开仓失败)
            price = None
            for attempt in range(3):
                # 1. 试 fapi
                try:
                    klines = _rl_request('GET', f"{self.fapi_url}/fapi/v1/klines",
                                          endpoint='klines',
                                          params={'symbol': symbol, 'interval': '1m', 'limit': 1},
                                          timeout=15, verify=certifi.where()).json()
                    if klines and isinstance(klines, list) and len(klines) > 0:
                        price = float(klines[0][4])
                        break
                except Exception:
                    pass
                # 2. 降级 papi
                try:
                    klines = _rl_request('GET', f"{self.papi_url}/papi/v1/um/klines",
                                          endpoint='um/klines',
                                          params={'symbol': symbol, 'interval': '1m', 'limit': 1},
                                          timeout=15, verify=certifi.where()).json()
                    if klines and isinstance(klines, list) and len(klines) > 0:
                        price = float(klines[0][4])
                        break
                except Exception:
                    pass
                time.sleep(0.2 * (attempt + 1))
            if price is None or price <= 0:
                print(f"❌ [SIMULATE] 无法获取 {symbol} 价格，开仓失败", file=sys.stderr)
                return {'status': 'REJECTED', 'reason': 'no_price', 'symbol': symbol}

            # Bug Fix: sim 路径必须用 caller 传的 margin，**不能再用 80% 余额重算**
            if margin is not None and margin > 0:
                # caller 明确指定保证金 → qty = margin*leverage/price
                if margin > _sim_balance:
                    print(f"❌ [SIMULATE] 保证金 {margin:.2f} 超过余额 {_sim_balance:.2f}，拒开", file=sys.stderr)
                    return {'status': 'REJECTED', 'reason': 'insufficient_balance', 'symbol': symbol, 'margin': margin, 'balance': _sim_balance}
                qty = (margin * leverage) / price
            else:
                # 兜底: 走 80% 余额路径（仅在没有 caller margin 时）
                available_margin = _sim_balance * 0.8
                qty = max(quantity, 1)
                margin = (qty * price) / leverage
                if margin > available_margin:
                    qty = available_margin * leverage / price
                    if qty < 0.0001:
                        print(f"❌ [SIMULATE] 余额不足: 可用 {available_margin:.2f} USDT，拒开", file=sys.stderr)
                        return {'status': 'REJECTED', 'reason': 'insufficient_balance', 'symbol': symbol, 'available': available_margin}
                    margin = (qty * price) / leverage
            position_value = qty * price
            fee = position_value * 0.001  # 0.1% 开仓手续费
            # positions 现在是 list，同一 symbol 可以多层持仓
            if symbol not in _sim_positions:
                _sim_positions[symbol] = []
            _sim_positions[symbol].append({'qty': qty, 'entry_price': price, 'leverage': leverage, 'margin': margin, 'side': 'SHORT'})
            _sim_balance -= (margin + fee)  # 扣除保证金和手续费
            _save_sim_state(_sim_balance, _sim_positions)
            # v10: 记录交易日志
            _save_trade({'symbol': symbol, 'side': 'SHORT', 'action': 'OPEN', 'entry': price, 'qty': qty, 'margin': margin, 'leverage': leverage}, simulated=True)
            print(f"[SIMULATE] 开空 {symbol} x{qty:.4f} @ {price}, 保证金={margin:.2f} USDT, 手续费={fee:.4f} USDT, 余额={_sim_balance:.4f}", file=sys.stderr)
            return {'orderId': 'sim_' + str(time.time()), 'symbol': symbol, 'side': 'SELL', 'origQty': str(qty), 'margin': margin}
        try:
            self.set_leverage(symbol, leverage)
            time.sleep(0.2)
        except Exception as e:
            print(f"[WARN] 设置杠杆失败(继续开仓): {e}", file=sys.stderr)

        if is_portfolio_margin():
            # 单向模式:side=SELL 开空,side=BUY 平空
            # PM 要求整数,用 math.floor 保留精度
            import math
            qty_int = round(quantity)
            if qty_int == 0:
                qty_int = 1
            result = self._papi_request('POST', '/papi/v1/um/order', {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'MARKET',
                'quantity': qty_int
            })
            return result
        # fapi
        params = {
            'symbol': symbol,
            'side': 'SELL',
            'positionSide': 'SHORT',
            'type': 'MARKET',
            'quantity': quantity,
            'timestamp': int(time.time()*1000)
        }
        params['signature'] = self._sign(params)
        r = _rl_request('POST', f"{self.fapi_url}/fapi/v1/order",
                          endpoint='um_order',
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=15)
        if r.status_code != 200:
            raise Exception(f"open_short Error {r.status_code}: {r.text}")
        result = r.json()
        return result

    def open_long(self, symbol: str, quantity: float, leverage: int = 10,
                  margin: float = None) -> Dict:
        """开多仓(PAPI um_order,单向模式:side=BUY 开多)
        Args:
            quantity: 持仓数量（枚）
            leverage: 杠杆倍数
            margin: 保证金 USDT(仅 sim 路径使用)
        """
        if SIMULATE:
            global _sim_balance, _sim_positions
            _load_sim_state()  # 确保读取最新状态
            # Bug Fix: 价格获取 fapi → papi 降级(SSL 拦截时避免开仓失败)
            price = None
            for attempt in range(3):
                # 1. 试 fapi
                try:
                    klines = _rl_request('GET', f"{self.fapi_url}/fapi/v1/klines",
                                          endpoint='klines',
                                          params={'symbol': symbol, 'interval': '1m', 'limit': 1},
                                          timeout=15, verify=certifi.where()).json()
                    if klines and isinstance(klines, list) and len(klines) > 0:
                        price = float(klines[0][4])
                        break
                except Exception:
                    pass
                # 2. 降级 papi
                try:
                    klines = _rl_request('GET', f"{self.papi_url}/papi/v1/um/klines",
                                          endpoint='um/klines',
                                          params={'symbol': symbol, 'interval': '1m', 'limit': 1},
                                          timeout=15, verify=certifi.where()).json()
                    if klines and isinstance(klines, list) and len(klines) > 0:
                        price = float(klines[0][4])
                        break
                except Exception:
                    pass
                time.sleep(0.2 * (attempt + 1))
            if price is None or price <= 0:
                print(f"❌ [SIMULATE] 无法获取 {symbol} 价格，开仓失败", file=sys.stderr)
                return {'status': 'REJECTED', 'reason': 'no_price', 'symbol': symbol}

            # Bug Fix: sim 路径必须用 caller 传的 margin，**不能再用 80% 余额重算**
            if margin is not None and margin > 0:
                if margin > _sim_balance:
                    print(f"❌ [SIMULATE] 保证金 {margin:.2f} 超过余额 {_sim_balance:.2f}，拒开", file=sys.stderr)
                    return {'status': 'REJECTED', 'reason': 'insufficient_balance', 'symbol': symbol, 'margin': margin, 'balance': _sim_balance}
                qty = (margin * leverage) / price
            else:
                # 兜底: 走 80% 余额路径
                available_margin = _sim_balance * 0.8
                qty = max(quantity, 1)
                margin = (qty * price) / leverage
                if margin > available_margin:
                    qty = available_margin * leverage / price
                    if qty < 0.0001:
                        print(f"❌ [SIMULATE] 余额不足: 可用 {available_margin:.2f} USDT，拒开", file=sys.stderr)
                        return {'status': 'REJECTED', 'reason': 'insufficient_balance', 'symbol': symbol, 'available': available_margin}
                    margin = (qty * price) / leverage
            position_value = qty * price
            fee = position_value * 0.001
            # positions 现在是 list，同一 symbol 可以多层持仓
            if symbol not in _sim_positions:
                _sim_positions[symbol] = []
            _sim_positions[symbol].append({'qty': qty, 'entry_price': price, 'leverage': leverage, 'margin': margin, 'side': 'LONG'})
            _sim_balance -= (margin + fee)
            _save_sim_state(_sim_balance, _sim_positions)
            # v10: 记录交易日志
            _save_trade({'symbol': symbol, 'side': 'LONG', 'action': 'OPEN', 'entry': price, 'qty': qty, 'margin': margin, 'leverage': leverage}, simulated=True)
            print(f"[SIMULATE] 开多 {symbol} x{qty:.4f} @ {price}, 保证金={margin:.2f} USDT, 手续费={fee:.4f} USDT, 余额={_sim_balance:.4f}", file=sys.stderr)
            return {'orderId': 'sim_' + str(time.time()), 'symbol': symbol, 'side': 'BUY', 'origQty': str(qty), 'margin': margin}
        try:
            self.set_leverage(symbol, leverage)
            time.sleep(0.2)
        except Exception as e:
            print(f"[WARN] 设置杠杆失败(继续开仓): {e}", file=sys.stderr)

        if is_portfolio_margin():
            # 单向模式:side=BUY 开多
            # PM 要求整数,用 math.floor 保留精度
            import math
            qty_int = round(quantity)
            if qty_int == 0:
                qty_int = 1
            result = self._papi_request('POST', '/papi/v1/um/order', {
                'symbol': symbol,
                'side': 'BUY',
                'type': 'MARKET',
                'quantity': qty_int
            })
            return result
        params = {
            'symbol': symbol,
            'side': 'BUY',
            'positionSide': 'LONG',
            'type': 'MARKET',
            'quantity': quantity,
            'timestamp': int(time.time()*1000)
        }
        params['signature'] = self._sign(params)
        r = _rl_request('POST', f"{self.fapi_url}/fapi/v1/order",
                          endpoint='um_order',
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=15)
        if r.status_code != 200:
            raise Exception(f"open_long Error {r.status_code}: {r.text}")
        result = r.json()
        return result


    # get_position_mode 已删除 - fapi接口不可用,PM账户用单向持仓无需查询


    def close_long(self, symbol: str, quantity: float = None) -> Dict:
        """平多仓 - 只接受LONG持仓(PAPI/fapi)"""
        if SIMULATE:
            global _sim_balance, _sim_positions
            _load_sim_state()
            if symbol not in _sim_positions or not _sim_positions[symbol]:
                raise Exception(f"[SIMULATE] No LONG position found for {symbol}")
            # 找最后一层 LONG（后进先出）
            if _sim_positions[symbol][-1]['side'] != 'LONG':
                raise Exception(f"[SIMULATE] {symbol} top layer is not LONG")
            pos = _sim_positions[symbol][-1]
            entry = pos['entry_price']
            margin = pos['margin']
            qty = pos['qty']
            try:
                klines = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=15).json()
                price = float(klines[0][4])
            except Exception:
                price = entry
            pnl = (price - pos['entry_price']) / pos['entry_price'] * pos.get('leverage', 10) * pos.get('margin', 0)
            close_fee = pos.get('margin', 0) * 0.001
            net_pnl = pnl - close_fee
            _sim_balance += net_pnl + pos.get('margin', 0)  # 平仓返还保证金 + 盈亏
            # pop 最后一层，不直接删除整个 symbol
            _sim_positions[symbol].pop()
            if not _sim_positions[symbol]:
                del _sim_positions[symbol]
            _save_sim_state(_sim_balance, _sim_positions)
            print(f"[SIMULATE] 平多 {symbol} x{qty:.4f} @ {price}, 盈亏={net_pnl:.4f} USDT, 余额={_sim_balance:.4f}", file=sys.stderr)
            return {'orderId': 'sim_' + str(time.time()), 'symbol': symbol, 'side': 'SELL', 'origQty': str(qty), 'pnl': net_pnl, 'margin': margin}

        positions = self.get_positions(symbol)
        has_long = any(pos.get('positionSide') == 'LONG' or pos['amount'] > 0 for pos in positions)
        if not has_long:
            raise Exception(f"No LONG position found for {symbol}")

        if quantity is None:
            for pos in positions:
                if pos.get('positionSide') == 'LONG' or pos['amount'] > 0:
                    quantity = abs(pos['amount'])
                    break
        if quantity is None or quantity <= 0:
            raise Exception(f"No LONG position found for {symbol}")

        import math
        qty_int = round(quantity)
        if qty_int == 0:
            qty_int = 1

        if is_portfolio_margin():
            positions = self.get_positions(symbol)
            has_long = any(pos.get('positionSide') == 'LONG' or pos['amount'] > 0 for pos in positions)
            if not has_long:
                raise Exception(f"No LONG position found for {symbol}")
            for pos in positions:
                if pos.get('positionSide') == 'LONG' or pos['amount'] > 0:
                    quantity = abs(pos['amount'])
                    break
            # 市价全平，直接用 quantity 避免名义值 < 5 USDT 被拒
            result = self._papi_request('POST', '/papi/v1/um/order', {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'MARKET',
                'quantity': quantity
            })
            self._clear_conditional_orders(symbol)
            return result
        # Bug #18 Fix: 检查名义价值，避免 $5 限制报错 -2019
        try:
            current_price = self.get_price(symbol)
            notional = quantity * current_price
            if notional < 5.0:
                # 名义价值过低 → 先报价值 + 建议
                print(f"⚠️ 平仓名义价值 ${notional:.2f} < 5 USDT，强制平仓可能被 -2019 拒绝", file=sys.stderr)
                print(f"   建议: 通过 reduceOnly 条件单 设为 STOP_MARKET 贴市价出发 (平仓最小单位是 step_size)", file=sys.stderr)
        except Exception:
            pass
        params = {
            'symbol': symbol,
            'side': 'SELL',
            'positionSide': 'LONG',
            'type': 'MARKET',
            'quantity': quantity,
            'timestamp': int(time.time()*1000)
        }
        params['signature'] = self._sign(params)
        r = _rl_request('POST', f"{self.fapi_url}/fapi/v1/order",
                          endpoint='um_order',
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=15)
        if r.status_code != 200:
            raise Exception(f"close_long Error {r.status_code}: {r.text}")
        return r.json()
# ========== 技术指标计算 ==========

# ========== 做空分析 ==========


# ========== LLM 分析模块 ==========


# ========== 主程序(扫描 + 指标 + 账户操作 + 命令执行)==========


# ========== 统一波动率扫描(多空一起获取)==========

# ========== 速率限制 ==========
# Binance Futures API Rate Limits (futures/usdel撮合):
#   - 6000 weight / minute (REQUEST + ORDER combined; 2025 提升)
#   - 2400 weight / minute (READ endpoint)
#   - 60 weight / second burst
# Reference: https://developers.binance.net/docs/rate-limits

_WEIGHT_CONFIG = {
    # Endpoint : (weight,  per_second_burst)
    'ticker/24hr':       (40,  5),   # 40w, ~30/s
    'ticker/price':      (2,   20),  # 2w
    'exchangeInfo':      (10,  45),  # 10w
    'klines':            (5,   120), # 5w per symbol+interval per request
    'depth':             (5,   120), # 5w
    'premiumIndex':       (5,   120), # funding rate / mark price
    'account':           (10,  45),
    'um_position_risk':  (5,   120),
    'um_order':          (1,   120),  # 实际 Binance 下单 1w,这里 1w
    'leverage':          (1,   120),  # 设杠杆 1w
    'default':            (5,   120),
}


class RateLimiter:
    """滑动窗口速率限制器 - 按权重计数
    持久化支持: cron 5min 一次,每次都是新进程,需要跨进程记忆 weight 历史。
    滑动窗口状态存到 .rate_limit_state.json (1s+60s 两个队列),
    新进程启动时从文件加载 → 合并 → 继续限速。
    """
    _STATE_FILE = os.path.join(os.path.dirname(__file__), '.rate_limit_state.json')
    _STATE_TTL = 70  # 超过 70s 的记录肯定超出 60s 窗口,无需保留

    def __init__(self, max_weight_per_sec: int = 60, max_weight_per_min: int = 6000):
        self.max_weight_per_sec = max_weight_per_sec
        self.max_weight_per_min = max_weight_per_min
        self._sec_timestamps = []  # [timestamp, ...] last-second window
        self._min_timestamps = []  # [timestamp, ...] last-minute window
        self._lock = __import__('threading').Lock()
        self._load_state()  # 跨进程同步:从文件读入上次的滑动窗口

    def _load_state(self):
        """从文件加载上次的滑动窗口状态(1s+60s 合并)
        Bug Fix (2026-06-06): 兼容空文件/损坏文件, 不再静默丢状态
        """
        if not os.path.exists(self._STATE_FILE):
            return
        # 空文件 (0 字节) 不算损坏, 是历史 Bug 残留, 静默跳过
        try:
            size = os.path.getsize(self._STATE_FILE)
            if size == 0:
                return
        except OSError:
            return
        try:
            with open(self._STATE_FILE, 'r') as f:
                state = json.load(f)
            now = time.time()
            # 1s 窗口只保留最近 1s(作用小,主要是能拒绝并发)
            sec = [t for t in state.get('sec', []) if now - t < 1.0]
            min_ = [t for t in state.get('min', []) if now - t < 60.0]
            self._sec_timestamps = sec
            self._min_timestamps = min_
            if sec or min_:
                print(f"  📊 限速器从文件恢复: 1s窗口={len(sec)} 60s窗口={len(min_)}", file=sys.stderr)
        except Exception as e:
            # 损坏文件 → 重命名备份, 让下次启动从干净状态开始
            try:
                os.rename(self._STATE_FILE, self._STATE_FILE + f'.corrupt.{int(time.time())}')
            except OSError:
                pass
            print(f"  ⚠️ 限速器状态文件损坏已隔离: {e}", file=sys.stderr)

    def _save_state(self):
        """持久化滑动窗口到文件(每次 acquire 后调用)
        Bug Fix (2026-06-06): 改用 temp + os.replace 原子写, 避免并发/中断导致 .json 文件被截断
        (症状: 下次启动报 "Expecting value: line 1 column 1 (char 0)" → 跨进程限速失效)
        """
        try:
            now = time.time()
            sec = [t for t in self._sec_timestamps if now - t < 1.0]
            min_ = [t for t in self._min_timestamps if now - t < 60.0]
            payload = json.dumps({'sec': sec, 'min': min_, 'ts': now})
            # 写临时文件 → fsync → rename (POSIX 原子) → 避免半写入文件被读到
            tmp = self._STATE_FILE + '.tmp'
            with open(tmp, 'w') as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._STATE_FILE)
        except Exception as e:
            # 状态文件写失败不应该让交易中断
            pass

    def acquire(self, weight: int, wait: bool = True, timeout: float = 30.0) -> bool:
        with self._lock:
            now = time.time()
            # 滑动窗口:清除过期的
            self._sec_timestamps = [t for t in self._sec_timestamps if now - t < 1.0]
            self._min_timestamps = [t for t in self._min_timestamps if now - t < 60.0]
            sec_used = len(self._sec_timestamps)
            min_used = len(self._min_timestamps)
            if sec_used + weight <= self.max_weight_per_sec and min_used + weight <= self.max_weight_per_min:
                for _ in range(weight):
                    self._sec_timestamps.append(now)
                    self._min_timestamps.append(now)
                self._save_state()  # 跨进程同步
                return True
        if not wait:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.1)
            with self._lock:
                now = time.time()
                self._sec_timestamps = [t for t in self._sec_timestamps if now - t < 1.0]
                self._min_timestamps = [t for t in self._min_timestamps if now - t < 60.0]
                if len(self._sec_timestamps) + weight <= self.max_weight_per_sec and \
                   len(self._min_timestamps) + weight <= self.max_weight_per_min:
                    for _ in range(weight):
                        self._sec_timestamps.append(now)
                        self._min_timestamps.append(now)
                    self._save_state()  # 跨进程同步
                    return True
        return False


def _binance_weight(endpoint: str, method: str = 'GET') -> int:
    return _WEIGHT_CONFIG.get(endpoint, _WEIGHT_CONFIG['default'])[0]


_rl = RateLimiter()


def _rl_request(method: str, url: str, endpoint: str = '', **kwargs) -> requests.Response:
    """带速率限制的 requests 封装 - 自动扣权重,遇到限速自动退让"""
    weight = _binance_weight(endpoint or url)
    kwargs.setdefault('timeout', 30 if method == 'GET' else 10)
    if not _rl.acquire(weight, wait=True, timeout=30.0):
        raise Exception(f"Rate limit timeout after 30s (weight={weight}) for {endpoint or url}")
    # Bug #1 Fix: 初始化为有效异常对象,避免 3 次重试都走"正常路径"时 raise None
    # （2026-06-03 修复:旧逻辑 last_err=None + 418 跳过赋值 → TypeError）
    last_err = Exception(f"Request failed after 3 attempts for {endpoint or url}")
    for attempt in range(3):
        try:
            r = requests.request(method, url, **kwargs)
            # 遇到限速(429/418)- 等待一段时间后重试
            if r.status_code in (418, 429):
                last_err = Exception(f"Rate limited ({r.status_code}) for {endpoint} after {attempt+1} attempts")
                wait_sec = (attempt + 1) * 2  # 2s, 4s, 6s
                print(f"  ⚠️ Rate limited ({r.status_code}) for {endpoint}, waiting {wait_sec}s (attempt {attempt+1}/3)...", file=sys.stderr)
                time.sleep(wait_sec)
                continue
            return r
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
            continue
    # last_err 肯定不是 None(初始化为有效异常),raise 不会变 TypeError
    raise last_err


def _rate_limit():
    """旧兼容函数 - 扣1个默认权重"""
    _rl.acquire(_WEIGHT_CONFIG['default'][0])


def _get_klines_raw(symbol: str, interval: str, limit: int) -> List:
    """Get raw klines without computing indicators (with retry, PM-safe)
    2026-06-04 修复: 改走 _rl_request 自动处理 418/429(指数退避 + 滑动窗口限速)
    """
    # PM账户优先papi公开端点,降级fapi
    for attempt in range(3):  # 最多重试3次
        try:
            # 优先 papi(PM账户无需签名)
            r = _rl_request(
                'GET',
                f"{PAPI_URL}/papi/v1/um/klines",
                endpoint='um/klines',
                params={'symbol': symbol, 'interval': interval, 'limit': limit},
                timeout=15,
                verify=certifi.where()
            )
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return data
            # papi失败,降级fapi
        except Exception:
            pass
        # 降级fapi
        try:
            r = _rl_request(
                'GET',
                f"{FAPI_URL}/fapi/v1/klines",
                endpoint='klines',
                params={'symbol': symbol, 'interval': interval, 'limit': limit},
                timeout=15,
                verify=certifi.where()
            )
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return data
            time.sleep(0.2)
            continue
        except Exception as e:
            time.sleep(0.3)
            continue
    # 3次都失败,打印警告
    print(f"  ⚠️ {symbol} {interval} K线获取失败", file=sys.stderr)
    return []


def _round_to_tick(price: float, symbol: str) -> float:
    """将价格对齐到 tickSize 精度,避免 Precision over maximum 错误

    Bug fix: 之前返回的 float 有浮点误差(0.028460000000000003),
    现在用字符串格式化对齐到 tickSize 指定的小数位数。
    优先 fapi exchangeInfo(更快),papi exchangeInfo 降级。
    """
    for attempt in range(3):
        try:
            r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/exchangeInfo",
                endpoint='exchangeInfo',
                timeout=15,
                verify=certifi.where()
            )
            if r.status_code != 200:
                raise Exception(f"status {r.status_code}")
            data = r.json()
            sym_data = next((s for s in data.get('symbols', []) if s.get('symbol') == symbol), None)
            if not sym_data:
                raise Exception(f"symbol {symbol} not in exchangeInfo")
            for f in sym_data.get('filters', []):
                if f.get('filterType') == 'PRICE_FILTER':
                    tick_str = f['tickSize']
                    tick = float(tick_str)
                    # 计算 tickSize 的小数位数(如 0.0000100 → 5位)
                    decimals = 0
                    if '.' in tick_str:
                        decimals = len(tick_str.rstrip('0').split('.')[1])
                    # round 后用字符串格式化消除浮点误差
                    raw = round(price / tick) * tick
                    return float(f"{raw:.{decimals}f}")
            break
        except Exception:
            pass
        try:
            # papi 降级
            r = _rl_request('GET', f"{PAPI_URL}/papi/v1/um/exchangeInfo",
                endpoint='um/exchangeInfo',
                params={'symbol': symbol},
                timeout=15,
                verify=certifi.where()
            )
            if r.status_code == 200:
                data = r.json()
                sym_data = next((s for s in data.get('symbols', []) if s.get('symbol') == symbol), None)
                if sym_data:
                    for f in sym_data.get('filters', []):
                        if f.get('filterType') == 'PRICE_FILTER':
                            tick_str = f['tickSize']
                            tick = float(tick_str)
                            decimals = 0
                            if '.' in tick_str:
                                decimals = len(tick_str.rstrip('0').split('.')[1])
                            raw = round(price / tick) * tick
                            return float(f"{raw:.{decimals}f}")
                break
        except Exception:
            pass
        time.sleep(0.3 * (attempt + 1))
    # 兜底:用 tickSize 0.0000100 → 5位小数
    return float(f"{price:.5f}")

def _get_price_decimals(symbol: str) -> int:
    """获取交易对的价格小数位数

    从 exchangeInfo 的 PRICE_FILTER 获取 tickSize，计算小数位数。
    例如: tickSize=0.0001 → 4, tickSize=0.01 → 2
    """
    for attempt in range(3):
        try:
            r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/exchangeInfo",
                endpoint='exchangeInfo',
                timeout=15,
                verify=certifi.where()
            )
            if r.status_code != 200:
                raise Exception(f"status {r.status_code}")
            data = r.json()
            sym_data = next((s for s in data.get('symbols', []) if s.get('symbol') == symbol), None)
            if not sym_data:
                raise Exception(f"symbol {symbol} not in exchangeInfo")
            for f in sym_data.get('filters', []):
                if f.get('filterType') == 'PRICE_FILTER':
                    tick_str = f['tickSize']
                    if '.' in tick_str:
                        return len(tick_str.rstrip('0').split('.')[1])
                    return 0
            break
        except Exception:
            pass
        try:
            r = _rl_request('GET', f"{PAPI_URL}/papi/v1/um/exchangeInfo",
                endpoint='um/exchangeInfo',
                params={'symbol': symbol},
                timeout=15,
                verify=certifi.where()
            )
            if r.status_code == 200:
                data = r.json()
                sym_data = next((s for s in data.get('symbols', []) if s.get('symbol') == symbol), None)
                if sym_data:
                    for f in sym_data.get('filters', []):
                        if f.get('filterType') == 'PRICE_FILTER':
                            tick_str = f['tickSize']
                            if '.' in tick_str:
                                return len(tick_str.rstrip('0').split('.')[1])
                            return 0
        except Exception:
            pass
        import time
        time.sleep(0.3 * (attempt + 1))
    # 兜底: 默认 4 位小数
    return 4

# 价格小数位缓存（避免每次持仓都调 exchangeInfo）
_PRICE_DECIMALS_CACHE: Dict[str, int] = {}

def _get_price_decimals_cached(symbol: str) -> int:
    """带缓存的版本,status 输出多币种时避免重复打 exchangeInfo"""
    if symbol not in _PRICE_DECIMALS_CACHE:
        _PRICE_DECIMALS_CACHE[symbol] = _get_price_decimals(symbol)
    return _PRICE_DECIMALS_CACHE[symbol]

def _fmt_price(price: float, symbol: str = None, default_decimals: int = 4) -> str:
    """统一格式化价格显示 - 根据 symbol 自动选小数位

    - 传 symbol: 用 _get_price_decimals_cached 自动取
    - 不传 symbol: 用 default_decimals 兜底
    """
    if symbol:
        d = _get_price_decimals_cached(symbol)
    else:
        d = default_decimals
    return f"${price:.{d}f}"


def _get_atr_and_volatility(symbol: str) -> Dict:
    """获取 ATR 和波动率 (PM-safe: papi优先,fapi降级,SSL正确配置)
    
    Returns:
        {'atr': float, 'atr_percent': float, 'volatility': 'low'|'medium'|'high'}
        - atr_percent: ATR占当前价格百分比
        - volatility: low(<0.5%), medium(0.5%-3%), high(>3%)
    """
    klines = []
    # 优先 fapi（公开端点，无认证门槛），降级 papi
    for attempt in range(3):
        try:
            r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/klines",
                endpoint='klines',
                params={'symbol': symbol, 'interval': '1h', 'limit': 60},
                timeout=15,
                verify=certifi.where()
            )
            klines = r.json()
            if isinstance(klines, list) and len(klines) > 15:
                break
        except Exception as e:
            if attempt == 0:
                print(f"  ⚠️ {symbol} ATR fapi SSL错误,切papi: {e}", file=sys.stderr)
        # 降级 papi
        try:
            r = _rl_request('GET', f"{PAPI_URL}/papi/v1/um/klines",
                endpoint='um/klines',
                params={'symbol': symbol, 'interval': '1h', 'limit': 60},
                timeout=15,
                verify=certifi.where()
            )
            klines = r.json()
            if isinstance(klines, list) and len(klines) > 15:
                break
        except Exception:
            pass
        time.sleep(0.2 * (attempt + 1))
    
    if not klines or not isinstance(klines, list) or len(klines) < 15:
        return {'atr': 0, 'atr_percent': 0, 'volatility': 'medium'}
    
    # 计算 ATR (14 周期)
    trs = []
    for i in range(1, min(15, len(klines))):
        h = float(klines[-i][2])
        l = float(klines[-i][3])
        pc = float(klines[-i-1][4])
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    atr = sum(trs) / len(trs) if trs else 0
    
    # 当前价格
    current_price = float(klines[-1][4])
    atr_percent = (atr / current_price * 100) if current_price > 0 else 0
    
    # 波动率分类
    if atr_percent < 0.5:
        volatility = 'low'
    elif atr_percent > 3.0:
        volatility = 'high'
    else:
        volatility = 'medium'
    
    return {'atr': atr, 'atr_percent': atr_percent, 'volatility': volatility}


def _check_lg_md_compliance(symbol: str, side: str) -> Dict:
    """lg.md 6.2.1 / 6.2.2 硬门检查 — P0 2026-06-06 添加
    背景: 30 天实盘 170 笔配对 总 PnL -3.69 USDT, 胜率 30%. 
         LONG 3 笔穿透 5% 地板 (STG -12.36% / BASED -7.20% / PORTAL -6.19%),
         SHORT 4 笔穿透. 多数都是 "1d 趋势仍空但 LLM 看 1h 反弹追多" / 
         "1d 横盘但 LLM 看 4h 阴线追空" 的尺度矛盾交易.
    修法: 在 do_open_long/short 中插入程序级硬门, 违反 lg.md 规则的直接拒开仓.
    
    Args:
        symbol: 交易币种
        side: 'LONG' 或 'SHORT'
    
    Returns:
        {'pass': bool, 'reason': str, 'evidence': dict}
    """
    evidence = {
        '1d_dir': None,   # -1/0/+1
        '1d_evidence': '',
        '4h_dir': None,
        '4h_evidence': '',
        '1h_dir': None,
        '1h_evidence': '',
    }
    # 拉多周期 K 线 (1d×10, 4h×20, 1h×30) 走 3 路线程，每条返独立方向
    try:
        klines_1d = _get_klines_raw(symbol, '1d', 10)
        klines_4h = _get_klines_raw(symbol, '4h', 20)
        klines_1h = _get_klines_raw(symbol, '1h', 30)
    except Exception as e:
        return {'pass': False, 'reason': f'K线拉取异常: {e}', 'evidence': evidence}
    
    if not klines_1d or not klines_4h or not klines_1h:
        return {'pass': False, 'reason': 'K线数据不足(至少要 1d/4h/1h 都有)', 'evidence': evidence}
    
    def _trend(klines: List) -> int:
        """从 K 线序列判断趋势方向.
        规则: 最近 5 根 close 均价 vs 前 5 根 close 均价, >1% 偏差才记方向.
        -1=下跌, 0=震荡, +1=上涨
        """
        if len(klines) < 10:
            return 0
        closes = [float(k[4]) for k in klines]
        recent = sum(closes[-5:]) / 5
        prior  = sum(closes[-10:-5]) / 5
        if prior <= 0:
            return 0
        chg = (recent - prior) / prior * 100
        if chg > 1.0:
            return 1
        elif chg < -1.0:
            return -1
        return 0
    
    d_1d = _trend(klines_1d)
    d_4h = _trend(klines_4h)
    d_1h = _trend(klines_1h)
    evidence['1d_dir'] = d_1d
    evidence['4h_dir'] = d_4h
    evidence['1h_dir'] = d_1h
    evidence['1d_evidence'] = f'近5vs前5 close 均值差判定: dir={d_1d:+d}'
    evidence['4h_evidence'] = f'近5vs前5 close 均值差判定: dir={d_4h:+d}'
    evidence['1h_evidence'] = f'近5vs前5 close 均值差判定: dir={d_1h:+d}'
    
    if side.upper() == 'LONG':
        # lg.md 6.2.1 LONG 严苛: 1d 趋势必须与方向一致, 1d 量价状态必须支持
        if d_1d <= 0:
            return {
                'pass': False,
                'reason': f'lg.md 6.2.1 LONG 严苛不通过: 1d 趋势不明确或下跌 (dir={d_1d:+d}), 禁止开多. 哪怕 1h/15m 看起来反弹也拒绝.',
                'evidence': evidence,
            }
        return {'pass': True, 'reason': 'OK (1d 上涨趋势, LONG 合规)', 'evidence': evidence}
    
    else:  # SHORT
        # lg.md 6.2.2 SHORT 警告: 必须 1d/4h/1h 三尺度都明确做空
        if d_1d >= 0:
            return {
                'pass': False,
                'reason': f'lg.md 6.2.2 SHORT 严苛不通过: 1d 趋势不明确或上涨 (dir={d_1d:+d}), 禁止开空.',
                'evidence': evidence,
            }
        if d_4h >= 0:
            return {
                'pass': False,
                'reason': f'lg.md 6.2.2 SHORT 严苛不通过: 4h 趋势不明确或上涨 (dir={d_4h:+d}), 必须三尺度一致做空才允许. 1d={d_1d:+d} 4h={d_4h:+d} 1h={d_1h:+d}',
                'evidence': evidence,
            }
        if d_1h >= 0:
            return {
                'pass': False,
                'reason': f'lg.md 6.2.2 SHORT 严苛不通过: 1h 趋势不明确或上涨 (dir={d_1h:+d}), 必须三尺度一致做空才允许. 1d={d_1d:+d} 4h={d_4h:+d} 1h={d_1h:+d}',
                'evidence': evidence,
            }
        return {'pass': True, 'reason': 'OK (1d/4h/1h 三尺度一致下跌, SHORT 合规)', 'evidence': evidence}


def _check_lg_md_strict(symbol: str, side: str) -> Dict:
    """lg.md 6.2.1/6.2.2 软门总入口 — P0 2026-06-06 改为软提示 (用户纠正: 不要硬编码, 亏本要改策略)
    背景: lg.md 0.4/0.7/6.1 明文 "不引入固定阈值", 严苛规则是给 LLM 看的策略逻辑.
         程序层硬门会绕过 "多视角+多尺度集成", 直接单维度 1d 阻断 → 错过 1d 空但 4h 反弹 V 反的真机会.
         决定: 改硬门为软门, 报警但不阻断, 由 LLM 主处理.
    """
    # 1. 趋势/尺度严苛检查 (仅提示)
    compliance = _check_lg_md_compliance(symbol, side)
    # 不阻断, 不论 pass/false 都返回
    return compliance


def _calculate_stop_loss(entry_price: float, atr: float, atr_percent: float, side: str) -> Dict:
    """只计算止损价格 (v4 简化 2026-06-09: 用户原话 "不要止赢, 只要止损就好")
    - LONG: 止损 = entry × 0.97 = 3% 紧
    - SHORT: 止损 = entry × 1.03 = 3% 紧
    - **不设止盈** — LLM 自由决定平仓时机
    
    Args:
        entry_price: 入场价格
        atr: ATR 绝对值（保留参数兼容，v4 不使用）
        atr_percent: ATR 占价格百分比（保留参数兼容，v4 不使用）
        side: 'LONG' 或 'SHORT'
    
    Returns:
        {'sl_trigger': float, 'sl_percent': float}
    """
    sl_pct = 3.0  # 紧止损 3%（用户原话 current × 0.97 / × 1.03）
    
    if side.upper() == 'LONG':
        sl_trigger = entry_price * (1 - sl_pct / 100)  # current × 0.97
    else:  # SHORT
        sl_trigger = entry_price * (1 + sl_pct / 100)  # current × 1.03
    
    return {
        'sl_trigger': sl_trigger,
        'sl_percent': round(sl_pct, 3)
    }


def scan_volatility_top(top_n: int = 30, min_vol: float = 0.5, top_klines: int = 30,
                       max_vol_24h: float = 20.0, max_chg_24h: float = 10.0,
                       min_quote_volume: float = 5_000_000, kline_detail: bool = False) -> List[Dict]:
    """
    Unified scan: Binance sortBy server-side ranking
    Get coins, fetch klines, hand to LLM for unified LONG/SHORT analysis

    过滤逻辑 v2 (2026-06-06 策略改造, 数据驱动):
      背景: 30 天实盘 129 笔配对中, |24h-chg| > 12% 的交易 = 插针抢反弹 (3 笔全部大亏)
      目标: 砍"暴跌抢反弹"型机会, 只看"温和已动"型
    1. 只取 status=TRADING 的币种
    2. 排除 blacklist.json 黑名单
    3. **温和优先**: |24h-chg| <= max_chg_24h (默认 ±12%)
    4. 活跃度上限: 24h-vol <= max_vol_24h (默认 25%, 避免暴跌大涨)
    5. 活跃度下限: 1h-vol >= min_vol (默认 3%, 排除死水)
    6. 流动性: 24h quote_volume >= 5M USDT (排除小币种插针)
    7. 排序: **按 |24h-chg| 升序** (找"刚开始动"的币, 不是"已爆跌/暴涨"的币)
    8. 方向判断交给 LLM
    """
    print(f"\n{'='*70}")
    print(f"UNIFIED SCAN - Binance sortBy + LLM Batch Analysis")
    print(f"{'='*70}")

    # 加载黑名单
    blacklist = set()
    try:
        with open(os.path.join(os.path.dirname(__file__), 'blacklist.json')) as f:
            bl = json.load(f)
            blacklist.update(bl.get('permanent_delist', []))
            blacklist.update(bl.get('coins', []))
    except:
        pass

    # 获取可交易币种(只取 TRADING 状态, fapi优先)
    _rate_limit()
    try:
        r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/exchangeInfo", endpoint='exchangeInfo', timeout=15)
        exchange_info = r.json()
        if not isinstance(exchange_info, dict) or 'symbols' not in exchange_info:
            raise Exception("fapi exchangeInfo invalid")
    except Exception:
        try:
            r = _rl_request('GET', f"{PAPI_URL}/papi/v1/um/exchangeInfo", endpoint='um/exchangeInfo', timeout=15)
            exchange_info = r.json()
        except Exception:
            print(f"❌ exchangeInfo 获取失败")
            return []
    tradeable_symbols = set()
    for s in exchange_info.get('symbols', []):
        if s.get('status') == 'TRADING' and s.get('quoteAsset') == 'USDT':
            sym = s['symbol']
            # 检查最小成交额 < 50U
            for f in s.get('filters', []):
                if f.get('filterType') == 'MIN_NOTIONAL':
                    min_notional = float(f.get('minNotional', 0))
                    if min_notional < 50:
                        tradeable_symbols.add(sym)
                        break
    print(f"可交易币种: {len(tradeable_symbols)}")

    # 获取24h行情(fapi优先,pap作备选)
    _rate_limit()
    try:
        r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={"limit": top_n * 3, "sortBy": "priceChangePercent", "sortType": "DESC"}, timeout=15)
        all_tickers_raw = r.json()
        if isinstance(all_tickers_raw, dict) and all_tickers_raw.get('code') == -1003:
            raise Exception("rate limited")
        if not isinstance(all_tickers_raw, list):
            raise Exception("not a list")
    except Exception:
        try:
            r = _rl_request('GET', f"{PAPI_URL}/papi/v1/um/ticker/24hr", endpoint='um/ticker/24hr', params={"limit": top_n * 3, "sortBy": "priceChangePercent", "sortType": "DESC"}, timeout=15)
            all_tickers_raw = r.json()
            if isinstance(all_tickers_raw, dict) and all_tickers_raw.get('code') == -1003:
                raise Exception("rate limited")
        except Exception:
            # 全部失败
            print(f"❌ ticker/24hr 获取失败")
            return []
    all_tickers = all_tickers_raw
    # 处理正常响应(list)或rate limit错误(dict)
    if isinstance(all_tickers, dict) and all_tickers.get('code') == -1003:
        print(f"⚠️ Binance 24hr API 速率限制,切换到本地排序模式")
        # 回退:获取全部24hr数据用本地Python排序
        _rate_limit()
        try:
            r2 = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={"limit": 200}, timeout=15)
            all_tickers = r2.json()
            if isinstance(all_tickers, dict) and all_tickers.get('code') == -1003:
                raise Exception("rate limited")
        except Exception:
            try:
                r2 = _rl_request('GET', f"{PAPI_URL}/papi/v1/um/ticker/24hr", endpoint='um/ticker/24hr', params={"limit": 200}, timeout=15)
                all_tickers = r2.json()
                if isinstance(all_tickers, dict) and all_tickers.get('code') == -1003:
                    raise Exception("rate limited")
            except Exception:
                pass
        if isinstance(all_tickers, dict) and all_tickers.get('code') == -1003:
            print(f"⚠️ Binance API 全面受限,等待60秒后重试")
            time.sleep(60)
            try:
                r2 = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={"limit": 200}, timeout=15)
                all_tickers = r2.json()
            except Exception:
                try:
                    r2 = _rl_request('GET', f"{PAPI_URL}/papi/v1/um/ticker/24hr", endpoint='um/ticker/24hr', params={"limit": 200}, timeout=15)
                    all_tickers = r2.json()
                except Exception:
                    pass
        if isinstance(all_tickers, dict) and all_tickers.get('code') == -1003:
            print(f"⚠️ 所有API均受限,尝试备选方案...")
            try:
                btc_r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/price", endpoint='ticker/price', params={"symbol": "BTCUSDT"}, timeout=15)
                btc_data = btc_r.json()
                if isinstance(btc_data, dict) and btc_data.get('symbol'):
                    btc_change = 0.0
                else:
                    btc_change = 0.0
            except:
                btc_change = 0.0
            print(f"⚠️ API 受限期间无法获取完整市场数据,请稍后重试")
            print(f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')},Binance 限制可能持续 1-2 分钟")
            candidates = []
            return candidates
        usdt_pairs = [t for t in all_tickers if isinstance(t, dict) and t.get('symbol', '').endswith('USDT')]
        usdt_pairs.sort(key=lambda x: float(x.get('priceChangePercent', 0) or 0), reverse=True)
        usdt_pairs = usdt_pairs[:top_n * 3]
    else:
        usdt_pairs = [t for t in all_tickers if isinstance(t, dict) and t.get('symbol', '').endswith('USDT')]
    print(f"Binance sorted: {len(usdt_pairs)} candidates")

    candidates = []
    for ticker in usdt_pairs:
        sym = ticker.get('symbol', '')

        # 过滤:黑名单 + 非交易状态
        if sym in blacklist:
            continue
        if sym not in tradeable_symbols:
            continue

        try:
            price  = float(ticker.get('lastPrice', 0))
            high   = float(ticker.get('highPrice', 0))
            low    = float(ticker.get('lowPrice', 0))
            change = float(ticker.get('priceChangePercent', 0))
            volume = float(ticker.get('quoteVolume', 0))
        except (ValueError, TypeError):
            continue
        if price == 0 or low == 0:
            continue
        # v2 (2026-06-06): 最低成交额提高到 5M (避免小币种插针)
        if volume < min_quote_volume:
            continue
        vol_pct = (high - low) / price * 100
        # v2 过滤 (数据驱动 - 30 天实盘 129 笔配对分析):
        # |24h-chg| > 12% → 必亏 (3 笔 STG/BASED/PORTAL 都是 -6% ~ -12%)
        # 24h-vol > 25% → 暴跌抢反弹型
        # 1h-vol < 3% → 死水币
        if vol_pct < min_vol:
            continue
        if vol_pct > max_vol_24h:
            continue
        if abs(change) > max_chg_24h:
            continue
        # v2 (2026-06-06): 过滤 |24h-chg| < 0.5% 的"完全横盘死水"币
        # 背景: +0.07% / -0.10% 这种币不可能有趋势机会
        if abs(change) < 0.5:
            continue
        candidates.append({
            'symbol':      sym,
            'price':       price,
            'change_24h':  round(change, 2),
            'volume_24h':  volume,
            'vol_24h_pct': round(vol_pct, 2),
            'high_24h':    high,
            'low_24h':     low,
        })

    # v2 排序: 按 |24h-chg| 升序 (找"刚开始动"的温和机会, 不是"已爆跌"的暴跌)
    candidates.sort(key=lambda x: abs(x['change_24h']))
    top_vol = candidates[:top_n]

    print(f"After vol filter: {len(candidates)} >= {min_vol}% (24h-vol<={max_vol_24h}%, |24h-chg|<={max_chg_24h}%) | 取前{top_n}名 | K线候选{top_klines}个")
    print(f"\nFetching klines for all coins (rate-limit: 0.1s/req)...\n")

    # 批量获取K线(1h+30m),避免超限
    # top_klines 控制实际取K线的数量
    kline_coins = top_vol[:top_klines]

    # Bug Fix: 不再重复拉 ticker/24hr (40w × N → N×40w 浪费)
    # lastPrice / change_24h 等在 1762 那个 bulk ticker/24hr 响应里已经拿到 (c['price'])
    def fetch_coin_klines(c):
        sym = c['symbol']
        try:
            # 窗口对齐 lg.md §1: 1d=40 4h=90 1h=80 15m=160 5m=288
            # 2026-06-08 16:45 修复: scan-all 之前只拉 1h=12 / 5m=288, 大方向(1d) / 趋势(4h) / 近期脉动(15m) 三个尺度全缺
            klines_1d  = _get_klines_raw(sym, '1d',  40)
            klines_4h  = _get_klines_raw(sym, '4h',  90)
            klines_1h  = _get_klines_raw(sym, '1h',  80)
            klines_15m = _get_klines_raw(sym, '15m', 160)
            klines_5m  = _get_klines_raw(sym, '5m',  288)
            return sym, {
                'klines_1d':   klines_1d,
                'klines_4h':   klines_4h,
                'klines_1h':   klines_1h,
                'klines_15m':  klines_15m,
                'klines_5m':   klines_5m,
                'current_price': c.get('price', 0),  # 复用 bulk ticker 的 lastPrice
            }
        except Exception:
            return sym, {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_coin_klines, c): c for c in kline_coins}
        for future in as_completed(futures):
            sym, kls = future.result()
            for c in top_vol:
                if c['symbol'] == sym:
                    c.update(kls)
                    break

    # Bug #27 Fix: 从 klines_1h 计算 vol_1h_pct（之前 1h-vol 总是 0）
    for c in top_vol:
        klines_1h = c.get('klines_1h', [])
        if klines_1h and len(klines_1h) > 0:
            try:
                # 以最近 1 根 1h K 线为准
                last = klines_1h[-1]
                h = float(last[2]); l = float(last[3])
                close = float(last[4])
                if close > 0:
                    c['vol_1h_pct'] = round((h - l) / close * 100, 2)
                else:
                    c['vol_1h_pct'] = 0
            except Exception:
                c['vol_1h_pct'] = 0
        else:
            c['vol_1h_pct'] = 0

    # 不在程序内做 LLM 分析（程序不传策略）
    # LLM 拿到候选后，会用 `market --symbol=X` 精读 K 线，按 lg.md 自行判断
    for c in top_vol:
        c['direction'] = 'NEUTRAL'
        c['llm_reason'] = 'program-prefiltered; LLM to analyze via market cmd'
        c['confidence'] = 0

    # 打印最终输出（让 LLM 看到的是这里，不是 candidates 里被海选的）
    print(f"\n{'='*60}")
    print(f"📊 Top {len(top_vol)} 候选 (按 1h 波动率排序,LLM 自行精读)")
    print(f"{'='*60}")
    for i, c in enumerate(top_vol, 1):
        sym = c.get('symbol', '?')
        vol_1h = c.get('vol_1h_pct', 0) or 0
        vol_24h = c.get('vol_24h_pct', 0) or 0
        chg_24h = c.get('change_24h', 0) or 0
        price = c.get('last_price') or c.get('current_price') or 0
        # v6 (2026-06-08 20:23): 改成原始数据 — 量比/atrpct 是工程化加工, 论文 Kronos 只需 OHLC + 原始 volume
        # 量比/均量/atrpct 全部删, 5m 摘要只保留: 方向 (n_up/n_dn 原始计数) + 净值 (chg) + 最近 12 根原始 vol
        k5 = c.get('klines_5m', [])
        if k5 and len(k5) >= 12:
            last5 = k5[-12:]
            n_up = sum(1 for k in last5 if float(k[4]) > float(k[1]))
            n_dn = sum(1 for k in last5 if float(k[4]) < float(k[1]))
            chg_5m = (float(last5[-1][4]) - float(last5[0][1])) / float(last5[0][1]) * 100 if float(last5[0][1]) > 0 else 0
            dir5m = "📈" if n_up > n_dn + 1 else ("📉" if n_dn > n_up + 1 else "➡️")
            # 原始 volume 序列 (最近 12 根, 12 个数字)
            vols_raw = [float(k[5]) for k in last5]
            vols_str = ",".join(f"{v:.0f}" for v in vols_raw)
            print(f"  {i:2d}. {sym:15s} 1h-vol={vol_1h:6.2f}% 24h-vol={vol_24h:7.2f}% 24h-chg={chg_24h:+7.2f}% price={price}")
            print(f"     └─ 5m×12: {dir5m} chg={chg_5m:+.2f}% 涨{n_up}/跌{n_dn} vol={vols_str}")
        else:
            print(f"  {i:2d}. {sym:15s} 1h-vol={vol_1h:6.2f}% 24h-vol={vol_24h:7.2f}% 24h-chg={chg_24h:+7.2f}% price={price}")
    # v4 (2026-06-08): 内嵌 K 线同时输出 1d/4h/1h/15m/5m 五尺度 — lg.md §1 / Kronos 论文以 5m 为基准
    # 1h 静默时段 (亚洲早盘 07-09 GMT+8) 仅看 1h 会判"横盘"误判
    if kline_detail:
        print(f"\n📊 内嵌 K 线详情 (1d × 5 + 4h × 5 + 1h × 5 + 15m × 5 + 5m × 12, 每币 ~32 行, 减少 LLM 二次调用)")
        for c in top_vol:
            sym = c.get('symbol', '?')
            klines_1d  = c.get('klines_1d',  [])
            klines_4h  = c.get('klines_4h',  [])
            klines_1h  = c.get('klines_1h',  [])
            klines_15m = c.get('klines_15m', [])
            klines_5m  = c.get('klines_5m',  [])
            if not klines_1h:
                print(f"  {sym:15s} (K线拉取失败)")
                continue
            # 1d 5 根 (5 天趋势)
            if klines_1d:
                print(f"  {sym:15s} (1d × {len(klines_1d[-5:])}):")
                for k in klines_1d[-5:]:
                    o, h, l, cl, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
                    chg = (cl - o) / o * 100 if o > 0 else 0
                    ts = datetime.fromtimestamp(k[0] / 1000).strftime('%m-%d %H:%M')
                    print(f"    {ts} O:{o:.6f} H:{h:.6f} L:{l:.6f} C:{cl:.6f} ({chg:+.2f}%) V:{v:.0f}")
            # 4h 5 根 (近 1 天)
            if klines_4h:
                print(f"  {sym:15s} (4h × {len(klines_4h[-5:])}):")
                for k in klines_4h[-5:]:
                    o, h, l, cl, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
                    chg = (cl - o) / o * 100 if o > 0 else 0
                    ts = datetime.fromtimestamp(k[0] / 1000).strftime('%m-%d %H:%M')
                    print(f"    {ts} O:{o:.6f} H:{h:.6f} L:{l:.6f} C:{cl:.6f} ({chg:+.2f}%) V:{v:.0f}")
            # 1h 5 根 (近 5h)
            print(f"  {sym:15s} (1h × 5):")
            for k in klines_1h[-5:]:
                o, h, l, cl, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
                chg = (cl - o) / o * 100 if o > 0 else 0
                ts = datetime.fromtimestamp(k[0] / 1000).strftime('%m-%d %H:%M')
                print(f"    {ts} O:{o:.6f} H:{h:.6f} L:{l:.6f} C:{cl:.6f} ({chg:+.2f}%) V:{v:.0f}")
            # 15m 5 根 (近 1.25h)
            if klines_15m:
                print(f"  {sym:15s} (15m × {len(klines_15m[-5:])}):")
                for k in klines_15m[-5:]:
                    o, h, l, cl, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
                    chg = (cl - o) / o * 100 if o > 0 else 0
                    ts = datetime.fromtimestamp(k[0] / 1000).strftime('%m-%d %H:%M')
                    print(f"    {ts} O:{o:.6f} H:{h:.6f} L:{l:.6f} C:{cl:.6f} ({chg:+.2f}%) V:{v:.0f}")
            # 5m 12 根 (近 1h, Kronos 论文基准)
            if klines_5m:
                print(f"  {sym:15s} (5m × {len(klines_5m[-12:])}):")
                for k in klines_5m[-12:]:
                    o, h, l, cl, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
                    chg = (cl - o) / o * 100 if o > 0 else 0
                    ts = datetime.fromtimestamp(k[0] / 1000).strftime('%m-%d %H:%M')
                    print(f"    {ts} O:{o:.6f} H:{h:.6f} L:{l:.6f} C:{cl:.6f} ({chg:+.2f}%) V:{v:.0f}")
    print()

    return candidates


def get_market_data(symbol: str, kline_count: int = 15, intervals: List[str] = None) -> Dict:
    """获取市场数据(无指标版:只返回K线原始数据供LLM分析)
    ⚠️ 严禁使用 RSI/MACD/MA/BB 等指标 - 指标是滞后的谎言
    Args:
        symbol: 币种
        kline_count: 每个周期取的K线数
        intervals: K线周期列表,如 ['1d','4h','1h','15m'];为 None 默认 ['30m','1h']
    """
    trader = BinanceTrader()
    # Bug #19 Fix: 支持多尺度（lg.md 第六章 6.1 要求"至少两个尺度方向一致"）
    if intervals is None:
        intervals = ['30m', '1h']
    # 窗口对齐 lg.md §1: 1d=40 4h=90 1h=80 15m=160 5m=288 (5×24=120 根/天, 288=2.4天, 论文 1 天)
    # 2026-06-08 16:41 修复: 之前硬编码 1h=30 / 4h=40 严重不足, LLM 看 4h 只看到 6.6 天结构
    iv_limits = {
        '1m': 288, '5m': 288, '15m': 160, '30m': 160, '1h': 80, '4h': 90, '1d': 40,
    }
    klines_data = {}
    # Bug #21 Fix: 并发拉取多周期 K线（避免串行超时）
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_one(iv_name: str) -> tuple:
        return iv_name, trader.get_klines(symbol, iv_name, limit=iv_limits[iv_name])

    valid_intervals = [iv for iv in intervals if iv in iv_limits]
    invalid_intervals = [iv for iv in intervals if iv not in iv_limits]
    for iv in invalid_intervals:
        print(f"⚠️ 不支持的周期 {iv}，跳过（支持 1m/5m/15m/30m/1h/4h/1d）", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=min(4, len(valid_intervals) or 1)) as executor:
        futures = {executor.submit(_fetch_one, iv): iv for iv in valid_intervals}
        for future in as_completed(futures):
            iv_name, kls = future.result()
            klines_data[iv_name] = kls
    if not klines_data:
        # 检查是否全部被拒绝（不合法周期）
        if intervals and all(iv not in iv_limits for iv in intervals):
            print(f"❌ 所有 --interval 都不合法: {intervals} (支持 1m/5m/15m/30m/1h/4h/1d)", file=sys.stderr)
        else:
            print(f"❌ 无法获取 {symbol} 任何周期 K线数据", file=sys.stderr)
        return None
    # 当前价格用最新非空周期的最后一根
    current_price = None
    for iv_name in intervals:
        kls = klines_data.get(iv_name, [])
        if kls:
            current_price = float(kls[-1][4])
            break
    if current_price is None:
        print(f"❌ 无法获取 {symbol} 价格", file=sys.stderr)
        return None

    # SOUL.md 严禁使用 MACD/RSI/MA/BB 等技术指标。ATR 是趋势滞后指标，也不在计算中使用。
    # 这里不计算 atr_percent，需要时由 trader._get_atr_and_volatility(symbol) 单独调。

    # 标注未闭合K线（当前时间段尚未结束）
    now_ts = time.time()
    unclosed = []
    for iv_name, klines in klines_data.items():
        if not klines:
            continue
        # 计算周期秒数
        interval_map = {'1m': 60, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, '1d': 86400}
        interval_sec = interval_map.get(iv_name, 1800)
        last_kline_ts = klines[-1][0] / 1000
        if now_ts - last_kline_ts < interval_sec * 0.9:  # 当前K线未闭合（时间过了90%以内）
            unclosed.append(iv_name)

    return {
        'symbol': symbol,
        'current_price': current_price,
        'klines_data': klines_data,
        'unclosed_intervals': unclosed,
        'intervals': intervals,  # 记录请求的周期顺序，供 print 复用
        'kline_last': kline_count,  # 传递显示数量限制，print 环节能据此控输出
    }

def _format_klines_for_market(klines: List, interval: str, unclosed: bool = False, kline_last: int = 20) -> str:
    """Format klines for market command - no indicators, pure price action
    Args:
        klines: raw OHLCV data
        interval: time interval name
        unclosed: whether current candle is unclosed (for warning annotation)
        kline_last: 仅输出最近 N 根(默认 20, 遵循 SKILL.md --kline-last 参数)
    """
    lines = []
    if not klines:
        return ""
    unclosed_marker = " ⚠️UNCLOSED" if unclosed else ""
    total = len(klines)
    display_count = min(kline_last, total)
    lines.append(f"## {interval} ({display_count} of {total} bars){unclosed_marker}")
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    current = closes[-1]
    period_high = max(highs)
    period_low = min(lows)
    pct_from_high = ((current - period_high) / period_high * 100) if period_high > 0 else 0
    pct_from_low = ((current - period_low) / period_low * 100) if period_low > 0 else 0
    lines.append(f"Current: {current:.6f} | High: {period_high:.6f}({pct_from_high:+.1f}%) | Low: {period_low:.6f}({pct_from_low:+.1f}%)")
    lines.append("")
    for k in klines[-kline_last:]:
        ts = k[0] / 1000
        dt = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')
        o = float(k[1]); c = float(k[4]); h = float(k[2]); l = float(k[3]); v = float(k[5])
        chg = (c - o) / o * 100 if o > 0 else 0
        body = "▲" if c >= o else "▼"
        upper_shadow = h - max(o, c)
        lower_shadow = min(o, c) - l
        lines.append(f"  {dt} O:{o:.5f} C:{c:.5f} H:{h:.5f} L:{l:.5f} {body}{chg:+.2f}% U:{upper_shadow:.5f} L:{lower_shadow:.5f} V:{v:.0f}")
    lines.append("")
    return "\n".join(lines)


def print_market_data(data: Dict):
    """打印市场数据 - 无指标版（LLM分析用原始数据）"""
    print(f"\n{'='*60}")
    print(f"📊 {data['symbol']} 市场数据")
    print(f"{'='*60}")
    print(f"当前价格: {data['current_price']:.6f}")

    # 标注未闭合K线
    unclosed = data.get('unclosed_intervals', [])
    if unclosed:
        print(f"⚠️ 未闭合K线: {', '.join(unclosed)} (当前时间段尚未结束，分析时注意)")
    print()

    # 多周期K线（按请求的顺序输出，标注未闭合）
    _kline_last = data.get('kline_last', 20)
    for interval in data.get('intervals', ['30m', '1h']):
        kls = data['klines_data'].get(interval, [])
        if kls:
            is_unclosed = interval in unclosed
            unclosed_flag = " ⚠️未闭合" if is_unclosed else ""
            print(_format_klines_for_market(kls, interval, kline_last=_kline_last) + unclosed_flag)
    print()
    print("✅ 数据已输出，LLM请根据K线力度/成交量判断（禁止使用指标）")


# ========== Kronos 风格市场数据输出 (2026-06-05 P1 新增) ==========
def _classify_body(o: float, c: float) -> str:
    """Kronos 风格: 将 K 线实体离散化
    分类: cross(<0.1%) / tiny(<0.3%) / small(<1%) / mid(<3%) / big(>=3%)
    """
    if o == 0: return 'cross'
    pct = abs(c - o) / o * 100
    if pct < 0.1: return 'cross'    # 十字星
    if pct < 0.3: return 'tiny'     # 小实体
    if pct < 1.0: return 'small'    # 中实体
    if pct < 3.0: return 'mid'      # 大实体
    return 'big'                    # 超大实体

def _classify_shadow(o: float, c: float, h: float, l: float) -> str:
    """上/下影线占比: 上下影哪个更长
    返回: upper / lower / equal / none
    """
    body_high = max(o, c)
    body_low = min(o, c)
    upper = h - body_high
    lower = body_low - l
    body_size = body_high - body_low
    if body_size == 0: return 'none'
    if upper > body_size * 2 and upper > lower: return 'longupper'
    if lower > body_size * 2 and lower > upper: return 'longlower'
    if upper > lower * 1.5: return 'upper'
    if lower > upper * 1.5: return 'lower'
    return 'equal'

def _format_klines_kronos(klines: List, interval: str, unclosed: bool = False, kline_last: int = 20) -> str:
    """Kronos 风格: 量价联合离散化输出
    格式: ts │ body   shadow   vol  │ 走势
    目的: 让 LLM 看到的是"类别 token"而非连续数字,降低噪声
    Args:
        klines: raw OHLCV data
        interval: time interval name
        unclosed: whether current candle is unclosed
        kline_last: 仅输出最近 N 根(默认 20)
    """
    if not klines: return ""
    lines = []
    unclosed_marker = " ⚠️UNCLOSED" if unclosed else ""
    total = len(klines)
    display_count = min(kline_last, total)
    lines.append(f"## {interval} (Kronos 离散 token 格式, {display_count} of {total} bars){unclosed_marker}")
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    vols = [float(k[5]) for k in klines]
    current = closes[-1]
    period_high = max(highs); period_low = min(lows)
    # v6 (2026-06-08 20:23): 改为原始数据 — Kronos 论文 volume 是 optional, 不离散化
    # 原始 volume 区间: 最小 / 最大 / 总额, 不算 avg
    vol_min = min(vols) if vols else 0
    vol_max = max(vols) if vols else 0
    vol_sum = sum(vols) if vols else 0
    lines.append(f"Current: {current:.6f} | Range: {period_low:.5f} ~ {period_high:.5f} | Vol: min={vol_min:.0f} max={vol_max:.0f} sum={vol_sum:.0f}")
    lines.append("")
    lines.append(f"  {'时间':<13}│ {'实体':6s} {'影线':10s} {'原始量':>12s} │ O/C 价")
    lines.append(f"  {'-'*13}│ {'-'*6} {'-'*10} {'-'*12} │ {'-'*12}")
    # 取最近 N 根 (遵循 kline_last 参数)
    recent = klines[-kline_last:]
    for i, k in enumerate(recent):
        ts = k[0] / 1000
        dt = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')
        o = float(k[1]); c = float(k[4]); h = float(k[2]); l = float(k[3]); v = float(k[5])
        body = _classify_body(o, c)
        shadow = _classify_shadow(o, c, h, l)
        # vol 原始数字 (v6 修正: 不再离散化, 论文 Kronos volume 是 optional)
        vol_raw = f"{v:.0f}"
        direction = '▲' if c >= o else '▼'
        lines.append(f"  {dt:<13}│ {body:6s} {shadow:10s} {vol_raw:>12s} │ {o:.5f} → {c:.5f} {direction}")
    lines.append("")
    # 快速体诊断
    last_3 = recent[-3:]
    bodies = [_classify_body(float(k[1]), float(k[4])) for k in last_3]
    vols_raw3 = [f"{float(k[5]):.0f}" for k in last_3]
    lines.append(f"  最近 3 根 → 实体: {' → '.join(bodies)} | 原始量: {' → '.join(vols_raw3)}")
    lines.append("")
    return "\n".join(lines)


def print_market_data_kronos(data: Dict):
    """Kronos 风格市场数据输出 — 取代 print_market_data
    P1 (2026-06-05): 对齐 Kronos 论文 (arXiv:2508.02739, AAAI 2026) 投喂范式
    创新:
      1. OHLCV 联合离散化 (body/shadow/vol token)
      2. 三视角提示模板内置 (趋势派 / 量价派 / 结构派)
      3. 多尺度方向分 S 加权提示
      4. 反向视角强制 (lg.md 2.3)
    """
    print(f"\n{'='*70}")
    print(f"📊 {data['symbol']} 市场数据 (Kronos 投喂格式)")
    print(f"{'='*70}")
    print(f"当前价格: {data['current_price']:.6f}")

    unclosed = data.get('unclosed_intervals', [])
    if unclosed:
        print(f"⚠️ 未闭合K线: {', '.join(unclosed)} (当前时间段尚未结束，分析时注意)")

    # 多周期 Kronos 格式
    _kline_last = data.get('kline_last', 20)
    for interval in data.get('intervals', ['30m', '1h']):
        kls = data['klines_data'].get(interval, [])
        if kls:
            is_unclosed = interval in unclosed
            print(_format_klines_kronos(kls, interval, unclosed=is_unclosed, kline_last=_kline_last))

    # 三视角分析提示 (lg.md 第二章)
    print('─' * 70)
    print('📐 三视角分析模板 (lg.md 2.1) — LLM 强制按此框架分析:')
    print()
    print('  【趋势派视角】 — 关注: 高低点是否抬高/降低？动量是否增强？')
    print('    1d 看 40 根大方向 | 4h 看 90 根中期 | 1h/15m 看微观动量')
    print('    结论: 做多(+1) / 观望(0) / 做空(-1) — 选一个, 不允许模糊')
    print()
    print('  【量价派视角】 — 关注: 放量/缩量 vs 价格方向 (lg.md 3.1)')
    print('    四种状态:')
    print('      • 量价齐升 (xls/big + 价↑) → 顺势')
    print('      • 量价背离 (xxs/xs + 价↑) → 诱多, 警惕')
    print('      • 量价齐跌 (xls/big + 价↓) → 顺势做空')
    print('      • 量价背离 (xxs/xs + 价↓) → 可能见底, 警惕反转')
    print('    结论: 做多 / 观望 / 做空 + 关键证据 1-2 句')
    print()
    print('  【结构派视角】 — 关注: 关键支撑/压力 + 形态 + 量价背离')
    print('    距 period high/low 多少 %? 是否有 longupper/longlower 关键形态?')
    print('    结论: 做多 / 观望 / 做空 + 关键证据')
    print()

    # 多尺度集成提示 (lg.md 第二章 2.2)
    print('─' * 70)
    print('📐 多尺度集成 (lg.md 2.2) — LLM 强制按此规则集成:')
    print('    S(1d)=0.4 × dir_1d + S(4h)=0.3 × dir_4h + S(1h)=0.2 × dir_1h + S(15m)=0.1 × dir_15m')
    print('    跨尺度矛盾 (1d 多 + 4h 空) → 观望 (硬规则)')
    print('    三视角一致 + 跨尺度一致 → 高置信 (S > 0.7)')
    print('    两视角一致 + 跨尺度一致 → 中置信 (0.4 < S < 0.7)')
    print('    其他 → 观望')
    print()

    # 反向视角提示 (lg.md 2.3)
    print('─' * 70)
    print('🔄 反向视角 (lg.md 2.3) — 强制从反向列举 3 个失败理由:')
    print('    若结论做多 → 假设空头, 列举 3 个做多会失败的理由')
    print('    若结论做空 → 假设多头, 列举 3 个做空会失败的理由')
    print('    列举 ≥ 2 个强反驳 → 置信度降一档')
    print('─' * 70)
    print()
    print('✅ Kronos 风格数据已输出 — LLM 强请按上述三视角 + 多尺度 + 反向视角框架分析')
    print('   原始数字表请使用 print_market_data (旧格式) — 两者互补')


def get_status(symbol: str = None) -> Dict:
    """获取账户状态
    Bug #14 Fix: balance 获取失败时不归零,提示重试/缓存上次值
    """
    trader = BinanceTrader()

    # Bug #14 Fix: balance 失败时使用 缓存（如果 文件存在）或报明确错误
    balance = None
    try:
        balance = trader.get_usdt_balance()
    except Exception as e:
        # 不静默 返 0,先试读缓存
        cache_path = os.path.join(os.path.dirname(__file__), '.last_balance.json')
        cached = None
        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    cached = float(json.load(f).get('balance', 0))
            except Exception:
                pass
        if cached:
            print(f"⚠️ balance 获取失败,使用上次缓存: ${cached:.2f} ({e})", file=sys.stderr)
            balance = cached
        else:
            print(f"❌ balance 获取失败且无缓存: {e}", file=sys.stderr)
            return {'error': str(e), 'status': 'BALANCE_FETCH_FAILED'}
        # 不返回,后面会保存新缓存
    # 保存本次成功的 balance 到缓存
    if balance is not None:
        try:
            with open(os.path.join(os.path.dirname(__file__), '.last_balance.json'), 'w') as f:
                json.dump({'balance': balance, 'ts': time.time()}, f)
        except Exception:
            pass

    positions = trader.get_positions(symbol)

    # 计算未实现盈亏合计
    unrealized_total = sum(pos.get('unrealizedProfit', 0) for pos in positions)
    # 总资产 = 余额 + 浮盈
    total_assets = balance + unrealized_total

    result = {
        'balance': balance,
        'total_assets': total_assets,
        'unrealized_profit': unrealized_total,
        'positions': positions
    }

    print(f"\n{'='*60}")
    print(f"💰 账户状态")
    print(f"{'='*60}")
    print(f"总资产: ${total_assets:.2f}  (余额 ${balance:.2f} + 浮盈 ${unrealized_total:.2f})")
    print()

    if positions:
        for pos in positions:
            print(f"持仓:")
            print(f"  币种: {pos['symbol']}")
            print(f"  方向: {pos['positionSide']}")
            print(f"  数量: {pos['amount']}")
            print(f"  加权平均开仓价: {_fmt_price(pos['entryPrice'], pos['symbol'])}")
            pnl = pos.get('unrealizedProfit')
            if pnl is not None:
                print(f"  未实现盈亏: ${pnl:.2f}")
            else:
                print(f"  未实现盈亏: $0.00")
            print(f"  杠杆: {pos['leverage']}x")
            # Bug #28 Fix: 打印层数(超过 1 层表示有合持仓,LLM 应警惕)
            layers = pos.get('layers', 1)
            margin = pos.get('margin', 0)
            if layers > 1:
                print(f"  ⚠️ 合并层数: {layers}层 (总保证金={margin:.2f} USDT)")
            else:
                print(f"  保证金: {margin:.2f} USDT")
    else:
        print("持仓: 无")

    # 显示追踪的活跃条件单(.conditional_orders.json)
    # P0 Bug Fix 2026-06-09: 删掉"主动 cancel + 清理"逻辑
    # 根因: PM 账户 /papi/v1/um/conditional/openOrders 返回 404,无法被动查询
    #       旧代码用 cancel_conditional_order() 当"校验"=主动取消所有止损单
    #       导致 cron 跑几轮后所有止损单都被 status 自毁
    # 新行为: status 只展示,绝不调 cancel。要清理追踪记录只在:
    #   1) close-long/short 成功时 (do_close_long/short 里清理)
    #   2) 用户显式调 cancel-conditionals 时
    #   3) 仓位不存在时(本函数末尾检查)
    tracked = _load_conditional_orders()
    if tracked:
        print(f"\n📋 追踪的条件单 ({sum(len(v) for v in tracked.values())} 条):")
        for sym, sides in tracked.items():
            for side, algo_id in sides.items():
                print(f"  {sym} {side} algo_id={algo_id}")

    # ⚠️ P0 警告: 检查"无止损持仓"(lg.md §7 主流程 + cron 步骤 1 必查)
    # 扫所有持仓, 看 .conditional_orders.json 里有没有对应 symbol+平仓方向 的止损单
    # 追踪文件里 key 是 symbol → {SELL/BUY(平仓方向) → algo_id}
    # LONG 持仓的止损单 side=SELL, SHORT 持仓的止损单 side=BUY
    if positions and tracked:
        tracked_positions = set()
        for sym, sides in tracked.items():
            for side in sides:
                # 平仓方向: LONG → SELL, SHORT → BUY
                tracked_positions.add((sym, side))
        no_sl_positions = []
        for pos in positions:
            sym = pos['symbol']
            ps = pos['positionSide']  # 'LONG' or 'SHORT'
            close_side = 'SELL' if ps == 'LONG' else 'BUY'
            if (sym, close_side) not in tracked_positions:
                no_sl_positions.append((sym, ps))
        if no_sl_positions:
            print(f"\n⚠️⚠️⚠️ 以下持仓本地追踪不到止损单 (可能被 status/cancel 误删):")
            for sym, ps in no_sl_positions:
                close_side = 'SELL' if ps == 'LONG' else 'BUY'
                print(f"  🛑 {sym} {ps} - 立即 replace-order --side={close_side} 设止损!")
    # ⚠️ P0 警告: 检查"孤儿追踪"(追踪里有但实际持仓没有了)
    # 通常是 do_close_long 之前先 cancel 成功但仓位还没平完造成的
    if tracked and positions is not None:
        current_pos_syms = set(p['symbol'] for p in positions)
        orphan_tracks = [sym for sym in tracked.keys() if sym not in current_pos_syms]
        if orphan_tracks:
            print(f"\n⚠️ 孤儿追踪 (持仓已平但追踪还在):")
            for sym in orphan_tracks:
                print(f"  🔍 {sym} - 调 cancel-conditionals --symbol={sym} 清理")
    # 删掉 "反向清理: 追踪里有但持仓没有了 → 清理追踪" 逻辑
    # P0 Bug Fix 2026-06-09 14:56: INU 平仓后 status 反向清理把 INU SELL 追踪删了,
    # 但服务端 INU SELL 止损单还在 (algo 2000001089853828) — 状态不一致
    # 正确做法: 追踪记录只在 do_close_long/short 成功时主动清, 跟仓位生命周期绑定
    # status 永远不动追踪

    return result

def do_open_short(symbol: str, margin: float, leverage: int) -> Dict:
    """开空仓"""
    # lg.md 6.2.2 软提示 (不阻断, 供 LLM 参考)
    # 用户纠正 2026-06-06 22:41: lg.md 是策略逻辑不是硬编码, 亏本要改策略
    compliance = _check_lg_md_strict(symbol, 'SHORT')
    if not compliance.get('pass', True):
        print(f"⚠️ lg.md 6.2.2 软提示: {compliance['reason']}")
        print(f"   证据: {compliance.get('evidence', {})}")
        print(f"   ⚠️ 仍可开仓, 由 LLM 综合多视角+多尺度+反向视角自行决定")
    # P1 Fix (2026-06-05): IC 降级检查 (lg.md 11.1)
    state = load_weight_state()
    degradation = state.get('degradation', {})
    level = degradation.get('level', 0)
    if level >= 2:
        # L2/L3 不拒交易,只控仓位 (2026-06-07 任务边界:程序不负责策略,策略不拒交易)
        old_margin = margin
        if level == 2:
            margin = max(margin * 0.25, 1.0)  # L2: 1/4 仓位
        else:  # level == 3
            margin = max(margin * 0.1, 1.0)   # L3: 1/10 试探仓
        print(f"⚠️ 降级 L{level}: 不拒交易,仓位 {old_margin}→{margin} USDT ({'L2: 1/4' if level==2 else 'L3: 1/10'})")
        print(f"   原因: {degradation.get('reason', '')}")
        print(f"   解锁: 需方向 IC ≥ 40%, 当前 {state.get('ic_stats', {}).get('directional_ic', 0)*100:.1f}%")
        print(f"   📌 仍可开仓,胜率低是试错阶段,仓位小=风险可控")
    if level == 1:
        # 仓位减半
        margin = margin * 0.5
        print(f"⚠️ 降级 L1: 仓位减半, 调整后 margin={margin} USDT")
    if margin < 1:
        margin = 1.0
    trader = BinanceTrader()
    # Bug #23 Fix: 同 symbol 已有 SHORT 持仓时报错拒绝（lg.md 6.1 "同币种不重复开仓"）
    existing = trader.get_positions(symbol)
    if existing and any(p.get('positionSide') == 'SHORT' for p in existing):
        print(f"❌ {symbol} 已有 SHORT 持仓，加仓前请先 close。lg.md 6.1 禁止同币种重复开仓。")
        return {'status': 'REJECTED', 'reason': 'duplicate_short_position', 'symbol': symbol}
    price = trader.get_price(symbol)

    # 获取数量精度
    step_size = None
    try:
        r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/exchangeInfo", endpoint='exchangeInfo')
        for s in r.json().get('symbols', []):
            if s['symbol'] == symbol:
                for f in s.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        break
                break
    except Exception:
        pass

    # ===== Bug P2 Fix: 开仓前检查最小名义价值 =====
    # 计算名义价值
    quantity = (margin * leverage) / price
    if step_size and step_size > 0:
        quantity = round(round(quantity / step_size) * step_size, 8)
    else:
        quantity = round(quantity, 8)
    if quantity <= 0:
        quantity = 0.00000001

    notional = quantity * price
    # Binance 最小名义价值 > 5 USDT，严格大于
    if notional <= 5:
        if step_size and step_size > 0:
            quantity = round((5.0 / price) / step_size + 1) * step_size
        else:
            quantity = round(5.0 / price * 1.01, 8)
        quantity = max(quantity, 0.00000001)
        notional = quantity * price
        print(f"[AUTO] 名义价值 ${notional:.2f} <= 5，自动调整数量至 {quantity}", file=sys.stderr)

    print(f"\n{'='*60}")
    print(f"🔴 开空仓: {symbol}")
    print(f"{'='*60}")
    print(f"保证金: ${margin}")
    print(f"杠杆: {leverage}x")
    print(f"价格: ${price:.4f}")
    print(f"数量: {quantity}")
    print(f"名义价值: ${notional:.2f}")
    print()

    # ===== Bug P1 Fix: PM 账户跳过杠杆设置 =====
    if is_portfolio_margin():
        try:
            trader.set_leverage(symbol, leverage)
        except Exception as e:
            print(f"[WARN] 设置杠杆失败(继续开仓): {e}", file=sys.stderr)

    result = trader.open_short(symbol, quantity, leverage, margin=margin)
    print(f"订单结果: {json.dumps(result, indent=2)}")
    # Bug P1 Fix: 以 status 为准判断是否成功
    order_status = result.get('status', '')
    is_success = order_status in ('NEW', 'FILLED', 'PARTIALLY_FILLED') or result.get('orderId') or result.get('clientOrderId')
    if is_success:
        print(f"\n✅ 开空仓成功")
        # ===== ATR 止损 (v10 规则) =====
        import math
        qty_int = max(1, math.ceil(quantity))
        
        # 获取 ATR
        atr_data = _get_atr_and_volatility(symbol)
        sl_result = _calculate_stop_loss(
            price, 
            atr_data['atr'], 
            atr_data['atr_percent'], 
            'SHORT'
        )
        sl_trigger = _round_to_tick(sl_result['sl_trigger'], symbol)
        
        print(f"  📊 ATR {atr_data['atr_percent']:.2f}% → 波动率: {atr_data['volatility']}")
        print(f"  🔒 止损 @ ${sl_trigger:.{_get_price_decimals(symbol)}f} ({sl_result['sl_percent']:.2f}%)")
        
        # P0 Fix (2026-06-05): 止损失败不再静默
        # 根因: 实盘 PORTAL/HEI/BASED 多次 -5% 以上巨亏, 止损从未生效
        # 新行为: 3 次重试 → 全部失败 → 明确报错 + 建议 LLM 立即平仓
        sl_placed = False
        last_err = None
        for attempt in range(3):
            try:
                sl_resp = trader.set_stop_loss(symbol, 'BUY', qty_int, sl_trigger)
                if sl_resp and sl_resp.get('algoId'):
                    print(f"  ✅ 止损单已设置 algo_id={sl_resp.get('algoId')}")
                    sl_placed = True
                    break
                last_err = Exception(f"set_stop_loss 返回无 algoId: {sl_resp}")
            except Exception as e:
                last_err = e
            if attempt < 2:
                wait = 1 * (attempt + 1)
                print(f"  ⚠ 止损设置失败(重试 {attempt+1}/3): {last_err}, 等待 {wait}s")
                time.sleep(wait)
        if not sl_placed:
            print(f"\n{'!'*60}")
            print(f"❌ 止损连续 3 次设置失败: {last_err}")
            print(f"⚠️ 持仓 {symbol} 空仓 {qty_int} @ entry={price} 当前无止损保护!")
            print(f"🛑 强烈建议: 立即 close-short {symbol} 全部仓位 (避免再次 -12% 巨亏)")
            print(f"{'!'*60}")
            return {'status': 'SL_FAILED', 'symbol': symbol, 'error': str(last_err),
                    'open_result': result, 'suggested_action': f'close-short {symbol}'}
    else:
        print(f"\n❌ 开空仓失败: {result}")
    return result

def _is_early_close_allowed(symbol: str, side: str) -> bool:
    """判断是否允许“剥头皮平仓” (持仓<5min)
    返回 True 如果: 1) 持仓 >= 5min 或 2) 浮盈 |ret| >= 2% 主动止盈

    P0 Fix 2026-06-06: 30 天实盘 20 笔 <5min 胜率 15% 总亏 -1.45 USDT, 强制禁止
    """
    return _check_early_close_inline(symbol, side)


def _check_early_close_inline(symbol: str, side: str) -> bool:
    """检查是否可提前平仓 (内联实现避免 import 循环)
    P0 Bug Fix 2026-06-09: 之前依赖 p.get('updateTime') 但 get_positions() 返回的 dict 不带该字段 → 全部 0 → 跳到 return False 拒绝
    新实现: 直接调 papi /positionRisk 拿带 updateTime 的原始数据
    """
    try:
        trader = BinanceTrader()
        # P0 Fix: 直接调 papi 拿原始 position (带 updateTime)
        try:
            raw = trader._papi_request('GET', '/papi/v1/um/positionRisk', {'symbol': symbol})
        except Exception as e:
            # 非 PM 账户则走 fapi
            print(f'  [WARN] papi positionRisk 失败, 走 fapi: {e}')
            raw = trader._fapi_request('GET', '/fapi/v2/positionRisk', {'symbol': symbol})
        if not isinstance(raw, list):
            raw = raw.get('positions', []) if isinstance(raw, dict) else []
        from datetime import datetime
        for p in raw:
            amt = float(p.get('positionAmt', 0))
            if side == 'LONG' and amt > 0:
                entry = float(p.get('entryPrice', 0))
                current = trader.get_price(symbol)
                if entry <= 0 or current <= 0:
                    return False
                ret_pct = (current - entry) / entry * 100
                # 检查持仓时间 — p['updateTime'] 是 PM/fapi positionRisk 原始字段
                update_time = int(p.get('updateTime', 0)) / 1000  # ms -> s
                if update_time > 0:
                    hold_min = (time.time() - update_time) / 60
                    if hold_min >= 5:
                        return True
                # 浮盈 >= 2% 主动止盈
                if ret_pct >= 2.0:
                    return True
                # 默认拒绝
                return False
            elif side == 'SHORT' and amt < 0:
                entry = float(p.get('entryPrice', 0))
                current = trader.get_price(symbol)
                if entry <= 0 or current <= 0:
                    return False
                ret_pct = (entry - current) / entry * 100
                update_time = int(p.get('updateTime', 0)) / 1000
                if update_time > 0:
                    hold_min = (time.time() - update_time) / 60
                    if hold_min >= 5:
                        return True
                if ret_pct >= 2.0:
                    return True
                return False
    except Exception as e:
        print(f'[WARN] _check_early_close_inline 异常: {e}')
        return True  # 异常时默认允许 (避免锁住)
    return True


    """平空仓 (支持部分平仓)"""
    # P0 Fix (2026-06-06): <5min 强制不平仓 (数据驱动, 30天20笔<5min剥头皮 15%胜率 必亏)
    if not force_close and percent >= 100 and not _is_early_close_allowed(symbol, 'SHORT'):
        print(f"❌ 拒绝平空仓 {symbol}: 持仓<5min, 剥头皮必亏(30天 20 笔 15% 胜率)")
        print(f"   解锁: 持仓 5min 以上, 或 |ret| >= 2% 主动止盈")
        return {'status': 'REJECTED', 'reason': 'min_hold_5min', 'symbol': symbol}
    trader = BinanceTrader()

    print(f"\n{'='*60}")
    print(f"🔚 平空仓: {symbol} ({percent}%)")
    print(f"{'='*60}")

    # Bug Fix: 无空仓时优雅退出，不发空 API 请求
    if SIMULATE:
        _load_sim_state()
        stacks = _sim_positions.get(symbol, [])
        short_stacks = [s for s in stacks if s.get('side') == 'SHORT']
        if not short_stacks:
            print(f"❌ 无 {symbol} 空仓，跳过平仓")
            return {'status': 'NO_POSITION', 'symbol': symbol, 'side': 'SHORT'}
    else:
        positions = trader.get_positions(symbol)
        short_pos = [p for p in positions if p.get('positionSide') == 'SHORT' or (p.get('amount', 0) < 0)]
        if not short_pos:
            print(f"❌ 无 {symbol} 空仓，跳过平仓")
            return {'status': 'NO_POSITION', 'symbol': symbol}

    # 支持部分平仓
    if percent < 100:
        positions = trader.get_positions(symbol)
        for pos in positions:
            if pos.get('positionSide') == 'SHORT' or (pos['amount'] < 0):
                total_qty = abs(pos['amount'])
                close_qty = total_qty * (percent / 100)
                print(f"部分平仓: {percent}% = {close_qty:.4f} / {total_qty:.4f} (全仓)")
                result = trader.close_position(symbol, close_qty)
                print(f"订单结果: {json.dumps(result, indent=2)}")
                if result.get('orderId') or result.get('clientOrderId') or result.get('symbol'):
                    print(f"\n✅ 部分平空仓成功({percent}%)")
                else:
                    print(f"\n❌ 平仓失败: {result}")
                return result
        raise Exception(f"No short position found for {symbol}")
    else:
        result = trader.close_position(symbol)
        print(f"订单结果: {json.dumps(result, indent=2)}")
        if result.get('orderId') or result.get('clientOrderId') or result.get('symbol'):
            print(f"\n✅ 平空仓成功")
        else:
            print(f"\n❌ 平仓失败: {result}")
        return result

def do_open_long(symbol: str, margin: float, leverage: int) -> Dict:
    """开多仓"""
    # lg.md 6.2.1 软提示 (不阻断, 供 LLM 参考)
    # 用户纠正 2026-06-06 22:41: "lg.md 讲的是策略逻辑判断, 不要对买卖有硬编码,
    # 亏本就要对策略进行检查, 而不是硬编码不让其交易"
    compliance = _check_lg_md_strict(symbol, 'LONG')
    if not compliance.get('pass', True):
        print(f"⚠️ lg.md 6.2.1 软提示: {compliance['reason']}")
        print(f"   证据: {compliance.get('evidence', {})}")
        print(f"   ⚠️ 仍可开仓, 由 LLM 综合多视角+多尺度+反向视角自行决定")
    # P1 Fix (2026-06-05): IC 降级检查 (lg.md 11.1)
    state = load_weight_state()
    degradation = state.get('degradation', {})
    level = degradation.get('level', 0)
    if level >= 2:
        # L2/L3 不拒交易,只控仓位 (2026-06-07 任务边界:程序不负责策略,策略不拒交易)
        old_margin = margin
        if level == 2:
            margin = max(margin * 0.25, 1.0)  # L2: 1/4 仓位
        else:  # level == 3
            margin = max(margin * 0.1, 1.0)   # L3: 1/10 试探仓
        print(f"⚠️ 降级 L{level}: 不拒交易,仓位 {old_margin}→{margin} USDT ({'L2: 1/4' if level==2 else 'L3: 1/10'})")
        print(f"   原因: {degradation.get('reason', '')}")
        print(f"   解锁: 需方向 IC ≥ 40%, 当前 {state.get('ic_stats', {}).get('directional_ic', 0)*100:.1f}%")
        print(f"   📌 仍可开仓,胜率低是试错阶段,仓位小=风险可控")
    if level == 1:
        # 仓位减半
        margin = margin * 0.5
        print(f"⚠️ 降级 L1: 仓位减半, 调整后 margin={margin} USDT")
    if margin < 1:
        margin = 1.0
    trader = BinanceTrader()
    # Bug #23 Fix: 同 symbol 已有 LONG 持仓时报错拒绝（lg.md 6.1 "同币种不重复开仓"）
    existing = trader.get_positions(symbol)
    if existing and any(p.get('positionSide') == 'LONG' for p in existing):
        print(f"❌ {symbol} 已有 LONG 持仓，加仓前请先 close。lg.md 6.1 禁止同币种重复开仓。")
        return {'status': 'REJECTED', 'reason': 'duplicate_long_position', 'symbol': symbol}
    price = trader.get_price(symbol)

    step_size = None
    try:
        r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/exchangeInfo", endpoint='exchangeInfo')
        for s in r.json().get('symbols', []):
            if s['symbol'] == symbol:
                for f in s.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        break
                break
    except Exception:
        pass

    quantity = (margin * leverage) / price
    if step_size and step_size > 0:
        quantity = round(round(quantity / step_size) * step_size, 8)
    else:
        quantity = round(quantity, 8)
    if quantity <= 0:
        quantity = 0.00000001

    # ===== Bug P2 Fix: 自动调整数量满足 Binance 最小名义价值 > 5 =====
    notional = quantity * price
    if notional <= 5:
        if step_size and step_size > 0:
            quantity = round((5.0 / price) / step_size + 1) * step_size
        else:
            quantity = round(5.0 / price * 1.01, 8)
        quantity = max(quantity, 0.00000001)
        notional = quantity * price
        print(f'[AUTO] 名义价值 ${notional:.2f} <= 5，自动调整数量至 {quantity}', file=sys.stderr)


    print(f"\n{'='*60}")
    print(f"\xf0\x9f\x9f\xa2 \xe5\xbc\x80\xe5\xa4\x9a\xe4\xbb\x93: {symbol}")
    print(f"{'='*60}")
    print(f"\xe4\xbf\x9d\xe8\xaf\x81\xe9\x87\x91: ${margin}")
    print(f"\xe6\x9d\x83\xe6\x9d\x83: {leverage}x")
    print(f"\xe4\xbb\xb7\xe6\xa0\xbc: ${price:.4f}")
    print(f"\xe6\x95\xb0\xe9\x87\x8f: {quantity}")
    print(f"\xe5\x90\x8d\xe4\xb9\x89\xe4\xbb\xa3\xe5\x80\xbc: ${notional:.2f}")
    print()

    # ===== Bug P4 Fix: PM \xe8\xb4\xa6\xe6\x88\xb7\xe8\xb7\xb3\xe8\xbf\x87\xe6\x9d\x83\xe6\x9d\x83\xe8\xae\xbe\xe7\xbd\xae =====
    if is_portfolio_margin():
        try:
            trader.set_leverage(symbol, leverage)
        except Exception as e:
            print(f"[WARN] \xe8\xae\xbe\xe7\xbd\xae\xe6\x9d\x83\xe6\x9d\x83\xe5\xa4\xb1\xe8\xb5\xa5(\xe7\xbb\xa7\xe7\xbb\xad\xe5\xbc\x80\xe4\xbb\x93): {e}", file=sys.stderr)
    result = trader.open_long(symbol, quantity, leverage, margin=margin)
    print(f"\xe8\xae\xa2\xe5\x8d\x95\xe7\xbb\x93\xe6\x9e\x9c: {json.dumps(result, indent=2)}")
    # Bug P1 Fix: \xe4\xbb\xa5 status \xe4\xb8\xba\xe5\x87\x86\xe5\x88\xa4\xe6\x96\xad\xe6\x98\xaf\xe5\x90\xa6\xe6\x88\x90\xe5\x8a\x9f
    order_status = result.get('status', '')
    is_success = order_status in ('NEW', 'FILLED', 'PARTIALLY_FILLED') or result.get('orderId') or result.get('clientOrderId')
    if is_success:
        print(f"\n\xe2\x9c\x85 \xe5\xbc\x80\xe5\xa4\x9a\xe4\xbb\x93\xe6\x88\x90\xe5\x8a\x9f")
        # ===== ATR \xe6\xad\xa2\xe6\x8d\x9f + \xe6\xad\xa2\xe7\x9b\x88 (lg.md \xe8\xa7\x84\u5219) =====
        import math
        qty_int = max(1, math.ceil(quantity))
        
        # \xe8\x8e\xb7\xe5\x8f\x96 ATR
        atr_data = _get_atr_and_volatility(symbol)
        sl_result = _calculate_stop_loss(
            price, 
            atr_data["atr"], 
            atr_data["atr_percent"], 
            "LONG"
        )
        sl_trigger = _round_to_tick(sl_result["sl_trigger"], symbol)
        
        print(f"  📊 ATR {atr_data["atr_percent"]:.2f}% → �΋娧率: {atr_data["volatility"]}")
        print(f"  🔒 止损 @ ${sl_trigger:.{_get_price_decimals(symbol)}f} ({sl_result["sl_percent"]:.2f}%)")
        
        # P0 Fix (2026-06-05): 止损失败不再静默
        # 根因: 实盘 STG 6/1 6:54 开多, 8:41 -12% 巨亏, 止损从未生效
        # 新行为: 3 次重试 → 全部失败 → 明确报错 + 建议 LLM 立即平仓
        sl_placed = False
        last_err = None
        for attempt in range(3):
            try:
                sl_resp = trader.set_stop_loss(symbol, 'SELL', qty_int, sl_trigger)
                if sl_resp and sl_resp.get('algoId'):
                    print(f"  ✅ 止损单已设置 algo_id={sl_resp.get('algoId')}")
                    sl_placed = True
                    break
                last_err = Exception(f"set_stop_loss 返回无 algoId: {sl_resp}")
            except Exception as e:
                last_err = e
            if attempt < 2:
                wait = 1 * (attempt + 1)
                print(f"  ⚠ 止损设置失败(重试 {attempt+1}/3): {last_err}, 等待 {wait}s")
                time.sleep(wait)
        if not sl_placed:
            print(f"\n{'!'*60}")
            print(f"❌ 止损连续 3 次设置失败: {last_err}")
            print(f"⚠️ 持仓 {symbol} 多仓 {qty_int} @ entry={price} 当前无止损保护!")
            print(f"🛑 强烈建议: 立即 close-long {symbol} 全部仓位 (避免再次 -12% 巨亏)")
            print(f"{'!'*60}")
            return {'status': 'SL_FAILED', 'symbol': symbol, 'error': str(last_err),
                    'open_result': result, 'suggested_action': f'close-long {symbol}'}
    else:
        print(f"\n❌ 开多仓失败: {result}")
    return result

def do_close_long(symbol: str, percent: float = 100, force_close: bool = False) -> Dict:
    """平多仓 (支持部分平仓)"""
    # P0 Fix (2026-06-06): <5min 强制不平仓 (数据驱动, 30天20笔<5min剥头皮 15%胜率 必亏)
    # 解锁: 1) 止损触发 2) |ret| >= 2% 主动止盈
    # 例外: force_close=True 参数可绕过
    if not force_close and percent >= 100 and not _is_early_close_allowed(symbol, 'LONG'):
        print(f"❌ 拒绝平多仓 {symbol}: 持仓<5min, 剥头皮必亏(30天 20 笔 15% 胜率)")
        print(f"   解锁: 持仓 5min 以上, 或 |ret| >= 2% 主动止盈")
        return {'status': 'REJECTED', 'reason': 'min_hold_5min', 'symbol': symbol}
    trader = BinanceTrader()

    print(f"\n{'='*60}")
    print(f"🔚 平多仓: {symbol} ({percent}%)")
    print(f"{'='*60}")

    # Bug Fix: 无多仓时优雅退出，不发空 API 请求
    if SIMULATE:
        _load_sim_state()
        stacks = _sim_positions.get(symbol, [])
        long_stacks = [s for s in stacks if s.get('side') == 'LONG']
        if not long_stacks:
            print(f"❌ 无 {symbol} 多仓，跳过平仓")
            return {'status': 'NO_POSITION', 'symbol': symbol, 'side': 'LONG'}
    else:
        positions = trader.get_positions(symbol)
        long_pos = [p for p in positions if p.get('positionSide') == 'LONG' or (p.get('amount', 0) > 0)]
        if not long_pos:
            print(f"❌ 无 {symbol} 多仓，跳过平仓")
            return {'status': 'NO_POSITION', 'symbol': symbol}

    # 支持部分平仓
    if percent < 100:
        positions = trader.get_positions(symbol)
        for pos in positions:
            if pos.get('positionSide') == 'LONG' or (pos['amount'] > 0):
                total_qty = abs(pos['amount'])
                close_qty = total_qty * (percent / 100)
                print(f"部分平仓: {percent}% = {close_qty:.4f} / {total_qty:.4f} (全仓)")
                result = trader.close_position(symbol, close_qty)
                print(f"订单结果: {json.dumps(result, indent=2)}")
                if result.get('orderId') or result.get('clientOrderId') or result.get('symbol'):
                    print(f"\n✅ 部分平多仓成功({percent}%)")
                else:
                    print(f"\n❌ 平仓失败: {result}")
                return result
        raise Exception(f"No long position found for {symbol}")
    else:
        result = trader.close_position(symbol)
        print(f"订单结果: {json.dumps(result, indent=2)}")
        if result.get('orderId') or result.get('clientOrderId') or result.get('symbol'):
            print(f"\n✅ 平多仓成功")
        else:
            print(f"\n❌ 平仓失败: {result}")
        return result


def do_close_short(symbol: str, percent: float = 100, force_close: bool = False) -> Dict:
    """平空仓 (支持部分平仓)
    P0 Bug Fix 2026-06-09 23:42: 之前 C 阶段删 check_take_profit 时误删 do_close_short 整段定义
    → main() 调 close-short 报 `name 'do_close_short' is not defined`
    修复: 重写完整平空逻辑, 结构对照 do_close_long
    """
    # P0 Fix (2026-06-06): <5min 强制不平仓 (数据驱动, 30天20笔<5min剥头皮 15%胜率 必亏)
    if not force_close and percent >= 100 and not _is_early_close_allowed(symbol, 'SHORT'):
        print(f"❌ 拒绝平空仓 {symbol}: 持仓<5min, 剥头皮必亏(30天 20 笔 15% 胜率)")
        print(f"   解锁: 持仓 5min 以上, 或 |ret| >= 2% 主动止盈")
        return {'status': 'REJECTED', 'reason': 'min_hold_5min', 'symbol': symbol}
    trader = BinanceTrader()

    print(f"\n{'='*60}")
    print(f"🔚 平空仓: {symbol} ({percent}%)")
    print(f"{'='*60}")

    # Bug Fix: 无空仓时优雅退出，不发空 API 请求
    if SIMULATE:
        _load_sim_state()
        stacks = _sim_positions.get(symbol, [])
        short_stacks = [s for s in stacks if s.get('side') == 'SHORT']
        if not short_stacks:
            print(f"❌ 无 {symbol} 空仓，跳过平仓")
            return {'status': 'NO_POSITION', 'symbol': symbol, 'side': 'SHORT'}
    else:
        positions = trader.get_positions(symbol)
        short_pos = [p for p in positions if p.get('positionSide') == 'SHORT' or (p.get('amount', 0) < 0)]
        if not short_pos:
            print(f"❌ 无 {symbol} 空仓，跳过平仓")
            return {'status': 'NO_POSITION', 'symbol': symbol}

    # 支持部分平仓
    if percent < 100:
        positions = trader.get_positions(symbol)
        for pos in positions:
            if pos.get('positionSide') == 'SHORT' or (pos['amount'] < 0):
                total_qty = abs(pos['amount'])
                close_qty = total_qty * (percent / 100)
                print(f"部分平仓: {percent}% = {close_qty:.4f} / {total_qty:.4f} (全仓)")
                result = trader.close_position(symbol, close_qty)
                print(f"订单结果: {json.dumps(result, indent=2)}")
                if result.get('orderId') or result.get('clientOrderId') or result.get('symbol'):
                    print(f"\n✅ 部分平空仓成功({percent}%)")
                else:
                    print(f"\n❌ 平仓失败: {result}")
                return result
        raise Exception(f"No short position found for {symbol}")
    else:
        result = trader.close_position(symbol)
        print(f"订单结果: {json.dumps(result, indent=2)}")
        if result.get('orderId') or result.get('clientOrderId') or result.get('symbol'):
            print(f"\n✅ 平空仓成功")
        else:
            print(f"\n❌ 平仓失败: {result}")
        return result


# ========== 阶梯巡检（cron 调）==========
def check_ladder() -> List[Dict]:
    """扫所有持仓 + 算浮盈 + 算阶梯档 + 给出 replace-order 建议

    v6 (2026-06-09 16:48): 阶梯"只上不下" - 触发价以历史 peak trigger 为准
    - LONG: 触发价 = max(历史 peak, current × 0.98) — 只往上调
    - SHORT: 触发价 = min(历史 peak, current × 1.02) — 只往下调
    - 浮盈表位: 2% 保本 / 3% 锁 1 / 4% 锁 2 / 5% 锁 3 / 6%+ 1:1 延伸
    - 浮盈表位只用于"心理锁档"提示 (LLM 看到 "锁 3%" 知道已锁 3% 利润)
    - 实际 trigger 走"只上不下"逻辑, 保本档 trigger=entry
    - peak 持久化到 .ladder_peak.json, 跨 cron 进程依然有效
    返回: [{'symbol', 'side', 'entry_price', 'current_price', 'upnl_pct', 'step_label', 'suggested_trigger', 'action'}]
        action: 'MOVE_SL' (建议调 replace-order) / 'HOLD' (未到阶梯档或已锁住不返档,不动)
    """
    trader = BinanceTrader()
    positions = trader.get_positions()
    peaks = _load_ladder_peaks()  # {"SYM": {"LONG": peak_trigger, "SHORT": peak_trigger}}
    if not positions:
        print(f"\n{'='*60}")
        print(f"🪜 阶梯巡检报告")
        print(f"{'='*60}")
        print("无持仓，无需巡检阶梯")
        return []

    print(f"\n{'='*60}")
    print(f"🪜 阶梯巡检报告 ({len(positions)} 个持仓)")
    print(f"{'='*60}")

    results = []
    updated = False
    for pos in positions:
        symbol = pos.get('symbol')
        side = pos.get('positionSide')  # 'LONG' or 'SHORT'
        entry = float(pos.get('entryPrice', 0))
        amount = float(pos.get('amount', 0))
        if not symbol or not side or entry <= 0:
            continue
        # 拿当前价
        try:
            current = trader.get_price(symbol)
        except Exception as e:
            print(f"  ⚠️ {symbol} 取价失败: {e}")
            continue
        # 算浮盈 %
        if side == 'LONG':
            upnl_pct = (current - entry) / entry * 100
            sl_side = 'SELL'
        else:  # SHORT
            upnl_pct = (entry - current) / entry * 100
            sl_side = 'BUY'
        # 阶梯表位计算(心理锁档)
        step_pct = 0.0
        step_label = '未到阶梯'
        if upnl_pct >= 6.0:
            step_pct = upnl_pct - 2.0  # 6→4, 7→5, 8→6, 9→7, 10→8 ...
            step_label = f'锁 {step_pct:.0f}%'
        elif upnl_pct >= 5.0:
            step_pct = 3.0
            step_label = '锁 3%'
        elif upnl_pct >= 4.0:
            step_pct = 2.0
            step_label = '锁 2%'
        elif upnl_pct >= 3.0:
            step_pct = 1.0
            step_label = '锁 1%'
        elif upnl_pct >= 2.0:
            step_label = '保本(锁 0%)'

        # v6: 阶梯"只上不下" - target trigger vs 历史 peak trigger
        # 浮盈 < 2% 不调止损, 沿用开仓 3% 紧
        action = 'HOLD'
        suggested_trigger = None
        peak_note = ''

        if upnl_pct < 2.0:
            # 未到保本档, 不动
            pass
        else:
            # 算"本轮 target trigger"
            if upnl_pct >= 3.0:
                # 锁档位: target = current × 0.98 (LONG) / × 1.02 (SHORT)
                target = current * 0.98 if side == 'LONG' else current * 1.02
            else:
                # 保本档: target = entry
                target = entry

            # 跟历史 peak trigger 比对 - "只上不下"
            sym_peaks = peaks.get(symbol, {})
            hist_peak = sym_peaks.get(side)
            if side == 'LONG':
                # 多仓: trigger 越高越好(锁档高 = 保护多), 只在 target > hist_peak 时调
                if hist_peak is None or target > hist_peak:
                    suggested_trigger = target
                    action = 'MOVE_SL'
                    if hist_peak is not None:
                        peak_note = f'(历史锁={hist_peak:.6f} → 本轮={target:.6f}, 上调)'
                    # 更新 peak (本次可能成为新高)
                    if symbol not in peaks:
                        peaks[symbol] = {}
                    peaks[symbol][side] = target
                    updated = True
                else:
                    # target < hist_peak, 锁住不返档
                    suggested_trigger = None
                    action = 'HOLD'
                    peak_note = f'(已锁={hist_peak:.6f} >= 本轮={target:.6f}, 锁住不返档)'
            else:  # SHORT
                # 空仓: trigger 越低越好(锁档低 = 保护多), 只在 target < hist_peak 时调
                if hist_peak is None or target < hist_peak:
                    suggested_trigger = target
                    action = 'MOVE_SL'
                    if hist_peak is not None:
                        peak_note = f'(历史锁={hist_peak:.6f} → 本轮={target:.6f}, 下调)'
                    if symbol not in peaks:
                        peaks[symbol] = {}
                    peaks[symbol][side] = target
                    updated = True
                else:
                    suggested_trigger = None
                    action = 'HOLD'
                    peak_note = f'(已锁={hist_peak:.6f} <= 本轮={target:.6f}, 锁住不返档)'

        # 输出
        rec = {
            'symbol': symbol,
            'side': side,
            'entry_price': entry,
            'current_price': current,
            'upnl_pct': round(upnl_pct, 2),
            'step_label': step_label,
            'suggested_trigger': round(suggested_trigger, 8) if suggested_trigger else None,
            'action': action,
        }
        results.append(rec)

        # 图标
        if action == 'MOVE_SL':
            icon = '🆕' if upnl_pct >= 3.0 else '🔔'  # 3% 锁档或更高 vs 保本档
        elif upnl_pct >= 2.0 and peaks.get(symbol, {}).get(side) is not None:
            icon = '🔒'  # 已锁档, 本轮不动
        else:
            icon = '⏸️'
        print(f"  {icon} {symbol} {side}: 入场={entry:.4f} 现价={current:.4f} 浮盈={upnl_pct:+.2f}% → {step_label} {peak_note}", end='')
        if action == 'MOVE_SL':
            print(f" → 建议 replace-order --symbol={symbol} --side={sl_side} --trigger={suggested_trigger}")
        else:
            print(f" → 不动")

    # 总结 + 持久化 peak
    move_count = sum(1 for r in results if r['action'] == 'MOVE_SL')
    print(f"\n总结: {move_count}/{len(results)} 持仓需调 replace-order (阶梯只上不下)")
    if updated:
        _save_ladder_peaks(peaks)
    return results


def analyze_position(symbol: str) -> Dict:
    """持仓 §14 4 问脚本化分析(lg.md §14)

    自动算 4 问:
    1. 趋势是否走完? 1d/4h 高点是否不再抬高? 1h/15m 是否跌破支撑?
    2. 动能是否衰退? 阳线 C-O 缩窄 / 阴线 C-O 变长 / 影线变长
    3. 量价是否配合? 价高量低 / 价低量高 / 量价背离
    4. 反向支撑是否强? 列举 3 个"为什么不平"的理由(LLM 决,程序只能算前 3 问)

    返回: {
        'symbol', 'side', 'upnl_pct', 'current_price',
        'q1_trend': {signal: '走完'|'持续'|'不明', evidence: '...'},
        'q2_momentum': {signal: '衰退'|'正常'|'不明', evidence: '...'},
        'q3_volume_price': {signal: '背离'|'配合'|'不明', evidence: '...'},
        'verdict': '主动平仓'|'锁档'|'持有'|'止损等待',
    }
    """
    trader = BinanceTrader()
    # 拿持仓 + 当前价
    pos_list = [p for p in trader.get_positions() if p.get('symbol') == symbol]
    if not pos_list:
        print(f"❌ {symbol} 无持仓")
        return {'error': 'no_position'}
    pos = pos_list[0]
    side = pos['positionSide']  # LONG/SHORT
    entry = float(pos['entryPrice'])
    amount = float(pos['amount'])
    current = trader.get_price(symbol)
    if side == 'LONG':
        upnl_pct = (current - entry) / entry * 100
    else:
        upnl_pct = (entry - current) / entry * 100

    # 拉 4 尺度 K 线 (1d/4h/1h/15m)
    klines = {}
    for iv in ('1d', '4h', '1h', '15m'):
        try:
            klines[iv] = _get_klines_raw(symbol, iv, 20)
        except Exception as e:
            klines[iv] = []

    def c_o(k):
        o, c = float(k[1]), float(k[4])
        return (c - o) / o * 100 if o > 0 else 0  # (close-open)/open %

    def body(k):
        return abs(float(k[4]) - float(k[1]))

    def upper_shadow(k):
        return float(k[2]) - max(float(k[1]), float(k[4]))

    def lower_shadow(k):
        return min(float(k[1]), float(k[4])) - float(k[3])

    # Q1 趋势走完
    q1 = {'signal': '不明', 'evidence': ''}
    if klines.get('1d') and len(klines['1d']) >= 5:
        d1 = klines['1d']
        highs = [float(k[2]) for k in d1[-5:]]
        if side == 'LONG':
            # 多仓: 高点应抬高
            if highs[-1] < max(highs[:-1]):
                q1 = {'signal': '走完', 'evidence': f'1d 近 5 根高点不抬高 (现={highs[-1]:.4f}, 近期 max={max(highs[:-1]):.4f})'}
            else:
                q1 = {'signal': '持续', 'evidence': f'1d 近 5 根高点抬升 (现高点={highs[-1]:.4f})'}
        else:
            # 空仓: 低点应走低
            lows = [float(k[3]) for k in d1[-5:]]
            if lows[-1] > min(lows[:-1]):
                q1 = {'signal': '走完', 'evidence': f'1d 近 5 根低点不抬低 (现={lows[-1]:.4f}, 近期 min={min(lows[:-1]):.4f})'}
            else:
                q1 = {'signal': '持续', 'evidence': f'1d 近 5 根低点下降 (现低点={lows[-1]:.4f})'}

    # Q2 动能衰退 - 1d/4h 连续 3 根 C-O 缩窄 + 影线变长
    q2 = {'signal': '不明', 'evidence': ''}
    for iv in ('1d', '4h'):
        if klines.get(iv) and len(klines[iv]) >= 5:
            ks = klines[iv][-5:]
            bodies = [body(k) for k in ks]
            # 近 3 根 C-O 绝对值 < 前 2 根 (动能缩窄)
            recent = sum(abs(c_o(k)) for k in ks[-3:])
            earlier = sum(abs(c_o(k)) for k in ks[:2])
            if recent < earlier * 0.5 and earlier > 0:
                # 再看影线变长
                avg_shadow = sum(upper_shadow(k) + lower_shadow(k) for k in ks[-3:]) / 3
                avg_body = sum(bodies[-3:]) / 3
                if avg_body > 0 and avg_shadow / avg_body > 0.8:
                    q2 = {'signal': '衰退', 'evidence': f'{iv} 近 3 根 C-O 缩窄 ({recent:.2f}% < 前 2 根 {earlier:.2f}%) + 影线/实体={avg_shadow/avg_body:.2f}'}
                    break
            if q2['signal'] == '不明':
                q2 = {'signal': '正常', 'evidence': f'{iv} C-O 动能未明显缩窄 (近 3={recent:.2f}% vs 前 2={earlier:.2f}%)'}
                break

    # Q3 量价背离 - 1d 价创高 + vol 不创高 (LONG) / 价创新低 + vol 不创新低 (SHORT)
    q3 = {'signal': '不明', 'evidence': ''}
    if klines.get('1d') and len(klines['1d']) >= 10:
        d1 = klines['1d'][-10:]
        # 划分前 5 根 vs 后 5 根
        front = d1[:5]
        back = d1[5:]
        if side == 'LONG':
            front_high = max(float(k[2]) for k in front)
            back_high = max(float(k[2]) for k in back)
            front_vol = sum(float(k[5]) for k in front)
            back_vol = sum(float(k[5]) for k in back)
            if back_high > front_high and back_vol < front_vol * 0.7:
                q3 = {'signal': '背离', 'evidence': f'1d 后 5 高点={back_high:.4f} > 前 5 高点={front_high:.4f} (创新高) 但 vol={back_vol:.0f} < 前 5 vol {front_vol:.0f} ({back_vol/front_vol:.0%})'}
            elif back_vol > front_vol * 1.3:
                q3 = {'signal': '配合', 'evidence': f'1d 后 5 vol={back_vol:.0f} > 前 5 vol {front_vol:.0f} ({back_vol/front_vol:.0%}) 量价配合'}
            else:
                q3 = {'signal': '正常', 'evidence': f'1d 量价未明显背离 (后5 vol={back_vol:.0f} / 前5 vol={front_vol:.0f} = {back_vol/front_vol if front_vol > 0 else 0:.2f})'}
        else:  # SHORT
            front_low = min(float(k[3]) for k in front)
            back_low = min(float(k[3]) for k in back)
            front_vol = sum(float(k[5]) for k in front)
            back_vol = sum(float(k[5]) for k in back)
            if back_low < front_low and back_vol < front_vol * 0.7:
                q3 = {'signal': '背离', 'evidence': f'1d 后 5 低点={back_low:.4f} < 前 5 低点={front_low:.4f} (创新低) 但 vol={back_vol:.0f} < 前 5 vol {front_vol:.0f} ({back_vol/front_vol:.0%})'}
            elif back_vol > front_vol * 1.3:
                q3 = {'signal': '配合', 'evidence': f'1d 后 5 vol={back_vol:.0f} > 前 5 vol {front_vol:.0f} 量价配合'}
            else:
                q3 = {'signal': '正常', 'evidence': f'1d 量价未明显背离'}

    # 判定
    weak_count = sum(1 for q in (q1, q2, q3) if q['signal'] in ('走完', '衰退', '背离'))
    if side == 'LONG':
        if weak_count >= 3 and upnl_pct > 0:
            verdict = '主动平仓'
        elif weak_count >= 2 and upnl_pct > 0:
            verdict = '锁档'
        elif upnl_pct < -3:
            verdict = '止损等待'
        else:
            verdict = '持有'
    else:  # SHORT
        if weak_count >= 3 and upnl_pct > 0:
            verdict = '主动平仓'
        elif weak_count >= 2 and upnl_pct > 0:
            verdict = '锁档'
        elif upnl_pct < -3:
            verdict = '止损等待'
        else:
            verdict = '持有'

    # 输出表格
    print(f"\n{'='*70}")
    print(f"🔍 {symbol} {side} 持仓分析 (lg.md §14)")
    print(f"{'='*70}")
    print(f"入场: ${entry:.4f}  现价: ${current:.4f}  浮盈: {upnl_pct:+.2f}%  数量: {amount}")
    print(f"\n{'='*70}")
    print(f"§14 4 问脚本化结果")
    print(f"{'='*70}")
    rows = [
        ('Q1 趋势走完?', q1['signal'], q1['evidence']),
        ('Q2 动能衰退?', q2['signal'], q2['evidence']),
        ('Q3 量价背离?', q3['signal'], q3['evidence']),
        ('Q4 反向支撑?', '(LLM 填)', '3 个"为什么不平"理由 — 需 LLM 列举'),
    ]
    for q, sig, ev in rows:
        print(f"  {q:18s} [{sig}]  {ev}")
    print(f"\n{'='*70}")
    print(f"判定: {verdict}  (weak_count={weak_count}/3, 浮盈={upnl_pct:+.2f}%)")
    print(f"{'='*70}\n")
    return {
        'symbol': symbol, 'side': side, 'upnl_pct': upnl_pct, 'current_price': current,
        'q1_trend': q1, 'q2_momentum': q2, 'q3_volume_price': q3,
        'verdict': verdict, 'weak_count': weak_count,
    }


# ========== 实盘数据统计 (2026-06-05 P0 新增) ==========
def get_trade_stats(days: int = 30, symbol: str = None) -> Dict:
    """从币安 papi 拉实盘成交记录,统计胜率/PnL/方向表现
    P0 Fix (2026-06-05): journal.json 是 SIM 模拟数据, 实盘数据必须从服务端拉
    """
    trader = BinanceTrader()  # 用 trader._papi_request 自动签名
    end_ms = int(time.time() * 1000)

    print(f"\n{'='*70}")
    print(f"📊 实盘交易统计 (最近 {days} 天)")
    print(f"{'='*70}")

    # 拉所有成交 (按 7 天窗口分页,币安限制单窗口 ≤7 天)
    all_trades = []
    for start_off in range(0, days, 7):
        s_t = end_ms - min(start_off + 7, days) * 24 * 3600 * 1000
        e_t = end_ms - start_off * 24 * 3600 * 1000
        params = {
            'limit': 100,
            'startTime': s_t,
            'endTime': e_t,
        }
        if symbol:
            params['symbol'] = symbol
        try:
            # _papi_request 自动签名 + 走限速器
            data = trader._papi_request('GET', '/papi/v1/um/userTrades', params)
            if isinstance(data, list):
                all_trades.extend(data)
        except Exception as ex:
            print(f'  ⚠️ 拉取 {start_off}-{start_off+7}d 失败: {ex}')
        time.sleep(0.2)

    # 去重
    seen = set()
    uniq = []
    for t in all_trades:
        if t['id'] not in seen:
            seen.add(t['id'])
            uniq.append(t)
    all_trades = sorted(uniq, key=lambda x: x['time'])

    print(f'实盘成交: {len(all_trades)} 笔')

    if not all_trades:
        print('(无成交)')
        return {'trades': 0}

    # 按 symbol FIFO 配对
    by_sym = {}
    for t in all_trades:
        by_sym.setdefault(t['symbol'], []).append(t)

    pairs = []
    for sym, trades in by_sym.items():
        pos_qty = 0
        pos_side = None
        pos_entry = 0
        for t in trades:
            side = t['side']
            qty = float(t['qty'])
            px = float(t['price'])
            pnl = float(t.get('realizedPnl', 0))
            comm = float(t.get('commission', 0))
            if pos_qty == 0:
                pos_side = 'LONG' if side == 'BUY' else 'SHORT'
                pos_entry = px
                pos_qty = qty if pos_side == 'LONG' else -qty
            else:
                # 同 side = 加仓
                if (pos_side == 'LONG' and side == 'BUY') or (pos_side == 'SHORT' and side == 'SELL'):
                    old_qty = abs(pos_qty)
                    pos_entry = (pos_entry * old_qty + px * qty) / (old_qty + qty)
                    pos_qty = (old_qty + qty) if pos_side == 'LONG' else -(old_qty + qty)
                else:
                    # 反向 = 平仓
                    old_qty = abs(pos_qty)
                    close_qty = min(qty, old_qty)
                    if pos_side == 'LONG':
                        ret = (px - pos_entry) / pos_entry * 100
                    else:
                        ret = (pos_entry - px) / pos_entry * 100
                    pnl_per_unit = pnl / qty if qty else 0
                    pair_pnl = pnl_per_unit * close_qty
                    pairs.append({
                        'symbol': sym, 'side': pos_side, 'entry': pos_entry,
                        'exit': px, 'qty': close_qty, 'ret_pct': ret,
                        'pnl': pair_pnl, 'commission': comm,
                        'close_time': datetime.fromtimestamp(t['time']/1000)
                    })
                    remain = qty - close_qty
                    if remain > 0:
                        pos_side = 'LONG' if side == 'BUY' else 'SHORT'
                        pos_entry = px
                        pos_qty = remain if pos_side == 'LONG' else -remain
                    else:
                        pos_qty = 0
                        pos_side = None
                        pos_entry = 0

    if not pairs:
        print('(无完整配对)')
        return {'trades': len(all_trades), 'pairs': 0}

    long_p = [p for p in pairs if p['side'] == 'LONG']
    short_p = [p for p in pairs if p['side'] == 'SHORT']
    wins = [p for p in pairs if p['pnl'] > 0]
    losses = [p for p in pairs if p['pnl'] < 0]
    total_pnl = sum(p['pnl'] for p in pairs)
    total_comm = sum(p['commission'] for p in pairs)
    breaches = [p for p in pairs if p['ret_pct'] < -5]

    print(f'\n=== 总览 ===')
    print(f'配对: {len(pairs)} 笔 (LONG {len(long_p)} + SHORT {len(short_p)})')
    print(f'总 PnL: {total_pnl:+.4f}  手续费: -{total_comm:.4f}  净: {total_pnl-total_comm:+.4f}')
    print(f'胜: {len(wins)} 负: {len(losses)} 胜率: {len(wins)/len(pairs)*100:.1f}%')
    if breaches:
        bp = sum(p['pnl'] for p in breaches)
        print(f'⚠️ 5% 止损地板穿透: {len(breaches)} 笔, 累计 {bp:+.4f}')

    def _stat(pl, name):
        if not pl:
            return
        w = [p for p in pl if p['pnl'] > 0]
        l = [p for p in pl if p['pnl'] < 0]
        pn = sum(p['pnl'] for p in pl)
        aw = sum(p['pnl'] for p in w) / len(w) if w else 0
        al = sum(p['pnl'] for p in l) / len(l) if l else 0
        wr = len(w) / len(pl) * 100
        rr = abs(aw / al) if al else 0
        sub_breaches = [p for p in pl if p['ret_pct'] < -5]
        print(f'\n=== {name} ({len(pl)} 笔) ===')
        print(f'  胜: {len(w)} 负: {len(l)} 胜率: {wr:.1f}%')
        print(f'  总 PnL: {pn:+.4f}  平均盈: {aw:+.4f}  平均亏: {al:+.4f}  R:R: {rr:.2f}')
        if sub_breaches:
            sbp = sum(p['pnl'] for p in sub_breaches)
            print(f'  ⚠️ 5% 地板穿透: {len(sub_breaches)} 笔, 累计 {sbp:+.4f}')
            for p in sub_breaches[:5]:
                print(f'    ❌ {p["close_time"].strftime("%m-%d %H:%M")} {p["symbol"]:12s} ret={p["ret_pct"]:+.2f}% pnl={p["pnl"]:+.4f}')

    _stat(long_p, 'LONG')
    _stat(short_p, 'SHORT')

    # 最近 15 笔
    print(f'\n=== 最近 15 笔 (按平仓时间) ===')
    pairs.sort(key=lambda p: p['close_time'])
    for p in pairs[-15:]:
        sign = '✅' if p['pnl'] > 0 else '❌'
        print(f"  {sign} {p['close_time'].strftime('%m-%d %H:%M')} {p['side']:5s} {p['symbol']:14s} ret={p['ret_pct']:+6.2f}% pnl={p['pnl']:+.4f}")

    # TOP 5 大亏
    if losses:
        losses.sort(key=lambda p: p['pnl'])
        print(f'\n=== 单笔亏损 TOP 5 ===')
        for p in losses[:5]:
            print(f"  ❌ {p['close_time'].strftime('%m-%d %H:%M')} {p['side']:5s} {p['symbol']:14s} ret={p['ret_pct']:+6.2f}% pnl={p['pnl']:+.4f}")

    return {
        'trades': len(all_trades),
        'pairs': len(pairs),
        'long': len(long_p),
        'short': len(short_p),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': len(wins) / len(pairs),
        'total_pnl': total_pnl,
        'total_commission': total_comm,
        'net_pnl': total_pnl - total_comm,
        'breaches_5pct': len(breaches),
    }


# ========== IC 评估 + 动态权重 + 降级 (2026-06-05 实装) ==========
_WEIGHT_STATE_FILE = os.path.join(os.path.dirname(__file__), '.weight_state.json')

def compute_ic_rolling(trades: List[Dict], window: int = 20) -> Dict:
    """计算最近 N 笔的方向 IC + Rank IC
    trades: 排序后的成交列表, 每笔包含 symbol/side/ret_pct
    返回: {
        'directional_ic': 胜率 (0-1),
        'rank_ic': Spearman-like 排序 IC,
        'magnitude_ic': Pearson-like 幅度 IC,
        'window': 实际窗口,
        'long_ic': LONG 胜率,
        'short_ic': SHORT 胜率,
    }
    """
    if not trades:
        return {'directional_ic': 0, 'rank_ic': 0, 'magnitude_ic': 0, 'window': 0}

    last_n = trades[-window:]
    n = len(last_n)

    # 方向 IC: 胜率 (ret > 0 算对)
    wins = sum(1 for t in last_n if t.get('pnl', 0) > 0)
    directional_ic = wins / n

    # 幅度 IC (Magnitude IC): 预判 vs 实际 (这里退化为实际 ret 的均值, 简化)
    # lg.md 5.2 要求预判 vs 实际配对, 但我们没存预判, 用 ret_pct 均值代替
    avg_ret = sum(t.get('ret_pct', 0) for t in last_n) / n
    magnitude_ic = avg_ret  # > 0 表示平均盈利

    # Rank IC (Spearman): 简化为 ret 排序与时间排序的相关系数
    sorted_by_ret = sorted(last_n, key=lambda x: x.get('ret_pct', 0), reverse=True)
    sorted_by_time = sorted(last_n, key=lambda x: x.get('close_time', datetime.min))
    # 排名差平方和
    d_squared = 0
    for i, t in enumerate(sorted_by_time):
        rank_by_ret = next(j for j, x in enumerate(sorted_by_ret) if x is t)
        d_squared += (rank_by_ret - i) ** 2
    if n > 1:
        rank_ic = 1 - 6 * d_squared / (n * (n * n - 1))
    else:
        rank_ic = 0

    # LONG / SHORT 胜率
    long_trades = [t for t in last_n if t.get('side') == 'LONG']
    short_trades = [t for t in last_n if t.get('side') == 'SHORT']
    long_ic = (sum(1 for t in long_trades if t.get('pnl', 0) > 0) / len(long_trades)) if long_trades else 0
    short_ic = (sum(1 for t in short_trades if t.get('pnl', 0) > 0) / len(short_trades)) if short_trades else 0

    return {
        'directional_ic': round(directional_ic, 4),
        'rank_ic': round(rank_ic, 4),
        'magnitude_ic': round(magnitude_ic, 4),
        'window': n,
        'long_ic': round(long_ic, 4),
        'short_ic': round(short_ic, 4),
    }


def get_degradation_level(ic_stats: Dict) -> Dict:
    """根据 lg.md 11.1 阈值返回降级等级（**不拒交易，只调仓位**）
    Returns: {
        'level': 0/1/2/3,
        'action': '正常'/'仓位减半'/'L2 轻仓'/'L3 试探仓',
        'reason': str,
        'position_scale': 1.0/0.5/0.25/0.1 (用于调整保证金)
    }
    """
    di = ic_stats.get('directional_ic', 0)
    ri = ic_stats.get('rank_ic', 0)
    n = ic_stats.get('window', 0)

    if n < 20:
        return {
            'level': -1,  # 冷启动
            'action': f'冷启动 (N={n}/20)',
            'reason': '样本不足 20 笔, 不评估',
            'position_scale': 1.0,
        }

    # lg.md 11.1 原始阈值 (不拒交易, 只调仓位):
    #   L0 (≥50%): position_scale = 1.0
    #   L1 (40-49%): position_scale = 0.5
    #   L2 (30-39%): position_scale = 0.25
    #   L3 (<30%): position_scale = 0.1
    # 2026-06-08 18:08 修复: 之前 L2/L3 设 position_scale=0.0 + "停止交易" 违反 lg.md "不拒交易" 原则
    # 修正: position_scale 恢复 0.25 / 0.1, action 文案改为 "建议暂停但仍可交易"
    if di < 0.30:
        return {
            'level': 3,
            'action': '⚠️ 仓位 × 0.1 (L3 试探仓), 建议复盘策略但仍可交易',
            'reason': f'方向 IC {di*100:.1f}% < 30%',
            'position_scale': 0.1,
        }
    if di < 0.40:
        return {
            'level': 2,
            'action': '⚠️ 仓位 × 0.25 (L2 轻仓), 不拒交易',
            'reason': f'方向 IC {di*100:.1f}% < 40%',
            'position_scale': 0.25,
        }
    if di < 0.50:
        return {
            'level': 1,
            'action': '⚠️ 仓位 × 0.5 (L1 减半), 不拒交易',
            'reason': f'方向 IC {di*100:.1f}% < 50%',
            'position_scale': 0.5,
        }
    return {
        'level': 0,
        'action': '✅ 正常交易',
        'reason': f'方向 IC {di*100:.1f}% ≥ 50%',
        'position_scale': 1.0,
    }


def compute_dynamic_weights(ic_stats: Dict) -> Dict:
    """根据各尺度 IC 计算动态权重 (v2 2026-06-10: 加 5m 权重)
    Args:
        ic_stats: {long_ic, short_ic, magnitude_ic, ...}
    Returns:
        {1d, 4h, 1h, 15m, 5m} 5 个权重 (和=1)
        5m 权重上限 0.10 (Kronos 基准频率, 但噪声大不能压过 1h)
    """
    # 默认权重 (冷启动 5 档)
    w = {'1d': 0.35, '4h': 0.25, '1h': 0.20, '15m': 0.10, '5m': 0.10}

    n = ic_stats.get('window', 0)
    if n < 20:
        return w  # 冷启动: 默认

    di = ic_stats['directional_ic']
    if di > 0.6:
        # 强趋势: 1d 主导, 5m 压到下限
        w = {'1d': 0.50, '4h': 0.25, '1h': 0.15, '15m': 0.05, '5m': 0.05}
    elif di > 0.5:
        # 中等: 1d 为主, 5m 适度
        w = {'1d': 0.40, '4h': 0.28, '1h': 0.18, '15m': 0.07, '5m': 0.07}
    elif di > 0.4:
        # 偏弱: 1d 減, 5m 抬到上限
        w = {'1d': 0.30, '4h': 0.28, '1h': 0.22, '15m': 0.10, '5m': 0.10}
    else:
        # 弱势 (L1/L2 状态): 1d 減到最低, 1h 主导, 5m 上限
        w = {'1d': 0.20, '4h': 0.25, '1h': 0.30, '15m': 0.15, '5m': 0.10}

    # 验证: 权重和=1 (5 档加权误差 ≤ 0.001)
    total = sum(w.values())
    assert abs(total - 1.0) < 0.001, f"5 档权重和={total} 偏离 1.0"
    return w


def save_weight_state(ic_stats: Dict, weights: Dict, degradation: Dict):
    """保存权重 + 降级状态"""
    state = {
        'updated_at': datetime.now().isoformat(),
        'ic_stats': ic_stats,
        'weights': weights,
        'degradation': degradation,
    }
    try:
        with open(_WEIGHT_STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f'[WARN] 保存权重状态失败: {e}')
    # lg.md 10.0/12.2 硬要求: 同步更新 memory 顶部权重表
    _ensure_memory_weights_table(weights, ic_stats)


def load_weight_state() -> Dict:
    """读取上次的权重状态"""
    if os.path.exists(_WEIGHT_STATE_FILE):
        try:
            with open(_WEIGHT_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# lg.md 10.0 / 12.2 顶部权重表 — P0 2026-06-06 添加
# 背景: qwen 反复不写"今日权重表"区块, 论文 10.0 硬要求落空.
# 修法: 程序在 ic-weights / 开仓 / 平仓 每次都强制维护该区块.
_MEMORY_DIR = os.path.join(os.path.dirname(__file__), 'memory')

def _ensure_memory_weights_table(weights: Dict, ic_stats: Dict) -> None:
    """lg.md 10.0 / 12.2 硬要求: memory/YYYY-MM-DD.md 顶部权重表始终存在
    如果不存在, 创建文件 + 写入顶部区块
    如果被 daily-skill 重写冲掉了, 重新插回顶部区块
    """
    try:
        os.makedirs(_MEMORY_DIR, exist_ok=True)
        today = datetime.now().strftime('%Y-%m-%d')
        mem_file = os.path.join(_MEMORY_DIR, f'{today}.md')
        # 表格区块 (10.0 格式)
        n_trades = ic_stats.get('window', 0)
        if n_trades < 20:
            status = f'冷启动中 (累计 {n_trades} 笔 / 需 20 笔解锁)'
            ic_str = '(冷启动中)'
        else:
            status = '已解锁 (≥20 笔)'
            ic_str = f"{ic_stats.get('directional_ic', 0)*100:.1f}%"
        table_block = f"""## 📐 今日权重表 (程序强制维护, lg.md 10.0/12.2)

| 尺度 | 权重 | 最近 20 笔方向 IC | 来源 |
|------|------|------------------|------|
| 1d   | {weights.get('1d', 0.35):.2f}  | {ic_str} | {'ic-weights' if n_trades >= 20 else '默认'} |
| 4h   | {weights.get('4h', 0.25):.2f}  | {ic_str} | {'ic-weights' if n_trades >= 20 else '默认'} |
| 1h   | {weights.get('1h', 0.20):.2f}  | {ic_str} | {'ic-weights' if n_trades >= 20 else '默认'} |
| 15m  | {weights.get('15m', 0.10):.2f} | {ic_str} | {'ic-weights' if n_trades >= 20 else '默认'} |
| 5m   | {weights.get('5m', 0.10):.2f}  | {ic_str} | {'ic-weights' if n_trades >= 20 else '默认'} |

{status} — 2026-06-10 加 5m 权重 (Kronos 基准频率), 程序会随 IC 变化自动更新

---
"""
        # 清理重复的 "---" 行 (daily-skill 反复重写会导致 3-9 个 "---" 重复)
        def _dedupe_separators(content: str) -> str:
            """压缩连续 3+ 个 '---' 行 为 1 个"""
            lines = content.split('\n')
            out = []
            sep_count = 0
            for line in lines:
                if line.strip() == '---':
                    sep_count += 1
                    if sep_count > 1:
                        continue  # 跳过重复
                    out.append(line)
                else:
                    sep_count = 0
                    out.append(line)
            return '\n'.join(out)
        if not os.path.exists(mem_file):
            with open(mem_file, 'w') as f:
                f.write(table_block)
            return
        # 已存在: 检测顶部区块是否完整 (检查 "今日权重表" 标记)
        with open(mem_file, 'r') as f:
            content = f.read()
        if '今日权重表' in content[:1500]:
            # 已存在, 覆盖该区块
            lines = content.split('\n')
            new_lines = []
            in_block = False
            block_done = False
            for line in lines:
                if line.startswith('## 📐 今日权重表'):
                    in_block = True
                    new_lines.extend(table_block.rstrip('\n').split('\n'))
                    continue
                if in_block and line.strip() == '---':
                    in_block = False
                    block_done = True
                    new_lines.append(line)
                    continue
                if in_block:
                    continue
                new_lines.append(line)
            new_content = '\n'.join(new_lines)
        else:
            # 被冲掉了, 插回顶部
            new_content = table_block + content
        # 最终清理重复 "---" (daily-skill 累积的)
        new_content = _dedupe_separators(new_content)
        with open(mem_file, 'w') as f:
            f.write(new_content)
    except Exception as e:
        # 写权重表失败不应该阻断交易
        pass


def verify_memory(date_str: str = None) -> Dict:
    """验证 memory/YYYY-MM-DD.md 是否按 lg.md §10.0 必填标题 + 列名填 (v2 2026-06-10)
    背景: 2026-06-10 cron 跑的 memory:
    - 5/5 必填标题在, 但
    - 缺总览表 (## Cron 总览)
    - 持仓巡检表只 6 列, 少 数量/入场/现价/阶梯档/peak 锁档 5 列
    - 候选池表改成 "1d 趋势/4h 动能" 语义化, 跟模板列名不一致
    - 决策缺 "3 条反向视角"
    - 4 问是 LLM 自己拍 (没调 analyze-position)
    修复: 6 个必填标题 + 关键列名校验, 缺一个报警

    Args:
        date_str: 'YYYY-MM-DD' (默认今天)
    Returns:
        {'date': 'YYYY-MM-DD', 'ok': bool, 'missing_titles': [...], 'missing_columns': [...], 'present_titles': [...]}
    """
    from datetime import datetime
    if date_str is None:
        date_str = datetime.now().strftime('%Y-%m-%d')
    mem_file = os.path.join(_MEMORY_DIR, f'{date_str}.md')

    # §10.0.1 必填 6 个标题 (v2 加 ### Cron 总览)
    required_titles = [
        '### Cron 总览',
        '### 持仓巡检（步骤 1a + 1b）',
        '### 候选池（步骤 2',
        '### 决策（步骤 3）',
        '### 持仓 4 问分析',
        '### Bug / 异常',
    ]

    # 必填列名 (v2 2026-06-10 升级, 防 LLM 改列名)
    required_columns = {
        '### Cron 总览': ['账户余额', '浮盈', 'IC 状态', 'Cron 步骤'],
        '### 持仓巡检（步骤 1a + 1b）': ['阶梯档', 'peak 锁档', '1b 判定'],
        '### 候选池（步骤 2': ['1h-vol', '24h-vol', '24h-chg', '5m 摘要'],
        '### 决策（步骤 3）': ['反向视角'],  # 3 条必填
    }

    if not os.path.exists(mem_file):
        print(f"\n{'='*70}")
        print(f"❌ {date_str} memory 文件不存在: {mem_file}")
        print(f"{'='*70}")
        return {'date': date_str, 'ok': False, 'missing_titles': required_titles, 'missing_columns': [], 'present_titles': []}

    with open(mem_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # 标题检查
    present_titles = [h for h in required_titles if h in content]
    missing_titles = [h for h in required_titles if h not in content]

    # 列名校验: 按标题切分内容, 在该标题下找必填列
    missing_columns = []
    for title, cols in required_columns.items():
        if title not in content:
            continue  # 标题都缺, 列名肯定缺
        # 找该标题下到下一个 ### 之前的内容
        idx = content.find(title)
        next_idx = content.find('\n### ', idx + len(title))
        section = content[idx:next_idx if next_idx > 0 else len(content)]
        for col in cols:
            if col not in section:
                missing_columns.append(f"{title}: 缺列 '{col}'")

    ok = not missing_titles and not missing_columns

    print(f"\n{'='*70}")
    print(f"🔍 {date_str} memory 验证 (lg.md §10.0.1 + 列名)")
    print(f"{'='*70}")
    print(f"文件: {mem_file}")
    print(f"大小: {len(content)} 字节")
    print(f"\n必填标题 (6 个):")
    for h in required_titles:
        status = '✅' if h in present_titles else '❌ 缺'
        print(f"  {status}  {h}")
    if required_columns:
        print(f"\n必填列名检查:")
        for col_err in missing_columns:
            print(f"  ❌ 缺列: {col_err}")
        if not missing_columns:
            print(f"  ✅ 所有必填列名都在")
    print(f"\n结果: {len(present_titles)}/6 标题, {len(missing_columns)} 列名缺")
    if missing_titles or missing_columns:
        print(f"\n⚠️ 需重写 memory:")
        for h in missing_titles:
            print(f"  - 缺标题: {h}")
        for col_err in missing_columns:
            print(f"  - {col_err}")
    print(f"{'='*70}\n")
    return {
        'date': date_str, 'ok': ok,
        'present_titles': present_titles, 'missing_titles': missing_titles,
        'missing_columns': missing_columns,
    }


def get_ic_and_weights(days: int = 30) -> Dict:
    """从实盘拉数据 → 算 IC → 调权重 → 返回报告
    同时打印 lg.md 5.2 闭环 + 11.1 降级表 + 动态权重
    """
    # 复用 get_trade_stats 的拉取逻辑
    trader = BinanceTrader()
    end_ms = int(time.time() * 1000)

    all_trades = []
    for start_off in range(0, days, 7):
        s_t = end_ms - min(start_off + 7, days) * 24 * 3600 * 1000
        e_t = end_ms - start_off * 24 * 3600 * 1000
        params = {
            'limit': 100,
            'startTime': s_t,
            'endTime': e_t,
        }
        try:
            data = trader._papi_request('GET', '/papi/v1/um/userTrades', params)
            if isinstance(data, list):
                all_trades.extend(data)
        except Exception as ex:
            print(f'  ⚠️ 拉取 {start_off}-{start_off+7}d 失败: {ex}')
        time.sleep(0.2)

    # 去重
    seen = set()
    uniq = []
    for t in all_trades:
        if t['id'] not in seen:
            seen.add(t['id'])
            uniq.append(t)
    all_trades = sorted(uniq, key=lambda x: x['time'])

    # FIFO 配对 (跟 get_trade_stats 一样)
    by_sym = {}
    for t in all_trades:
        by_sym.setdefault(t['symbol'], []).append(t)
    pairs = []
    for sym, trades in by_sym.items():
        pos_qty = 0; pos_side = None; pos_entry = 0
        for t in trades:
            side = t['side']
            qty = float(t['qty'])
            px = float(t['price'])
            pnl = float(t.get('realizedPnl', 0))
            if pos_qty == 0:
                pos_side = 'LONG' if side == 'BUY' else 'SHORT'
                pos_entry = px
                pos_qty = qty if pos_side == 'LONG' else -qty
            else:
                if (pos_side == 'LONG' and side == 'BUY') or (pos_side == 'SHORT' and side == 'SELL'):
                    old_qty = abs(pos_qty)
                    pos_entry = (pos_entry * old_qty + px * qty) / (old_qty + qty)
                    pos_qty = (old_qty + qty) if pos_side == 'LONG' else -(old_qty + qty)
                else:
                    old_qty = abs(pos_qty)
                    close_qty = min(qty, old_qty)
                    if pos_side == 'LONG':
                        ret = (px - pos_entry) / pos_entry * 100
                    else:
                        ret = (pos_entry - px) / pos_entry * 100
                    pnl_per_unit = pnl / qty if qty else 0
                    pair_pnl = pnl_per_unit * close_qty
                    pairs.append({
                        'symbol': sym, 'side': pos_side, 'entry': pos_entry,
                        'exit': px, 'qty': close_qty, 'ret_pct': ret,
                        'pnl': pair_pnl, 'commission': float(t.get('commission', 0)),
                        'close_time': datetime.fromtimestamp(t['time']/1000)
                    })
                    remain = qty - close_qty
                    if remain > 0:
                        pos_side = 'LONG' if side == 'BUY' else 'SHORT'
                        pos_entry = px
                        pos_qty = remain if pos_side == 'LONG' else -remain
                    else:
                        pos_qty = 0; pos_side = None; pos_entry = 0

    # 算 IC + 权重 + 降级
    ic_stats = compute_ic_rolling(pairs, window=20)
    weights = compute_dynamic_weights(ic_stats)
    degradation = get_degradation_level(ic_stats)

    # 持久化
    save_weight_state(ic_stats, weights, degradation)

    # 打印报告
    print(f"\n{'='*70}")
    print(f"📊 IC 评估 + 动态权重报告 (最近 {days} 天, 配对 {len(pairs)} 笔)")
    print(f"{'='*70}")
    print(f"窗口: 最近 {ic_stats['window']} 笔")
    print(f"\n--- IC 指标 (lg.md 5.2) ---")
    print(f"  方向 IC (胜率):       {ic_stats['directional_ic']*100:5.1f}%")
    print(f"  幅度 IC (平均收益率): {ic_stats['magnitude_ic']:+.2f}%")
    print(f"  Rank IC (Spearman):   {ic_stats['rank_ic']:+.4f}")
    print(f"  LONG 胜率:             {ic_stats['long_ic']*100:5.1f}%")
    print(f"  SHORT 胜率:            {ic_stats['short_ic']*100:5.1f}%")

    print(f"\n--- 降级状态 (lg.md 11.1) ---")
    print(f"  等级: L{degradation['level']}")
    print(f"  动作: {degradation['action']}")
    print(f"  原因: {degradation['reason']}")
    print(f"  仓位倍数: {degradation['position_scale']}x")

    print(f"\n--- 动态权重 (lg.md 5.2 + 11.1, 5 档) ---")
    print(f"  1d  = {weights['1d']:.2f}")
    print(f"  4h  = {weights['4h']:.2f}")
    print(f"  1h  = {weights['1h']:.2f}")
    print(f"  15m = {weights['15m']:.2f}")
    print(f"  5m  = {weights['5m']:.2f}  (Kronos 基准频率, v2 2026-06-10 新增)")
    print(f"  (默认: 1d=0.35 4h=0.25 1h=0.20 15m=0.10 5m=0.10)")

    # 与历史状态对比
    prev = load_weight_state()
    if prev and prev.get('weights'):
        prev_w = prev['weights']
        if prev_w != weights:
            print(f"\n--- 权重调整 ---")
            for k in weights:
                if abs(weights[k] - prev_w.get(k, weights[k])) > 0.01:
                    arrow = '↑' if weights[k] > prev_w.get(k, 0) else '↓'
                    print(f"  {k}: {prev_w.get(k, 0):.2f} {arrow} {weights[k]:.2f}")

    return {
        'ic_stats': ic_stats,
        'weights': weights,
        'degradation': degradation,
        'pairs': len(pairs),
    }



# ========== 命令行入口 ==========
def main():
    parser = argparse.ArgumentParser(description='Binance Trading Bot v13.0')
    subparsers = parser.add_subparsers(dest='command', help='子命令')

    # status
    status_parser = subparsers.add_parser('status', help='账户状态')
    status_parser.add_argument('--symbol', type=str, help='指定币种')

    # sync-sim: 从 journal 重构修复 .sim_state
    sync_parser = subparsers.add_parser('sync-sim', help='从journal重构状态修复.sim_state')

    # verify-sim: 诊断状态一致性
    verify_parser = subparsers.add_parser('verify-sim', help='验证.sim_state与journal一致性')

    # verify-memory: 检查 memory/YYYY-MM-DD.md 是否含 §10.0 必填标题
    verify_memory_parser = subparsers.add_parser('verify-memory', help='检查 memory 是否按 §10.0 必填标题填 (缺标题报警)')
    verify_memory_parser.add_argument('--date', type=str, default=None, help='指定日期 YYYY-MM-DD (默认今天)')

    # scan-all(统一波动率扫描,多空一起)
    scan_all_parser = subparsers.add_parser('scan-all', help='统一波动率扫描(多空一起)')
    scan_all_parser.add_argument('--top', type=int, default=30, help='最终输出币种个数(默认 30, 对齐 SKILL.md §3 cron 循环)')
    scan_all_parser.add_argument('--klines', type=int, default=None, help='取多少个候选币种拿K线(默认等于--top值)')
    scan_all_parser.add_argument('--kline-detail', action='store_true',
                                  help='每个候选打印 1d × 5 + 4h × 5 + 1h × 5 + 15m × 5 + 5m × 12 K 线')
    scan_all_parser.add_argument('--min-vol', type=float, default=0.5, help='最低 1h 波动率(默认 0.5%, 对齐 SKILL.md §3 cron 循环)')
    scan_all_parser.add_argument('--max-vol-24h', type=float, default=20.0, help='24h 波动率上限(默认 20%, 对齐 SKILL.md §3 cron 循环)')
    scan_all_parser.add_argument('--max-chg-24h', type=float, default=10.0, help='24h 涨跌幅上限(默认 ±10%, 对齐 SKILL.md §3 cron 循环)')
    scan_all_parser.add_argument('--log', type=str, default=None, help='日志文件(覆盖模式,默认stdout)')

    # check-ladder: 扫所有持仓 + 算阶梯 + 给出 replace-order 建议
    check_ladder_parser = subparsers.add_parser('check-ladder', help='扫所有持仓 + 算阶梯档 + 建议调 replace-order')

    # analyze-position: 持仓 §14 4 问脚本化分析 (lg.md §14)
    analyze_position_parser = subparsers.add_parser('analyze-position', help='单个持仓 §14 4 问脚本化分析 (趋势/动能/量价/反向支撑)')
    analyze_position_parser.add_argument('--symbol', type=str, required=True, help='币种')

    # trade-stats (2026-06-05 P0 新增) - 从币安 papi 拉实盘数据
    trade_stats_parser = subparsers.add_parser('trade-stats', help='实盘交易统计 (papi userTrades, 替代 journal.json)')
    trade_stats_parser.add_argument('--days', type=int, default=30, help='拉最近 N 天 (默认 30)')
    trade_stats_parser.add_argument('--symbol', type=str, default=None, help='指定币种 (可选)')

    # ic-weights (2026-06-05): IC 评估 + 动态权重 + 降级 (lg.md 5.2 + 11.1)
    ic_weights_parser = subparsers.add_parser('ic-weights', help='IC 评估 + 动态权重 + 降级 (lg.md 5.2/11.1)')
    ic_weights_parser.add_argument('--days', type=int, default=30, help='拉最近 N 天 (默认 30)')

    # market
    market_parser = subparsers.add_parser('market', help='市场数据')
    market_parser.add_argument('--symbol', type=str, required=True, help='币种')
    market_parser.add_argument('--kline-last', type=int, default=15, help='K线数量(默认15)')
    market_parser.add_argument('--interval', type=str, action='append', default=None,
                               help='K线周期(可多次指定,可选 1m/5m/15m/30m/1h/4h/1d;默认 30m,1h;多尺度验证推荐 1d,4h,1h,15m)')
    market_parser.add_argument('--format', type=str, default='classic', choices=['classic', 'kronos'],
                               help='输出格式: classic (默认,原始数字) | kronos (P1,离散 token + 三视角提示)')

    # open-short
    open_short_parser = subparsers.add_parser('open-short', help='开空仓')
    open_short_parser.add_argument('margin', type=float, nargs='?', default=1.0, help='保证金 (USDT, 默认 1.0 — 小于 1 也补到 1, 最小可开仓)')
    open_short_parser.add_argument('--symbol', type=str, required=True, help='币种')
    open_short_parser.add_argument('--leverage', type=int, default=10, help='杠杆')


    # close-short
    close_short_parser = subparsers.add_parser('close-short', help='平空仓')
    close_short_parser.add_argument('--symbol', type=str, required=True, help='币种')
    close_short_parser.add_argument('--percent', type=float, default=100, help='平仓比例(0-100),默认100全平')
    close_short_parser.add_argument('--force', action='store_true', help='绕过 <5min 强制不平仓 (默认 False)')

    # open-long
    open_long_parser = subparsers.add_parser('open-long', help='开多仓')
    open_long_parser.add_argument('margin', type=float, nargs='?', default=1.0, help='保证金 (USDT, 默认 1.0 — 小于 1 也补到 1, 最小可开仓)')
    open_long_parser.add_argument('--symbol', type=str, required=True, help='币种')
    open_long_parser.add_argument('--leverage', type=int, default=10, help='杠杆')

    # close-long
    close_long_parser = subparsers.add_parser('close-long', help='平多仓')
    close_long_parser.add_argument('--symbol', type=str, required=True, help='币种')
    close_long_parser.add_argument('--percent', type=float, default=100, help='平仓比例(0-100),默认100全平')
    close_long_parser.add_argument('--force', action='store_true', help='绕过 <5min 强制不平仓 (默认 False)')

    # replace-order: 替换条件单(自动取消旧单+下新单)
    replace_parser = subparsers.add_parser('replace-order', help='替换条件单(自动取消旧单后下新单)')
    replace_parser.add_argument('--symbol', type=str, required=True, help='币种')
    replace_parser.add_argument('--side', type=str, required=True, help="SELL=平多/BUY=平空")
    replace_parser.add_argument('--type', type=str, default='STOP_MARKET',
                                  help="STOP_MARKET/TAKE_PROFIT_MARKET/STOP/TAKE_PROFIT")
    replace_parser.add_argument('--algo-id', type=int, default=None,
                                  help='旧条件单ID(可选)')
    replace_parser.add_argument('--trigger', type=float, default=None,
                                  help='手动指定触发价(可选;默认按 lg.md 7.1 ATR 公式自动计算)')
    replace_parser.add_argument('--qty', type=float, default=None,
                                  help='手动指定数量(可选;默认从持仓自动取)')

    # cancel-conditionals: 取消指定币种全部追踪的委托单
    cancel_cond_parser = subparsers.add_parser('cancel-conditionals', help='取消指定币种全部追踪的委托单')
    cancel_cond_parser.add_argument('--symbol', type=str, required=True, help='币种')
    cancel_cond_parser.add_argument('--side', type=str, default=None,
                                  help='SELL=平多/BUY=平空(可选,不填则取消该币种全部)')

    args = parser.parse_args()
    try:

            if not args.command:
                parser.print_help()
                return

            if args.command == 'status':
                # ⚠️ 强制使用模拟盘读取持仓（避免真实API消耗）
                os.environ['SIMULATE'] = 'true'
                _load_sim_state()
                if args.symbol:
                    resolved = _resolve_symbol(args.symbol)
                    if not resolved:
                        return
                    if resolved != args.symbol.strip().upper():
                        print(f"[AUTO] symbol 解析 → {resolved}")
                    args.symbol = resolved
                get_status(args.symbol)

            elif args.command == 'sync-sim':
                # 从 journal 重构状态，修复 .sim_state
                print(f"\n{'='*60}")
                print(f"🔧 SYNC-SIM: 从 journal 重构模拟状态")
                print(f"{'='*60}")
                journal = _load_journal()
                calc_balance = 1000.0
                calc_positions = {}  # symbol -> list of margin infos
                for t in journal:
                    action = t['action']
                    symbol = t['symbol']
                    margin = t.get('margin', 0)
                    qty = t.get('qty', 0)
                    entry = t.get('entry', 0)
                    pnl = t.get('pnl', 0)
                    side = t.get('side', 'LONG')
                    leverage = t.get('leverage', 10)
                    if action == 'OPEN':
                        fee = qty * entry * 0.001
                        calc_balance -= (margin + fee)
                        if symbol not in calc_positions:
                            calc_positions[symbol] = []
                        calc_positions[symbol].append({'margin': margin, 'qty': qty, 'entry_price': entry, 'side': side, 'leverage': leverage})
                    elif action == 'CLOSE':
                        calc_balance += pnl
                        if symbol in calc_positions and calc_positions[symbol]:
                            calc_positions[symbol].pop()
        
                # 只保留还有持仓的币种（转为 list 结构）
                final_positions = {}
                for sym, stacks in calc_positions.items():
                    if stacks:
                        final_positions[sym] = stacks  # 保存完整栈（支持多层）
        
                _save_sim_state(calc_balance, final_positions)
                print(f"✅ 已同步: balance={calc_balance:.4f}, positions={list(final_positions.keys())}")
                _verify_sim_state(calc_balance, final_positions)

            elif args.command == 'verify-sim':
                _load_sim_state()
                _verify_sim_state(_sim_balance, _sim_positions)

            elif args.command == 'verify-memory':
                from datetime import datetime as _dt
                date_str = args.date or _dt.now().strftime('%Y-%m-%d')
                verify_memory(date_str)

            elif args.command == 'scan-all':
                _orig_out = os.dup(sys.stdout.fileno())
                _orig_err = os.dup(sys.stderr.fileno())
                _log_fd = None
                if args.log:
                    _log_fd = open(args.log, 'w')
                    os.dup2(_log_fd.fileno(), sys.stdout.fileno())
                    os.dup2(_log_fd.fileno(), sys.stderr.fileno())
                    _log_fd.write(datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '\n')
                    _log_fd.flush()
                scan_volatility_top(args.top, args.min_vol, args.klines if args.klines else args.top,
                                    max_vol_24h=args.max_vol_24h, max_chg_24h=args.max_chg_24h,
                                    kline_detail=args.kline_detail)
                if args.log:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os.dup2(_orig_out, sys.stdout.fileno())
                    os.dup2(_orig_err, sys.stderr.fileno())
                    _log_fd.close()
                    os.close(_orig_out)
                    os.close(_orig_err)

            elif args.command == 'market':
                if not args.symbol:
                    print("Error: --symbol is required")
                    return
                # Bug Fix: 用 exchangeInfo 消歧 auto-append (避免 TAU→TAUUSDT 错误)
                sym = _resolve_symbol(args.symbol)
                if not sym:
                    return
                if sym != args.symbol.strip().upper():
                    print(f"[AUTO] symbol 解析 → {sym}")
                data = get_market_data(sym, args.kline_last, args.interval)
                if data is None:
                    return
                # P1 (2026-06-05): 支持 kronos 格式 (量价联合离散 + 三视角提示)
                if args.format == 'kronos':
                    print_market_data_kronos(data)
                else:
                    print_market_data(data)

            elif args.command == 'open-short':
                if not args.symbol:
                    print("Error: --symbol is required")
                    return
                # Bug Fix: exchangeInfo 消歧(同 market)
                sym = _resolve_symbol(args.symbol)
                if not sym:
                    return
                if sym != args.symbol.strip().upper():
                    print(f"[AUTO] symbol 解析 → {sym}")
                do_open_short(sym, args.margin, args.leverage)

            elif args.command == 'close-short':
                if not args.symbol:
                    print("Error: --symbol is required")
                    return
                sym = _resolve_symbol(args.symbol)
                if not sym:
                    return
                if sym != args.symbol.strip().upper():
                    print(f"[AUTO] symbol 解析 → {sym}")
                do_close_short(sym, args.percent, force_close=args.force)

            elif args.command == 'open-long':
                if not args.symbol:
                    print("Error: --symbol is required")
                    return
                sym = _resolve_symbol(args.symbol)
                if not sym:
                    return
                if sym != args.symbol.strip().upper():
                    print(f"[AUTO] symbol 解析 → {sym}")
                do_open_long(sym, args.margin, args.leverage)

            elif args.command == 'close-long':
                if not args.symbol:
                    print("Error: --symbol is required")
                    return
                sym = _resolve_symbol(args.symbol)
                if not sym:
                    return
                if sym != args.symbol.strip().upper():
                    print(f"[AUTO] symbol 解析 → {sym}")
                do_close_long(sym, args.percent, force_close=args.force)

            elif args.command == 'replace-order':
                trader = BinanceTrader()
                # Bug Fix: exchangeInfo 消歧(同 market)
                sym = _resolve_symbol(args.symbol)
                if not sym:
                    return
                if sym != args.symbol.strip().upper():
                    print(f"[AUTO] symbol 解析 → {sym}")
                # 优先用命令行 --algo-id,其次从文件查找
                algo_id = args.algo_id
                if not algo_id:
                    orders = _load_conditional_orders()
                    algo_id = orders.get(sym, {}).get(args.side)
                # 获取数量和入场价(优先查持仓,其次从文件;用户可通过 --qty/--trigger 覆盖)
                # Bug #13 Fix: 持仓为空 + 用户未传 --qty 时,明确报错,不允许用 qty=1 强下止损单
                qty = None
                entry_price = None
                positions = trader.get_positions(sym)
                if positions:
                    qty = int(max(1, abs(positions[0]['amount'])))
                    entry_price = float(positions[0].get('entryPrice', 0)) or None
                if args.qty is not None:
                    qty = int(max(1, args.qty))
                    print(f"[MANUAL] 使用命令行 --qty={qty}")
                if qty is None:
                    print(f"❌ {sym} 无持仓,且未指定 --qty,无法下止损单(避免以 qty=1 误下不完整止损)")
                    print(f"   修复方案: 重开仓后重试,或手动 --qty=<持仓量> 覆盖")
                    return
                if args.trigger is not None:
                    # 用户手动指定触发价 → 跳过 ATR 计算
                    trigger_price = _round_to_tick(args.trigger, sym)
                    print(f"[MANUAL] 使用命令行 --trigger={trigger_price}")
                    # 跳过自动计算，直接下条件单
                    result = trader._place_conditional_order(sym, args.side, qty, args.type, trigger_price, algo_id)
                else:
                    # 触发价由replace_conditional_order内部基于 ATR + lg.md 7.1 公式自动计算
                    result = trader.replace_conditional_order(
                        sym, args.side, qty, args.type, algo_id, entry_price
                    )
                print(f"✅ 条件单已替换: {result}")

            elif args.command == 'check-ladder':
                check_ladder()

            elif args.command == 'analyze-position':
                sym = _resolve_symbol(args.symbol)
                if not sym:
                    return
                if sym != args.symbol.strip().upper():
                    print(f"[AUTO] symbol 解析 → {sym}")
                analyze_position(sym)

            elif args.command == 'trade-stats':
                # P0 (2026-06-05): 从币安 papi 拉实盘数据
                sym = _resolve_symbol(args.symbol) if args.symbol else None
                if args.symbol and not sym:
                    return
                get_trade_stats(days=args.days, symbol=sym)

            elif args.command == 'ic-weights':
                # P1 (2026-06-05): IC 评估 + 动态权重 + 降级 (lg.md 5.2 + 11.1)
                get_ic_and_weights(days=args.days)

            elif args.command == 'cancel-conditionals':
                trader = BinanceTrader()
                orders = _load_conditional_orders()
                if args.symbol:
                    sym = _resolve_symbol(args.symbol)
                    if not sym:
                        return
                    if sym != args.symbol.strip().upper():
                        print(f"[AUTO] symbol 解析 → {sym}")
                else:
                    sym = None
                if sym not in orders or not orders[sym]:
                    print(f"❌ 无追踪记录: {sym}")
                else:
                    cancelled = 0
                    for side, algo_id in list(orders[sym].items()):
                        if args.side and args.side != side:
                            continue
                        try:
                            trader.cancel_conditional_order(sym, algo_id)
                            print(f"✅ 已取消 {sym} {side} algo_id={algo_id}")
                            cancelled += 1
                        except Exception as e:
                            # P0 Fix 2026-06-09: 服务端 -2011 "Unknown order" 表示已无此单(已触发/已取消)
                            # 本地追踪也要清，避免孤儿
                            if 'Unknown order' in str(e) or '-2011' in str(e):
                                orders = _load_conditional_orders()
                                if sym in orders and side in orders[sym]:
                                    del orders[sym][side]
                                    orders = {k: v for k, v in orders.items() if v}
                                    trader._save_conditional_orders = _save_conditional_orders  # 内部用
                                    _save_conditional_orders(orders)
                                    print(f"✅ 服务端已无此单,清理本地追踪 {sym} {side}")
                                    cancelled += 1
                            else:
                                print(f"⚠️ 取消失败 {sym} {side}: {e}")
                    if cancelled == 0:
                        print(f"❌ 未找到匹配的追踪记录")

    except Exception as e:
        print(f'[ERROR] {e}', file=sys.stderr)
        sys.exit(1)
if __name__ == '__main__':
    main()