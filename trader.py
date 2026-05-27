# ========== v10 核心原则 (2026-05-20) ==========
# 1. 严禁使用指标 (RSI/MACD/布林带/MA) — 指标是滞后的谎言
# 2. 只看价格行为: K线力度、成交量、关键位置博弈
# 3. 仓位: 单笔保证金 <= 20% 余额，波动大时 <= 10%
# 4. 止损: 入场价 ±2% (固定) 或 ±1.5倍ATR (波动调整)
# 5. 止盈: 止损幅度的 1.5倍 (R:R = 1:1.5)
# 6. 每笔交易前强制检查黑名单
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
_JOURNAL_FILE = os.path.join(os.path.dirname(__file__), 'journal.json')

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

def _save_trade(trade: Dict):
    """追加单笔交易到 journal.json"""
    journal = _load_journal()
    journal.append({**trade, 'ts': datetime.now().isoformat()})
    try:
        with open(_JOURNAL_FILE, 'w') as f:
            json.dump(journal, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[JOURNAL] ❌ 保存失败({_JOURNAL_FILE}): {e}", file=sys.stderr)


def _verify_sim_state(balance: float, positions: Dict) -> bool:
    """验证 sim_state 与 journal 重构值的一致性（诊断用）"""
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
        if action == 'OPEN':
            fee = qty * entry * 0.001
            calc_balance -= (margin + fee)
            if symbol not in calc_positions:
                calc_positions[symbol] = []
            calc_positions[symbol].append({'margin': margin, 'qty': qty, 'entry': entry, 'side': side})
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
    print(f"[SIM STATE] ✅ 状态一致 (balance={balance:.4f}, positions={list(sim_symbols)})")
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
            r = requests.get(url, headers=headers, timeout=15)
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
        """fapi 公共请求(市场数据)"""
        headers = {'X-MBX-APIKEY': self.api_key}
        if params is None:
            params = {}
        url = f"{self.fapi_url}{endpoint}"
        if method == 'GET':
            r = requests.get(url, headers=headers, params=params, timeout=10)
        elif method == 'POST':
            r = requests.post(url, headers=headers, params=params, timeout=10)
        elif method == 'DELETE':
            r = requests.delete(url, headers=headers, params=params, timeout=10)
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
        """PAPI 签名请求 (PM 统一账户),带重试 - 每次重试都重新签名"""
        headers = {'X-MBX-APIKEY': self.api_key}
        if params is None:
            params = {}
        last_err = None
        for attempt in range(retries):
            # ⭐ 每次重试都重新签名(时间戳必须最新)
            signed_params = self._papi_sign(params.copy())
            url = f"{self.papi_url}{endpoint}?{signed_params}"
            try:
                if method == 'GET':
                    r = requests.get(url, headers=headers, timeout=15)
                elif method == 'POST':
                    r = requests.post(url, headers=headers, timeout=15)
                elif method == 'DELETE':
                    r = requests.delete(url, headers=headers, timeout=15)
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

    def replace_conditional_order(self, symbol: str, side: str, quantity: int,
                                   order_type: str = 'STOP_MARKET',
                                   algo_id: int = None) -> Dict:
        """下新的条件单,自动追踪algo_id

        流程:1)从文件查找旧algo_id 2)尝试取消旧单 3)下新单 4)保存新algo_id
        触发价由本方法基于实时市价自动计算:
          SELL(平多止损): 市价×0.99 (-1%)
          BUY (平空止损): 市价×1.01 (+1%)

        Args:
            symbol: 币种,如 NILUSDT
            side: 'SELL'=平多仓/'BUY'=平空仓
            quantity: 数量(整数)
            order_type: 'STOP_MARKET'(止损) / 'TAKE_PROFIT_MARKET'(止盈)
            algo_id: 可选,直接指定旧条件单ID
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

        # 3. 获取实时价格作为触发价(SELL则低于市价0.1%,BUY则高于市价0.1%)
        current_price = self.get_price(symbol)
        if side.upper() == 'SELL':
            trigger_price = _round_to_tick(current_price * 0.985, symbol)
        else:
            trigger_price = _round_to_tick(current_price * 1.015, symbol)
        print(f"[replace] 当前市价={current_price} → 触发价={trigger_price}")

        # 4. 下新单
        result = self._papi_request('POST', '/papi/v1/um/algo/order', {
            'symbol': symbol,
            'side': side,
            'algoType': 'CONDITIONAL',
            'type': order_type,
            'quantity': str(quantity),
            'triggerPrice': str(trigger_price),
            'reduceOnly': 'true',
        })

        # 5. 保存新algo_id到文件追踪
        new_algo_id = result.get('algoId')
        if new_algo_id:
            orders = _load_conditional_orders()
            if symbol not in orders:
                orders[symbol] = {}
            orders[symbol][side] = new_algo_id
            _save_conditional_orders(orders)
            print(f"[replace] ✅ 新条件单已设置 algo_id={new_algo_id}")


    def _clear_conditional_orders(self, symbol: str):
        """平仓后清除符号的所有条件单追踪"""
        orders = _load_conditional_orders()
        if symbol in orders:
            del orders[symbol]
            _save_conditional_orders(orders)
            print(f"[条件单] 已清除 {symbol} 追踪记录")

    def cancel_conditional_order(self, symbol: str, algo_id: int) -> Dict:
        """取消PM账户条件单,并清除文件追踪记录"""
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
        """获取持仓(PAPI um_position_risk)"""
        if SIMULATE:
            _load_sim_state()
            result = []
            for s, stacks in _sim_positions.items():
                if symbol and s != symbol:
                    continue
                # 同一 symbol 可能有多层持仓，合并计算（后进先出只看最上层）
                if not stacks:
                    continue
                pos = stacks[-1]  # 取最后一层（最新开的）
                try:
                    kl = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': s, 'interval': '1m', 'limit': 1}, timeout=10, verify=certifi.where()).json()
                    current = float(kl[0][4])
                except Exception:
                    current = pos['entry_price']
                entry = pos['entry_price']
                qty = pos['qty']
                margin = pos.get('margin', 0)
                lev = pos.get('leverage', 10)
                if pos.get('side') == 'SHORT':
                    upnl = (entry - current) / entry * lev * margin
                else:
                    upnl = (current - entry) / entry * lev * margin
                result.append({
                    'symbol': s,
                    'amount': qty,
                    'entryPrice': entry,
                    'unrealizedProfit': round(upnl, 4),
                    'leverage': lev,
                    'positionSide': pos.get('side', 'LONG'),
                    'margin': margin,
                    'layers': len(stacks)  # 额外信息：有多少层
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
        """
        if SIMULATE:
            # Bug 8 Fix: 每次调用都重新从磁盘加载，确保获取最新余额
            # （不同进程/命令调用时，内存状态可能已过期）
            _load_sim_state()
            return _sim_balance  # 直接返回内存值，不触发磁盘加载
        if is_portfolio_margin():
            try:
                import urllib3
                import json as _json
                ts = str(int(time.time() * 1000))
                q = f"timestamp={ts}"
                sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
                url = f"https://papi.binance.com/papi/v1/balance?{q}&signature={sig}"
                pool = urllib3.PoolManager(cert_reqs='CERT_REQUIRED', ca_certs=certifi.where())
                headers = {"X-MBX-APIKEY": API_KEY}
                r = pool.urlopen('GET', url, headers=headers, timeout=10.0)
                data = _json.loads(r.data)
                for item in data:
                    if item.get('asset') == 'USDT':
                        return float(item.get('totalWalletBalance', 0))
                return 0.0
            except Exception:
                return 0.0
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
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=10).json()
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
            _save_sim_state(_sim_balance, _sim_positions)
            _save_trade({'symbol': symbol, 'side': side, 'action': 'CLOSE', 'entry': pos['entry_price'], 'exit': price, 'qty': qty, 'pnl': net_pnl, 'leverage': pos.get('leverage', 10), 'margin': pos['margin']})
            print(f"[SIMULATE] 平仓 {symbol} x{qty:.4f} @ {price}, 盈亏={net_pnl:.4f} USDT, 余额={_sim_balance:.4f}", file=sys.stderr)
            return {'orderId': 'sim_' + str(time.time()), 'symbol': symbol, 'side': 'SELL' if side == 'LONG' else 'BUY', 'origQty': str(qty), 'pnl': net_pnl, 'margin': pos['margin']}
        if is_portfolio_margin():
            import math
            positions = self.get_positions(symbol)
            if not positions:
                raise Exception(f"无持仓: {symbol}")
            pos = positions[0]
            if quantity is None:
                quantity = abs(pos['amount'])
            qty_int = max(1, math.ceil(quantity))
            return self._papi_request('POST', '/papi/v1/um/order', {
                'symbol': symbol,
                'side': 'SELL' if pos['positionSide'] == 'LONG' else 'BUY',
                'type': 'MARKET',
                'quantity': qty_int
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
            'quantity': quantity,
            'timestamp': int(time.time()*1000),
        }
        params['signature'] = self._sign(params)
        r = requests.post(f"{self.fapi_url}/fapi/v1/order",
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=10)
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
        r = requests.post(f"{self.fapi_url}/fapi/v1/leverage",
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=10)
        if r.status_code != 200:
            raise Exception(f"set_leverage Error {r.status_code}: {r.text}")
        return r.json()

# ========== BinanceTrader 交易方法(做空/做多)==========
    def open_short(self, symbol: str, quantity: float, leverage: int = 10,
                   ) -> Dict:
        """开空仓(PAPI um_order,单向模式:side=SELL 无需positionSide)"""
        if SIMULATE:
            global _sim_balance, _sim_positions
            # 获取当前价格
            # Bug 3 Fix: 价格获取失败时拒绝开仓（不能用0.001，会导致名义价值计算错误）
            price = None
            try:
                klines = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=10).json()
                if klines and isinstance(klines, list) and len(klines) > 0:
                    price = float(klines[0][4])
            except Exception:
                pass
            if price is None or price <= 0:
                raise Exception(f"[SIMULATE] 无法获取 {symbol} 价格，开仓失败")
            # 保证金模型: 使用80%余额作为保证金的上限
            available_margin = _sim_balance * 0.8
            # 根据可用保证金反推数量
            qty = max(quantity, 1)
            position_value = qty * price
            margin = position_value / leverage
            # 如果保证金超出可用额度,按比例缩减
            if margin > available_margin:
                qty = available_margin * leverage / price
                if qty < 0.0001:  # 金额太小无法交易
                    raise Exception(f"[SIMULATE] 余额不足: 可用 {available_margin:.2f} USDT 无法开仓 {symbol} (价格={price})")
                position_value = qty * price
                margin = position_value / leverage
            fee = position_value * 0.001  # 0.1% 开仓手续费
            # positions 现在是 list，同一 symbol 可以多层持仓
            if symbol not in _sim_positions:
                _sim_positions[symbol] = []
            _sim_positions[symbol].append({'qty': qty, 'entry_price': price, 'leverage': leverage, 'margin': margin, 'side': 'SHORT'})
            _sim_balance -= (margin + fee)  # 扣除保证金和手续费
            _save_sim_state(_sim_balance, _sim_positions)
            # v10: 记录交易日志
            _save_trade({'symbol': symbol, 'side': 'SHORT', 'action': 'OPEN', 'entry': price, 'qty': qty, 'margin': margin, 'leverage': leverage})
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
            qty_int = math.floor(quantity)
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
        r = requests.post(f"{self.fapi_url}/fapi/v1/order",
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=10)
        if r.status_code != 200:
            raise Exception(f"open_short Error {r.status_code}: {r.text}")
        result = r.json()
        return result

    def open_long(self, symbol: str, quantity: float, leverage: int = 10,
                  ) -> Dict:
        """开多仓(PAPI um_order,单向模式:side=BUY 开多)"""
        if SIMULATE:
            global _sim_balance, _sim_positions
            _load_sim_state()  # 确保读取最新状态
            # Bug 3 Fix: 价格获取失败时拒绝开仓
            price = None
            try:
                klines = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=10).json()
                if klines and isinstance(klines, list) and len(klines) > 0:
                    price = float(klines[0][4])
            except Exception:
                pass
            if price is None or price <= 0:
                raise Exception(f"[SIMULATE] 无法获取 {symbol} 价格，开仓失败")
            available_margin = _sim_balance * 0.8
            qty = max(quantity, 1)
            position_value = qty * price
            margin = position_value / leverage
            if margin > available_margin:
                qty = available_margin * leverage / price
                if qty < 0.0001:
                    raise Exception(f"[SIMULATE] 余额不足: 可用 {available_margin:.2f} USDT 无法开多 {symbol}")
                position_value = qty * price
                margin = position_value / leverage
            fee = position_value * 0.001
            # positions 现在是 list，同一 symbol 可以多层持仓
            if symbol not in _sim_positions:
                _sim_positions[symbol] = []
            _sim_positions[symbol].append({'qty': qty, 'entry_price': price, 'leverage': leverage, 'margin': margin, 'side': 'LONG'})
            _sim_balance -= (margin + fee)
            _save_sim_state(_sim_balance, _sim_positions)
            # v10: 记录交易日志
            _save_trade({'symbol': symbol, 'side': 'LONG', 'action': 'OPEN', 'entry': price, 'qty': qty, 'margin': margin, 'leverage': leverage})
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
            qty_int = math.floor(quantity)
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
        r = requests.post(f"{self.fapi_url}/fapi/v1/order",
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=10)
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
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=10).json()
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
        qty_int = math.floor(quantity)
        if qty_int == 0:
            qty_int = 1

        if is_portfolio_margin():
            result = self._papi_request('POST', '/papi/v1/um/order', {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'MARKET',
                'quantity': qty_int
            })
            self._clear_conditional_orders(symbol)
            return result
        params = {
            'symbol': symbol,
            'side': 'SELL',
            'positionSide': 'LONG',
            'type': 'MARKET',
            'quantity': quantity,
            'timestamp': int(time.time()*1000)
        }
        params['signature'] = self._sign(params)
        r = requests.post(f"{self.fapi_url}/fapi/v1/order",
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=10)
        if r.status_code != 200:
            raise Exception(f"close_long Error {r.status_code}: {r.text}")
        return r.json()
# ========== 技术指标计算 ==========
class TechnicalIndicators:
    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> float:
        """计算RSI"""
        if len(prices) < period + 1:
            return 50.0

        deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]

        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 2)

    @staticmethod
    def calculate_macd(prices: List[float]) -> Dict:
        """计算MACD"""
        if len(prices) < 26:
            return {'macd': 0, 'signal': 0, 'histogram': 0}

        # EMA
        def ema(data, period):
            multiplier = 2 / (period + 1)
            ema_val = data[0]
            for price in data[1:]:
                ema_val = (price * multiplier) + (ema_val * (1 - multiplier))
            return ema_val

        # 计算EMA12, EMA26
        ema12 = ema(prices, 12)
        ema26 = ema(prices, 26)
        macd_line = ema12 - ema26

        macd_line = ema12 - ema26

        return {
            'macd': round(macd_line, 4),
        }

    @staticmethod
    def calculate_bollinger_bands(prices: List[float], period: int = 20, std_dev: int = 2) -> Dict:
        """计算布林带"""
        if len(prices) < period:
            return {'upper': 0, 'middle': 0, 'lower': 0, 'position': 50}

        recent = prices[-period:]
        middle = sum(recent) / period
        variance = sum((p - middle) ** 2 for p in recent) / period
        std = variance ** 0.5

        upper = middle + (std_dev * std)
        lower = middle - (std_dev * std)

        # 当前位置百分比
        if upper != lower:
            position = ((prices[-1] - lower) / (upper - lower)) * 100
        else:
            position = 50

        return {
            'upper': round(upper, 2),
            'middle': round(middle, 2),
            'lower': round(lower, 2),
            'position': round(position, 2)
        }

    @staticmethod
    def calculate_atr(klines: List, period: int = 14) -> float:
        """计算ATR"""
        if len(klines) < period + 1:
            return 0.0

        true_ranges = []
        for i in range(1, len(klines)):
            high = float(klines[i][2])
            low = float(klines[i][3])
            prev_close = float(klines[i-1][4])

            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
            true_ranges.append(tr)

        atr = sum(true_ranges[-period:]) / period
        return round(atr, 2)

    @staticmethod
    def calculate_ma(prices: List[float], period: int) -> float:
        """计算MA"""
        if len(prices) < period:
            return prices[-1] if prices else 0
        return sum(prices[-period:]) / period

# ========== 做空分析 ==========
def get_ath_price(symbol: str) -> Dict:
    """获取历史高点(ALL-TIME HIGH)"""
    try:
        # 用1D K线取最大范围(limit=500,约2年)
        r = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                        params={'symbol': symbol, 'interval': '1d', 'limit': 500}, timeout=10)
        klines = r.json()
        if not klines:
            return {'ath': 0, 'ath_pct': 0}

        highs = [float(k[2]) for k in klines]  # 最高价
        ath = max(highs)

        # 当前价格
        current = float(klines[-1][4])  # 收盘价

        # 距离ATH百分比
        ath_pct = ((current - ath) / ath * 100) if ath > 0 else 0

        return {'ath': round(ath, 6), 'ath_pct': round(ath_pct, 3), 'current': current}
    except Exception as e:
        return {'ath': 0, 'ath_pct': 0}

def get_recent_highs(symbol: str, periods: List[int] = [7, 30]) -> Dict:
    """获取近期高点(7天、30天)"""
    result = {}
    for days in periods:
        try:
            limit = min(days * 24, 500)  # 1h周期,最多500根
            r = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                            params={'symbol': symbol, 'interval': '1h', 'limit': limit}, timeout=10)
            klines = r.json()
            if klines:
                highs = [float(k[2]) for k in klines]
                recent_high = max(highs)
                current = float(klines[-1][4])
                pct_from_high = ((current - recent_high) / recent_high * 100) if recent_high > 0 else 0
                result[f'{days}d_high'] = round(recent_high, 6)
                result[f'{days}d_pct'] = round(pct_from_high, 3)
        except Exception:
            result[f'{days}d_high'] = 0
            result[f'{days}d_pct'] = 0
    return result

def detect_reversal_signals(symbol: str) -> Dict:
    """检测见顶回落信号(5m+30m+1h+4h)"""
    _empty = {'score': 0, 'reasons': [], 'rsi_14': 50, 'rsi_7': 50, 'rsi_5m': 50,
              'rsi_1h': 50, 'rsi_4h': 50,
              'ma5_dev': 0, 'ma20_dev': 0, 'vol_ratio': 1, 'waterfall': False,
              'macd_dead_cross_30m': False, 'macd_dead_cross_1h': False, 'macd_dead_cross_4h': False,
              'multi_rsi_overbought': 0}
    try:
        klines_5m = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                  params={'symbol': symbol, 'interval': '5m', 'limit': 20}, timeout=10).json()
        klines_30m = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                  params={'symbol': symbol, 'interval': '30m', 'limit': 10}, timeout=10).json()
        klines_1h = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                 params={'symbol': symbol, 'interval': '1h', 'limit': 10}, timeout=10).json()
        klines_4h = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                 params={'symbol': symbol, 'interval': '4h', 'limit': 6}, timeout=10).json()

        if len(klines_5m) < 10 or len(klines_30m) < 5 or len(klines_1h) < 5 or len(klines_4h) < 3:
            return _empty

        closes_5m = [float(k[4]) for k in klines_5m]
        closes_30m = [float(k[4]) for k in klines_30m]
        closes_1h = [float(k[4]) for k in klines_1h]
        closes_4h = [float(k[4]) for k in klines_4h]
        volumes_5m = [float(k[5]) for k in klines_5m]

        rsi_14 = TechnicalIndicators.calculate_rsi(closes_30m, 14)
        rsi_7 = TechnicalIndicators.calculate_rsi(closes_30m, 7)
        rsi_5m = TechnicalIndicators.calculate_rsi(closes_5m, 14)
        rsi_1h = TechnicalIndicators.calculate_rsi(closes_1h, 14)
        rsi_4h = TechnicalIndicators.calculate_rsi(closes_4h, 14)

        # MACD 多周期
        macd_30m = TechnicalIndicators.calculate_macd(closes_30m)
        macd_1h = TechnicalIndicators.calculate_macd(closes_1h)
        macd_4h = TechnicalIndicators.calculate_macd(closes_4h)

        # 短期均线偏离
        ma5 = TechnicalIndicators.calculate_ma(closes_30m, 5)
        ma20 = TechnicalIndicators.calculate_ma(closes_30m, 20)
        current = closes_30m[-1]

        ma5_dev = ((current - ma5) / ma5 * 100) if ma5 > 0 else 0
        ma20_dev = ((current - ma20) / ma20 * 100) if ma20 > 0 else 0

        # 成交量衰竭检测(最近5根 vs 前5根)
        recent_vol = sum(volumes_5m[-5:]) / 5
        prev_vol = sum(volumes_5m[-10:-5]) / 5
        vol_ratio = recent_vol / prev_vol if prev_vol > 0 else 1

        # 多周期RSI共振超买
        multi_rsi_overbought = (rsi_14 > 70) + (rsi_7 > 75) + (rsi_5m > 70) + (rsi_1h > 70) + (rsi_4h > 70)

        # 瀑布信号:最近3根K线收盘价连续下降
        last_3 = closes_30m[-3:]
        waterfall = all(last_3[i] > last_3[i+1] for i in range(2))

        # MACD死叉信号(多周期确认)
        macd_dead_cross_30m = macd_30m['macd'] < macd_30m['signal'] and macd_30m['histogram'] < 0
        macd_dead_cross_1h = macd_1h['macd'] < macd_1h['signal'] and macd_1h['histogram'] < 0
        macd_dead_cross_4h = macd_4h['macd'] < macd_4h['signal'] and macd_4h['histogram'] < 0

        # 综合做空评分
        score = 0
        reasons = []

        if rsi_14 > 70:
            score += 25
            reasons.append(f'RSI14超买({rsi_14})')
        if rsi_7 > 75:
            score += 20
            reasons.append(f'RSI7极超买({rsi_7})')
        if ma5_dev > 15:
            score += 15
            reasons.append(f'MA5偏离+{ma5_dev:.1f}%')
        if ma20_dev > 20:
            score += 15
            reasons.append(f'MA20偏离+{ma20_dev:.1f}%')
        if vol_ratio < 0.7:
            score += 15
            reasons.append(f'量能萎缩({vol_ratio:.2f}x)')
        if waterfall:
            score += 15
            reasons.append('K线瀑布')
        if macd_dead_cross_30m or macd_dead_cross_1h or macd_dead_cross_4h:
            score += 10
            reasons.append('MACD死叉')
        if rsi_5m > 70:
            score += 10
            reasons.append(f'5m_RSI超买')
        if rsi_1h > 70:
            score += 10
            reasons.append(f'1h_RSI超买')
        if rsi_4h > 70:
            score += 10
            reasons.append(f'4h_RSI超买')

        return {
            'score': score,
            'reasons': reasons,
            'rsi_14': rsi_14,
            'rsi_7': rsi_7,
            'rsi_5m': rsi_5m,
            'rsi_1h': rsi_1h,
            'rsi_4h': rsi_4h,
            'ma5_dev': round(ma5_dev, 2),
            'ma20_dev': round(ma20_dev, 2),
            'vol_ratio': round(vol_ratio, 2),
            'waterfall': waterfall,
            'macd_dead_cross_30m': macd_dead_cross_30m,
            'macd_dead_cross_1h': macd_dead_cross_1h,
            'macd_dead_cross_4h': macd_dead_cross_4h,
            'multi_rsi_overbought': multi_rsi_overbought
        }
    except Exception as e:
        return {'score': 0, 'reasons': [], 'rsi_14': 50, 'rsi_7': 50, 'rsi_5m': 50,
                'rsi_1h': 50, 'rsi_4h': 50,
                'ma5_dev': 0, 'ma20_dev': 0, 'vol_ratio': 1, 'waterfall': False,
                'macd_dead_cross_30m': False, 'macd_dead_cross_1h': False, 'macd_dead_cross_4h': False,
                'multi_rsi_overbought': 0}

# ========== LLM 分析模块 ==========
def format_for_llm(symbol: str, action: str = "open") -> str:
    """格式化 K 线原始数据供 LLM 分析(无指标、无方向提示)"""

    intervals = ["1h", "30m"]
    limits = {"1h": 12, "30m": 24}
    result = {}

    for interval in intervals:
        try:
            r = requests.get(
                f"{FAPI_URL}/fapi/v1/klines",
                params={'symbol': symbol, 'interval': interval, 'limit': limits[interval]},
                timeout=10
            )
            result[interval] = r.json()
        except Exception:
            result[interval] = []

    lines = [f"# {symbol} K线原始数据 ({action})", ""]

    for interval, klines in result.items():
        # 检查是否为空或非列表(可能是rate limit错误响应)
        if not isinstance(klines, list) or not klines:
            lines.append(f"## {interval}: 无数据\n")
            continue

        lines.append(f"## {interval} ({len(klines)} 根)")
        lines.append(f"{'时间':<20} {'开':>12} {'高':>12} {'低':>12} {'收':>12} {'成交量':>14}")
        lines.append("-" * 82)

        for k in klines:
            ts = k[0] / 1000
            dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
            o = float(k[1]); h = float(k[2]); l = float(k[3])
            c = float(k[4]); v = float(k[5])
            lines.append(f"{dt:<20} {o:>12.6f} {h:>12.6f} {l:>12.6f} {c:>12.6f} {v:>14.2f}")
        lines.append("")

    return "\n".join(lines)

def do_llm_analysis(symbol: str, action: str = "open"):
    """LLM 分析输出"""
    print(f"\n{'='*60}")
    print(f"🧠 LLM 分析数据: {symbol} ({action})")
    print(f"{'='*60}\n")

    output = format_for_llm(symbol, action)
    print(output)
    print(f"\n{'='*60}")
    print(f"📋 请复制以上数据让 LLM 分析决策")
    print(f"{'='*60}")

def _fetch_klines_multi(symbol: str, intervals: List[str] = None) -> Dict:
    """并发拉取多周期K线,返回原始数据给LLM分析"""
    if intervals is None:
        intervals = ["1h", "30m"]
    limits = {"1h": 12, "30m": 24}
    results = {}

    def _fetch(interval: str):
        try:
            r = requests.get(
                f"{FAPI_URL}/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limits.get(interval, 60)},
                timeout=10
            )
            data = r.json()
            if isinstance(data, list):
                return interval, data
        except Exception:
            pass
        return interval, []

    with ThreadPoolExecutor(max_workers=len(intervals)) as executor:
        futures = {executor.submit(_fetch, iv): iv for iv in intervals}
        for future in as_completed(futures):
            iv, klines = future.result()
            results[iv] = klines
    return results

def _format_klines_raw(klines: List, interval: str) -> str:
    """格式化K线为可读文本(原始数据,无指标)"""
    lines = []
    if not klines:
        return f"  ({interval} 无数据)"
    lines.append(f"--- {interval} ({len(klines)} bars) ---")
    lines.append(f"{'时间':<12} {'开盘':>10} {'最高':>10} {'最低':>10} {'收盘':>10} {'成交量':>14}")
    for k in klines[-20:]:
        ts = k[0] / 1000
        dt = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')
        o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4]); v = float(k[5])
        chg = (c - o) / o * 100 if o > 0 else 0
        body = "▲" if c >= o else "▼"
        lines.append(f"{dt:<12} {o:>10.5f} {h:>10.5f} {l:>10.5f} {c:>10.5f} {body}{chg:+6.2f}% {v:>12.2f}")
    return "\n".join(lines)

def scan_short_candidates(min_change: float = 10) -> List[Dict]:
    """
    扫描做空候选币种(多线程拉K线,LLM判断)
    程序只提供原始OHLCV数据,不计算任何指标
    """
    print(f"\n{'='*60}")
    print(f"📈 做空候选扫描(涨幅 >= {min_change}%,多线程拉K线)")
    print(f"{'='*60}")

    r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={"limit": 500})
    all_tickers = r.json()

    usdt_pairs = [t for t in all_tickers if isinstance(t, dict) and t.get('symbol', '').endswith('USDT')]
    sorted_tickers = sorted(usdt_pairs, key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)
    top_tickers = sorted_tickers[:20]

    candidates = []

    def process_short_symbol(ticker):
        symbol = ticker.get('symbol', '')
        change_24h = float(ticker.get('priceChangePercent', 0))
        if change_24h < min_change:
            return None
        try:
            klines_data = _fetch_klines_multi(symbol, intervals=["1h", "30m"])
            return {
                'symbol': symbol,
                'price': float(ticker.get('lastPrice', 0)),
                'change_24h': round(change_24h, 2),
                'volume_24h': float(ticker.get('quoteVolume', 0)),
                'klines': klines_data
            }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_symbol = {executor.submit(process_short_symbol, t): t for t in top_tickers}
        for future in as_completed(future_to_symbol):
            res = future.result()
            if res is not None:
                candidates.append(res)

    candidates.sort(key=lambda x: x['change_24h'], reverse=True)

    print(f"\n扫描完成,找到 {len(candidates)} 个候选做空币种(多线程拉K线)")
    print(f"\n{'='*60}")
    print(f"📊 原始数据 - 等待LLM分析(程序不计算指标)")
    print(f"{'='*60}")

    for c in candidates[:10]:
        print(f"\n{'='*60}")
        print(f"[做空候选] {c['symbol']} | 价格={c['price']:.6g} | 24h涨幅={c['change_24h']:+.2f}% | 24h成交额={c['volume_24h']:.0f}U")
        print(f"{'='*60}")
        for iv in ["1h", "30m"]:
            kl = c['klines'].get(iv, [])
            print(_format_klines_raw(kl, iv))

    print(f"\n{'='*60}")
    print("✅ 原始数据已输出,LLM请根据K线力度/成交量判断是否做空")
    print(f"{'='*60}")
    return candidates

# ========== 主程序(扫描 + 指标 + 账户操作 + 命令执行)==========

def get_all_perpetual_symbols() -> List[str]:
    """获取所有USDT永续合约"""
    r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/exchangeInfo", endpoint='exchangeInfo')
    data = r.json()

    symbols = []
    for s in data.get('symbols', []):
        if (s.get('contractType') == 'PERPETUAL' and
            s.get('quoteAsset') == 'USDT' and
            s.get('status') == 'TRADING'):
            symbols.append(s.get('symbol'))

    return symbols

def detect_bottom_signals(symbol: str) -> Dict:
    """检测见底反弹信号(5m+30m+1h+4h)"""
    _empty = {'score': 0, 'reasons': [], 'rsi_14': 50, 'rsi_7': 50, 'rsi_5m': 50,
              'rsi_1h': 50, 'rsi_4h': 50,
              'ma5_dev': 0, 'ma20_dev': 0, 'vol_ratio': 1, 'rebound': False,
              'macd_golden_cross_30m': False, 'macd_golden_cross_1h': False, 'macd_golden_cross_4h': False,
              'multi_rsi_oversold': 0}
    try:
        klines_5m = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                  params={'symbol': symbol, 'interval': '5m', 'limit': 20}, timeout=10).json()
        klines_30m = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                   params={'symbol': symbol, 'interval': '30m', 'limit': 10}, timeout=10).json()
        klines_1h = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                  params={'symbol': symbol, 'interval': '1h', 'limit': 10}, timeout=10).json()
        klines_4h = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                  params={'symbol': symbol, 'interval': '4h', 'limit': 6}, timeout=10).json()

        if len(klines_5m) < 10 or len(klines_30m) < 5 or len(klines_1h) < 5 or len(klines_4h) < 3:
            return _empty

        closes_5m = [float(k[4]) for k in klines_5m]
        closes_30m = [float(k[4]) for k in klines_30m]
        closes_1h = [float(k[4]) for k in klines_1h]
        closes_4h = [float(k[4]) for k in klines_4h]
        volumes_5m = [float(k[5]) for k in klines_5m]

        rsi_14 = TechnicalIndicators.calculate_rsi(closes_30m, 14)
        rsi_7 = TechnicalIndicators.calculate_rsi(closes_30m, 7)
        rsi_5m = TechnicalIndicators.calculate_rsi(closes_5m, 14)
        rsi_1h = TechnicalIndicators.calculate_rsi(closes_1h, 14)
        rsi_4h = TechnicalIndicators.calculate_rsi(closes_4h, 14)

        macd_30m = TechnicalIndicators.calculate_macd(closes_30m)
        macd_1h = TechnicalIndicators.calculate_macd(closes_1h)
        macd_4h = TechnicalIndicators.calculate_macd(closes_4h)
        ma5 = TechnicalIndicators.calculate_ma(closes_30m, 5)
        ma20 = TechnicalIndicators.calculate_ma(closes_30m, 20)
        current = closes_30m[-1]

        ma5_dev = ((current - ma5) / ma5 * 100) if ma5 > 0 else 0
        ma20_dev = ((current - ma20) / ma20 * 100) if ma20 > 0 else 0

        recent_vol = sum(volumes_5m[-5:]) / 5
        prev_vol = sum(volumes_5m[-10:-5]) / 5
        vol_ratio = recent_vol / prev_vol if prev_vol > 0 else 1

        # 瀑布反弹信号:最近3根K线收盘价连续上升
        last_3 = closes_30m[-3:]
        rebound = all(last_3[i] < last_3[i+1] for i in range(2))

        # MACD金叉信号(多周期确认)
        macd_golden_cross_30m = macd_30m['macd'] > macd_30m['signal'] and macd_30m['histogram'] > 0
        macd_golden_cross_1h = macd_1h['macd'] > macd_1h['signal'] and macd_1h['histogram'] > 0
        macd_golden_cross_4h = macd_4h['macd'] > macd_4h['signal'] and macd_4h['histogram'] > 0

        # 多周期RSI共振超卖
        multi_rsi_oversold = (rsi_14 < 30) + (rsi_7 < 25) + (rsi_5m < 30) + (rsi_1h < 30) + (rsi_4h < 30)

        score = 0
        reasons = []

        if rsi_14 < 30:
            score += 25
            reasons.append(f'RSI14超卖({rsi_14:.1f})')
        if rsi_7 < 25:
            score += 20
            reasons.append(f'RSI7极超卖({rsi_7:.1f})')
        if ma5_dev < -15:
            score += 15
            reasons.append(f'MA5偏离{ma5_dev:.1f}%')
        if ma20_dev < -20:
            score += 15
            reasons.append(f'MA20偏离{ma20_dev:.1f}%')
        if vol_ratio > 1.3:
            score += 15
            reasons.append(f'量能放大({vol_ratio:.2f}x)')
        if rebound:
            score += 15
            reasons.append('K线反弹')
        if macd_golden_cross_30m or macd_golden_cross_1h or macd_golden_cross_4h:
            score += 10
            reasons.append('MACD金叉')
        if rsi_5m < 30:
            score += 10
            reasons.append(f'5m_RSI超卖')
        if rsi_1h < 30:
            score += 10
            reasons.append(f'1h_RSI超卖')
        if rsi_4h < 30:
            score += 10
            reasons.append(f'4h_RSI超卖')

        return {
            'score': score,
            'reasons': reasons,
            'rsi_14': rsi_14,
            'rsi_7': rsi_7,
            'rsi_5m': rsi_5m,
            'rsi_1h': rsi_1h,
            'rsi_4h': rsi_4h,
            'ma5_dev': round(ma5_dev, 2),
            'ma20_dev': round(ma20_dev, 2),
            'vol_ratio': round(vol_ratio, 2),
            'rebound': rebound,
            'macd_golden_cross_30m': macd_golden_cross_30m,
            'macd_golden_cross_1h': macd_golden_cross_1h,
            'macd_golden_cross_4h': macd_golden_cross_4h,
            'multi_rsi_oversold': multi_rsi_oversold
        }
    except Exception as e:
        return {'score': 0, 'reasons': [], 'rsi_14': 50, 'rsi_7': 50, 'rsi_5m': 50,
                'rsi_1h': 50, 'rsi_4h': 50,
                'ma5_dev': 0, 'ma20_dev': 0, 'vol_ratio': 1, 'rebound': False,
                'macd_golden_cross_30m': False, 'macd_golden_cross_1h': False, 'macd_golden_cross_4h': False,
                'multi_rsi_oversold': 0}

def scan_long_candidates(min_change: float = -10, max_change: float = -3) -> List[Dict]:
    """
    扫描做多候选币种(多线程拉K线,LLM判断)
    程序只提供原始OHLCV数据,不计算任何指标
    """
    print(f"\n{'='*60}")
    print(f"📉 做多候选扫描(跌幅 {max_change}% ~ {min_change}%,多线程拉K线)")
    print(f"{'='*60}")

    r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={"limit": 500})
    all_tickers = r.json()

    usdt_pairs = [t for t in all_tickers if isinstance(t, dict) and t.get('symbol', '').endswith('USDT')]
    sorted_tickers = sorted(usdt_pairs, key=lambda x: float(x.get('priceChangePercent', 0)))

    # 初筛跌幅范围内的币
    quick_candidates = []
    for ticker in sorted_tickers:
        symbol = ticker.get('symbol', '')
        change_24h = float(ticker.get('priceChangePercent', 0))
        if min_change <= change_24h <= max_change:
            quick_candidates.append(ticker)

    quick_candidates = quick_candidates[:50]
    print(f"初筛找到 {len(quick_candidates)} 个候选,开始多线程拉K线...")

    def process_long_symbol(ticker):
        symbol = ticker.get('symbol', '')
        try:
            klines_data = _fetch_klines_multi(symbol)
            return {
                'symbol': symbol,
                'price': float(ticker.get('lastPrice', 0)),
                'change_24h': round(float(ticker.get('priceChangePercent', 0)), 2),
                'volume_24h': float(ticker.get('quoteVolume', 0)),
                'klines': klines_data
            }
        except Exception:
            return None

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_t = {executor.submit(process_long_symbol, t): t for t in quick_candidates}
        for future in as_completed(future_to_t):
            res = future.result()
            if res is not None:
                results.append(res)

    results.sort(key=lambda x: x['change_24h'])

    print(f"\n扫描完成,找到 {len(results)} 个候选做多币种(多线程拉K线)")
    print(f"\n{'='*60}")
    print(f"📊 原始数据 - 等待LLM分析(程序不计算指标)")
    print(f"{'='*60}")

    for c in results[:10]:
        print(f"\n{'='*60}")
        print(f"[做多候选] {c['symbol']} | 价格={c['price']:.6g} | 24h跌幅={c['change_24h']:+.2f}% | 24h成交额={c['volume_24h']:.0f}U")
        print(f"{'='*60}")
        for iv in ["5m", "30m", "1h", "4h"]:
            kl = c['klines'].get(iv, [])
            print(_format_klines_raw(kl, iv))

    print(f"\n{'='*60}")
    print("✅ 原始数据已输出,LLM请根据K线力度/成交量判断是否做多")
    print(f"{'='*60}")
    return results

def scan_candidates(min_change: float = 10) -> List[Dict]:
    """扫描涨幅超过门槛的币种(只获取10个)"""
    print(f"\n{'='*60}")
    print(f"🔍 扫描候选币种(涨幅 >= {min_change}%,只分析10个)")
    print(f"{'='*60}")

    r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={"limit": 10})
    all_tickers = r.json()
    print(f"总币种数: {len(all_tickers)}")

    candidates = []
    for ticker in all_tickers:
        if isinstance(ticker, dict):
            symbol = ticker.get('symbol', '')
            if not symbol.endswith('USDT'):
                continue
            price_change = float(ticker.get('priceChangePercent', 0))
            if price_change >= min_change:
                candidates.append({
                    'symbol': symbol,
                    'price': float(ticker.get('lastPrice', 0)),
                    'change': price_change,
                    'volume': float(ticker.get('quoteVolume', 0))
                })

    candidates.sort(key=lambda x: x['change'], reverse=True)
    print(f"\n找到 {len(candidates)} 个候选币种")
    for i, c in enumerate(candidates[:10]):
        print(f"  {i+1}. {c['symbol']}: ${c['price']:.4f} (+{c['change']:.2f}%)")
    return candidates

# ========== 统一波动率扫描(多空一起获取)==========

# ========== 速率限制 ==========
# Binance Futures API Rate Limits (futures/usdel撮合):
#   - 1200 weight / minute (REQUEST + ORDER combined)
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
    'um_order':          (5,   120),
    'default':            (5,   120),
}

class RateLimiter:
    """滑动窗口速率限制器 - 按权重计数"""
    def __init__(self, max_weight_per_sec: int = 60, max_weight_per_min: int = 1200):
        self.max_weight_per_sec = max_weight_per_sec
        self.max_weight_per_min = max_weight_per_min
        self._sec_timestamps = []  # [timestamp, ...] last-second window
        self._min_timestamps = []  # [timestamp, ...] last-minute window
        self._lock = __import__('threading').Lock()

    def acquire(self, weight: int, wait: bool = True, timeout: float = 30.0) -> bool:
        """请求 weight 单位,必要时阻塞等待配额返回"""
        start = time.time()
        while True:
            with self._lock:
                now = time.time()
                # 清理过期戳 (timestamps = list of (time, weight))
                self._sec_timestamps = [(t, w) for t, w in self._sec_timestamps if now - t < 1.0]
                self._min_timestamps = [(t, w) for t, w in self._min_timestamps if now - t < 60.0]
                sec_w = sum(w for _, w in self._sec_timestamps)
                min_w = sum(w for _, w in self._min_timestamps)
                if (sec_w + weight <= self.max_weight_per_sec and
                    min_w + weight <= self.max_weight_per_min):
                    self._sec_timestamps.append((now, weight))
                    self._min_timestamps.append((now, weight))
                    return True
            if not wait:
                return False
            # 等待最近一个请求过期(优先等秒级)
            with self._lock:
                if self._sec_timestamps:
                    oldest_sec = min(t for t, _ in self._sec_timestamps)
                    sleep_sec = 1.0 - (now - oldest_sec)
                elif self._min_timestamps:
                    oldest_min = min(t for t, _ in self._min_timestamps)
                    sleep_sec = 60.0 - (now - oldest_min)
                else:
                    sleep_sec = 0.05
            sleep_sec = max(0.01, min(sleep_sec, timeout - (time.time() - start)))
            if sleep_sec <= 0:
                break
            time.sleep(sleep_sec)
            if time.time() - start > timeout:
                return False
        return False

    def remaining(self) -> tuple:
        """返回 (sec_remaining, min_remaining) 权重配额"""
        with self._lock:
            now = time.time()
            sec_w = sum(w for t, w in self._sec_timestamps if now - t < 1.0)
            min_w = sum(w for t, w in self._min_timestamps if now - t < 60.0)
            return (max(0, self.max_weight_per_sec - sec_w),
                    max(0, self.max_weight_per_min - min_w))


_rl = RateLimiter()


def _binance_weight(endpoint: str, method: str = 'GET') -> int:
    """根据 endpoint 估算请求权重"""
    ep = endpoint.strip('/').split('/')[-1]  # e.g. 'fapi/v1/klines' -> 'klines'
    for key in _WEIGHT_CONFIG:
        if key in ep:
            return _WEIGHT_CONFIG[key][0]
    return _WEIGHT_CONFIG['default'][0]


def _rl_request(method: str, url: str, endpoint: str = '', **kwargs) -> requests.Response:
    """带速率限制的 requests 封装 - 自动扣权重,遇到限速自动退让"""
    weight = _binance_weight(endpoint or url)
    kwargs.setdefault('timeout', 30 if method == 'GET' else 10)
    if not _rl.acquire(weight, wait=True, timeout=30.0):
        raise Exception(f"Rate limit timeout after 30s (weight={weight}) for {endpoint or url}")
    last_err = None
    for attempt in range(3):
        try:
            r = requests.request(method, url, **kwargs)
            # 遇到限速(429/418)- 等待一段时间后重试
            if r.status_code in (418, 429) or (r.status_code == 418):
                wait_sec = (attempt + 1) * 2  # 2s, 4s, 6s
                print(f"  ⚠️ Rate limited ({r.status_code}) for {endpoint}, waiting {wait_sec}s...", file=sys.stderr)
                time.sleep(wait_sec)
                continue
            return r
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
            continue
    raise last_err


def _rate_limit():
    """旧兼容函数 - 扣1个默认权重"""
    _rl.acquire(_WEIGHT_CONFIG['default'][0])


def _get_klines_raw(symbol: str, interval: str, limit: int) -> List:
    """Get raw klines without computing indicators (with retry, PM-safe)"""
    _rate_limit()
    # PM账户优先papi公开端点,降级fapi
    for attempt in range(3):  # 最多重试3次
        try:
            # 优先 papi(PM账户无需签名)
            r = requests.get(
                f"{PAPI_URL}/papi/v1/um/klines",
                params={'symbol': symbol, 'interval': interval, 'limit': limit},
                timeout=10,
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
            r = requests.get(
                f"{FAPI_URL}/fapi/v1/klines",
                params={'symbol': symbol, 'interval': interval, 'limit': limit},
                timeout=10,
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
            r = requests.get(
                f"{FAPI_URL}/fapi/v1/exchangeInfo",
                timeout=10,
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
            r = requests.get(
                f"{PAPI_URL}/papi/v1/um/exchangeInfo",
                params={'symbol': symbol},
                timeout=10,
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
            r = requests.get(
                f"{FAPI_URL}/fapi/v1/exchangeInfo",
                timeout=10,
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
            r = requests.get(
                f"{PAPI_URL}/papi/v1/um/exchangeInfo",
                params={'symbol': symbol},
                timeout=10,
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


def _get_atr_and_volatility(symbol: str) -> Dict:
    """获取 ATR 和波动率
    
    Returns:
        {'atr': float, 'atr_percent': float, 'volatility': 'low'|'medium'|'high'}
        - atr_percent: ATR占当前价格百分比
        - volatility: low(<0.5%), medium(0.5%-3%), high(>3%)
    """
    try:
        r = requests.get(
            f"{FAPI_URL}/fapi/v1/klines",
            params={'symbol': symbol, 'interval': '1h', 'limit': 60},
            timeout=10,
            verify=certifi.where()
        )
        klines = r.json()
        if not klines or not isinstance(klines, list):
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
    except Exception as e:
        print(f"  ⚠️ ATR计算失败: {e}", file=sys.stderr)
        return {'atr': 0, 'atr_percent': 0, 'volatility': 'medium'}


def _get_dynamic_position_size(balance: float, price: float, atr_percent: float, leverage: int = 10) -> float:
    """根据波动率计算动态仓位
    
    Args:
        balance: 账户余额
        price: 当前价格
        atr_percent: ATR占价格百分比
        leverage: 杠杆倍数
    
    Returns:
        margin: 建议开仓保证金 USDT
    """
    # 基础仓位: 20% 余额
    base_margin = balance * 0.20
    
    # 根据波动率调整
    if atr_percent < 0.5:
        # 低波动: 可以多开一点，但不超过 20%
        margin = base_margin
    elif atr_percent > 3.0:
        # 高波动: 只开 10%
        margin = balance * 0.10
    else:
        # 中等波动: 15%
        margin = balance * 0.15
    
    return margin


def _calculate_stop_loss(entry_price: float, atr: float, atr_percent: float, side: str) -> Dict:
    """计算止损和止盈价格
    
    Args:
        entry_price: 入场价格
        atr: ATR 绝对值
        atr_percent: ATR 占价格百分比
        side: 'LONG' 或 'SHORT'
    
    Returns:
        {'sl_trigger': float, 'tp_trigger': float, 'sl_percent': float, 'tp_percent': float}
    """
    # v10 规则: 优先 ATR，止损 = 1.5x ATR
    # 如果 ATR 太小(低波动币种)，用固定的 2%
    if atr_percent > 0.3:
        sl_pct = atr_percent * 1.5  # ATR 的 1.5 倍
    else:
        sl_pct = 2.0  # 固定 2%
    
    tp_pct = sl_pct * 1.5  # 止盈 = 止损的 1.5 倍 (R:R = 1:1.5)
    
    if side.upper() == 'LONG':
        sl_trigger = entry_price * (1 - sl_pct / 100)
        tp_trigger = entry_price * (1 + tp_pct / 100)
    else:  # SHORT
        sl_trigger = entry_price * (1 + sl_pct / 100)
        tp_trigger = entry_price * (1 - tp_pct / 100)
    
    return {
        'sl_trigger': sl_trigger,
        'tp_trigger': tp_trigger,
        'sl_percent': sl_pct,
        'tp_percent': tp_pct
    }


def _check_cooldown() -> bool:
    """检查 cooldown 状态
    
    Returns:
        True: 可以交易 (不在 cooldown 中)
        False: 在 cooldown 中，禁止交易
    """
    import os
    cooldown_file = os.path.join(os.path.dirname(__file__), '.cooldown.json')
    if not os.path.exists(cooldown_file):
        return True
    
    try:
        with open(cooldown_file) as f:
            data = json.load(f)
        
        # 检查是否在 cooldown 中
        if data.get('in_cooldown'):
            cooldown_end = data.get('cooldown_end', 0)
            import time
            now = time.time()
            if now < cooldown_end:
                remaining = int(cooldown_end - now)
                print(f"⏸️ Cooldown 中，剩余 {remaining//60}m {remaining%60}s", file=sys.stderr)
                return False
            else:
                # cooldown 结束，移除标记
                data['in_cooldown'] = False
                with open(cooldown_file, 'w') as f:
                    json.dump(data, f)
                return True
    except Exception:
        pass
    return True


def _trigger_cooldown(reason: str = "连亏2笔"):
    """触发 cooldown"""
    import os, time
    cooldown_file = os.path.join(os.path.dirname(__file__), '.cooldown.json')
    cooldown_duration = 3600  # 1小时
    
    data = {
        'in_cooldown': True,
        'cooldown_end': time.time() + cooldown_duration,
        'reason': reason,
        'triggered_at': time.time()
    }
    with open(cooldown_file, 'w') as f:
        json.dump(data, f)
    print(f"⏸️ Cooldown 已触发: {reason}, 持续1小时", file=sys.stderr)


def _check_and_update_consecutive_losses():
    """检查并更新连亏状态，触发 cooldown"""
    import os
    journal_file = os.path.join(os.path.dirname(__file__), 'journal.json')
    if not os.path.exists(journal_file):
        return
    
    try:
        with open(journal_file) as f:
            journal = json.load(f)
        if not journal:
            return
        
        # 获取最近的交易
        recent = journal[-10:] if len(journal) > 10 else journal
        
        # 统计连续亏损
        losses = 0
        for t in reversed(recent):
            pnl = t.get('pnl', 0)
            if pnl < 0:
                losses += 1
            else:
                break
        
        # 如果连亏>=2笔，触发 cooldown
        if losses >= 2:
            _trigger_cooldown(f"连亏{losses}笔")
    except Exception:
        pass


def _round_qty_to_step(qty: float, symbol: str) -> float:
    """将数量对齐到 stepSize 精度(避免 QTY precision 错误)"""
    for attempt in range(3):
        try:
            r = requests.get(
                f"{FAPI_URL}/fapi/v1/exchangeInfo",
                timeout=10,
                verify=certifi.where()
            )
            if r.status_code != 200:
                raise Exception(f"status {r.status_code}")
            data = r.json()
            sym_data = next((s for s in data.get('symbols', []) if s.get('symbol') == symbol), None)
            if not sym_data:
                raise Exception(f"symbol {symbol} not in exchangeInfo")
            for f in sym_data.get('filters', []):
                if f.get('filterType') == 'LOT_SIZE':
                    step_str = f['stepSize']
                    step = float(step_str)
                    decimals = 0
                    if '.' in step_str:
                        decimals = len(step_str.rstrip('0').split('.')[1])
                    raw = round(qty / step) * step
                    return float(f"{raw:.{decimals}f}")
            break
        except Exception:
            pass
        time.sleep(0.3 * (attempt + 1))
    # 兜底
    return float(f"{qty:.1f}")


def _detect_price_pattern(klines: List, interval: str) -> Dict:
    """检测K线形态: 找线->等突破->看回踩 入场信号

    入场三步曲:
    1. 找线: 识别关键支撑/压力位 (最近20根K线的高低点)
    2. 等突破: 大阳线(实体>1.5%且放量>1.5倍均量)向上突破关键位
    3. 看回踩: 回踩关键位，缩量，不跌破，入场做多

    平仓四步曲(检测到反向信号):
    1. 拉高: 大阳线快速拉高
    2. 洗盘: 大阴线快速下跌
    3. 反包: 再次拉高形成反包
    4. 阴跌: 连续小阴线无反弹

    Returns:
        dict: {
          'pattern': str,        # 信号类型
          'signal': str,         # 'LONG'|'SHORT'|'CLOSE'|'WATCH'
          'signal_emoji': str,
          'reason': str,
          'strength': float,     # 1.0~3.0 信号强度
          'key_level': float,    # 关键位价格
          'level_type': str,     # 'support'|'resistance'
        }
    """
    if not klines or len(klines) < 5:
        return {'pattern': 'EMPTY', 'signal': 'WATCH', 'signal_emoji': '⬜',
                'reason': '数据不足', 'strength': 0, 'key_level': 0, 'level_type': 'none'}

    closes = [float(k[4]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    vols   = [float(k[5]) for k in klines]

    current = closes[-1]
    current_vol = vols[-1]
    avg_vol = sum(vols[-5:]) / 5 if len(vols) >= 5 else sum(vols) / max(len(vols), 1)
    period_high  = max(highs)
    period_low  = min(lows)
    range_size  = period_high - period_low

    def body_pct(k):
        o,c = float(k[1]), float(k[4])
        return abs(c - o) / o * 100 if o > 0 else 0
    def is_bullish(k): return float(k[4]) > float(k[1])
    def is_bearish(k): return float(k[4]) < float(k[1])
    def vol_spike(k, avg): return float(k[5]) / avg if avg > 0 else 1.0

    # ---- 平仓信号: 拉高->洗盘->反包->阴跌 ----
    if len(klines) >= 3:
        k_n2 = klines[-3]
        k_n1 = klines[-2]
        k_0  = klines[-1]
        pull_up = body_pct(k_n2) > 1.5 and is_bullish(k_n2) and vol_spike(k_n2, avg_vol) > 1.2
        shake  = body_pct(k_n1) > 1.5 and is_bearish(k_n1) and vol_spike(k_n1, avg_vol) > 1.2
        rev    = body_pct(k_0) > 1.0 and is_bullish(k_0) and float(k_0[4]) > float(k_n1[1])
        if pull_up and shake and rev:
            return {'pattern': 'DIST_REV', 'signal': 'CLOSE', 'signal_emoji': '🚪',
                    'reason': '拉高诱多→洗盘→反包,主力派发,离场', 'strength': 3.0,
                    'key_level': highs[-1], 'level_type': 'resistance'}
        if len(klines) >= 4:
            slow_leak = (is_bearish(k_n2) and body_pct(k_n2) < 0.8 and
                        is_bearish(k_n1) and body_pct(k_n1) < 0.8 and
                        is_bearish(k_0)  and body_pct(k_0)  < 0.8)
            if slow_leak and closes[-1] < closes[-3]:
                return {'pattern': 'SLOW_LEAK', 'signal': 'CLOSE', 'signal_emoji': '🚪',
                        'reason': '连续小阴线阴跌,主力派发,离场', 'strength': 2.5,
                        'key_level': period_low, 'level_type': 'support'}

    # ---- 入场信号: 突破->回踩->确认 ----
    if len(klines) >= 4:
        k_3 = klines[-4]; k_2 = klines[-3]; k_1 = klines[-2]; k_0 = klines[-1]
        consol   = body_pct(k_3) < 1.0
        breakout = body_pct(k_2) > 1.5 and is_bullish(k_2) and vol_spike(k_2, avg_vol) > 1.5
        pullback = body_pct(k_1) < 1.0
        vol_dry  = vol_spike(k_1, avg_vol) < 0.6
        confirm  = is_bullish(k_0) and closes[-1] > closes[-3]
        if consol and breakout and pullback and vol_dry and confirm:
            return {'pattern': 'BREAKOUT_PULLBACK', 'signal': 'LONG', 'signal_emoji': '🟢',
                    'reason': f'突破{body_pct(k_2):.1f}%+{vol_spike(k_2,avg_vol):.1f}x量→缩量回踩{vol_spike(k_1,avg_vol):.2f}x→确认入场',
                    'strength': 3.0, 'key_level': highs[-3], 'level_type': 'resistance'}
        if breakout and not (pullback and vol_dry):
            return {'pattern': 'BREAKOUT_WAIT', 'signal': 'WATCH', 'signal_emoji': '🟡',
                    'reason': f'刚突破{body_pct(k_2):.1f}%+{vol_spike(k_2,avg_vol):.1f}x量,等回踩确认',
                    'strength': 2.0, 'key_level': highs[-3], 'level_type': 'resistance'}

    # ---- 做空信号: 向下突破->反抽->确认 ----
    if len(klines) >= 4:
        k_3 = klines[-4]; k_2 = klines[-3]; k_1 = klines[-2]; k_0 = klines[-1]
        break_down = body_pct(k_2) > 1.5 and is_bearish(k_2) and vol_spike(k_2, avg_vol) > 1.5
        pull_up_s  = body_pct(k_1) < 1.0 and vol_spike(k_1, avg_vol) < 0.6 and float(k_1[4]) < float(k_3[1])
        confirm_s  = is_bearish(k_0) and closes[-1] < closes[-3]
        if break_down and pull_up_s and confirm_s:
            return {'pattern': 'BREAKDOWN_PULLBACK', 'signal': 'SHORT', 'signal_emoji': '🔵',
                    'reason': f'向下突破{body_pct(k_2):.1f}%+{vol_spike(k_2,avg_vol):.1f}x量→缩量反抽确认做空',
                    'strength': 3.0, 'key_level': lows[-3], 'level_type': 'support'}

    return {'pattern': 'NONE', 'signal': 'WATCH', 'signal_emoji': '⬜',
            'reason': '无信号,等待', 'strength': 0, 'key_level': 0, 'level_type': 'none'}



def _format_klines_for_llm(klines: List, interval: str) -> str:
    """Format klines into LLM-readable text with strategy signal annotations"""
    lines = []
    if not klines:
        return ""
    lines.append(f"## {interval} ({len(klines)} bars)")
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
    for k in klines[-15:]:
        ts = k[0] / 1000
        dt = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')
        o = float(k[1]); c = float(k[4]); h = float(k[2]); l = float(k[3]); v = float(k[5])
        chg = (c - o) / o * 100 if o > 0 else 0
        body = "▲" if c > o else "▼"
        upper_shadow = h - max(o, c)
        lower_shadow = min(o, c) - l
        lines.append(f"  {dt} O:{o:.5f} C:{c:.5f} H:{h:.5f} L:{l:.5f} {body}{chg:+.2f}% U:{upper_shadow:.5f} L:{lower_shadow:.5f} V:{v:.0f}")
    lines.append("")
    return "\n".join(lines)

def llm_analyze_batch(coins: List[Dict]) -> Dict[str, Dict]:
    """
    打印原始K线数据供LLM分析,程序不做任何计算
    """
    for c in coins:
        sym = c['symbol']
        price = c['price']
        change = c['change_24h']
        vol = c['vol_24h_pct']
        high = c['high_24h']
        low = c['low_24h']
        pos = ((price - low) / (high - low) * 100) if (high - low) > 0 else 50

        print(f"[COIN] {sym} | Price={price} | 24h={change:+.2f}% | Vol={vol}% | Pos={pos:.0f}%")

        for interval, key in [('1h', 'klines_1h'), ('30m', 'klines_30m')]:
            kls = c.get(key, [])
            if kls:
                print(_format_klines_for_llm(kls, interval))
        print()
    return {}

def scan_volatility_top(top_n: int = 10, min_vol: float = 3.0, top_klines: int = 30) -> List[Dict]:
    """
    Unified scan: Binance sortBy server-side ranking
    Get coins, fetch klines, hand to LLM for unified LONG/SHORT analysis

    过滤逻辑:
    1. 只取 status=TRADING 的币种
    2. 排除 blacklist.json 黑名单
    3. 过滤 minNotional < 50U 的币(可交易)
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
                btc_r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/price", endpoint='ticker/price', params={"symbol": "BTCUSDT"}, timeout=10)
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
        if volume < 1_000_000:
            continue
        vol_pct = (high - low) / price * 100
        if vol_pct < min_vol:
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

    candidates.sort(key=lambda x: x['vol_24h_pct'], reverse=True)
    top_vol = candidates[:top_n]

    print(f"After vol filter: {len(candidates)} >= {min_vol}% | 取前{top_n}名 | K线候选{top_klines}个")
    print(f"\nFetching klines for all coins (rate-limit: 0.1s/req)...\n")

    # 批量获取K线(1h+30m),避免超限
    # top_klines 控制实际取K线的数量
    kline_coins = top_vol[:top_klines]

    def fetch_coin_klines(c):
        sym = c['symbol']
        try:
            klines_1h  = _get_klines_raw(sym, '1h',  12)
            klines_30m = _get_klines_raw(sym, '30m', 8)
            try:
                r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={'symbol': sym}, timeout=8)
                data = r.json()
                current_price = float(data.get('lastPrice', 0))
            except Exception:
                current_price = 0.0
            return sym, {
                'klines_1h':   klines_1h,
                'klines_30m':  klines_30m,
                'current_price': current_price,
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

    # LLM batch analysis
    llm_result = llm_analyze_batch(top_vol)

    for c in top_vol:
        sym = c['symbol']
        if sym in llm_result:
            c.update(llm_result[sym])
        else:
            c['direction'] = 'NEUTRAL'
            c['llm_reason'] = 'LLM no output'
            c['confidence'] = 0

    return candidates

def scan_pick_top(top_n: int = 30, min_vol_24h: float = 3000000,
                  min_volatility: float = 1.5, max_volatility: float = 30.0,
                  round1_count: int = 30) -> List[Dict]:
    """
    scan-pick: 两轮筛选

    第一轮: 从全市场 ticker/24hr 筛选:
      - quoteVolume >= min_vol_24h (默认300万U)
      - 24h波动率 [min_volatility%, max_volatility%] (默认1.5%~30%)
      - 按 quoteVolume * volatility 取前 round1_count 名

    第二轮: 直接拉1h+30m K线,按第一轮排序输出(程序不做额外筛选)
    输出: 纯原始数据(24h行情 + 1h/30m K线),程序不做任何评分/指标
    """
    print(f"\n{'='*70}")
    print(f"SCAN-PICK - 量价筛选 (top={top_n}, min_vol_24h={min_vol_24h:.0f}, volatility={min_volatility}-{max_volatility}%)")
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

    # 获取可交易币种
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
            for f in s.get('filters', []):
                if f.get('filterType') == 'MIN_NOTIONAL':
                    min_notional = float(f.get('minNotional', 0))
                    if min_notional < 50:
                        tradeable_symbols.add(sym)
                        break
    print(f"可交易币种: {len(tradeable_symbols)}")

    # 获取全部24h行情(不按涨跌幅排序,拿全市场)
    _rate_limit()
    try:
        r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={"limit": 200}, timeout=15)
        all_tickers = r.json()
        if isinstance(all_tickers, dict) and all_tickers.get('code') == -1003:
            raise Exception("rate limited")
    except Exception:
        try:
            r = _rl_request('GET', f"{PAPI_URL}/papi/v1/um/ticker/24hr", endpoint='um/ticker/24hr', params={"limit": 200}, timeout=15)
            all_tickers = r.json()
        except Exception:
            print(f"❌ ticker/24hr 获取失败")
            return []

    # === 第一轮: 基于24h行情粗筛 ===
    candidates = []
    for ticker in all_tickers:
        if not isinstance(ticker, dict):
            continue
        sym = ticker.get('symbol', '')
        if not sym.endswith('USDT'):
            continue
        if sym in blacklist:
            continue
        if sym not in tradeable_symbols:
            continue

        try:
            price = float(ticker.get('lastPrice', 0))
            high = float(ticker.get('highPrice', 0))
            low = float(ticker.get('lowPrice', 0))
            change = float(ticker.get('priceChangePercent', 0))
            volume = float(ticker.get('quoteVolume', 0))
        except (ValueError, TypeError):
            continue
        if price == 0 or low == 0:
            continue
        if volume < min_vol_24h:
            continue
        vol_pct = (high - low) / price * 100
        if vol_pct < min_volatility or vol_pct > max_volatility:
            continue

        candidates.append({
            'symbol': sym,
            'price': price,
            'change_24h': round(change, 2),
            'volume_24h': volume,
            'vol_24h_pct': round(vol_pct, 2),
            'high_24h': high,
            'low_24h': low,
        })

    # 按 成交额×波动率 取前30名进入第二轮
    candidates.sort(key=lambda x: x['volume_24h'] * x['vol_24h_pct'], reverse=True)
    round1_top = min(round1_count, len(candidates))
    round1_coins = candidates[:round1_top]
    print(f"第一轮: {len(candidates)} 通过粗筛 | 取前{round1_top}名\n")

    # === 第二轮: 直接拉1h+30m K线(按第一轮排序输出,不额外筛选) ===
    final_coins = round1_coins[:top_n]

    def _fetch_klines(c):
        sym = c['symbol']
        try:
            klines_1h  = _get_klines_raw(sym, '1h', 12)
            klines_30m = _get_klines_raw(sym, '30m', 8)
            return sym, {'klines_1h': klines_1h, 'klines_30m': klines_30m}
        except Exception:
            return sym, {}

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch_klines, c): c for c in final_coins}
        for future in as_completed(futures):
            sym, kls = future.result()
            for c in final_coins:
                if c['symbol'] == sym:
                    c.update(kls)
                    break

    print(f"输出K线 ({len(final_coins)}个币种)...\n")

    # 输出: 复用 llm_analyze_batch 的紧凑格式
    llm_analyze_batch(final_coins)

    return final_coins

def scan_score_top(top_n: int = 30,
                   min_vol_24h: float = 3000000,
                   min_volatility: float = 1.5,
                   max_volatility: float = 30.0) -> List[Dict]:
    """
    scan-score: 全市场趋势评分

    第一步: 全市场 ticker/24hr 过滤成交额+波动率
    第二步: 并发拉所有候选币的 1h+30m K线
    第三步: 程序计算趋势分(方向+位置+量能)
    第四步: 按分排序,输出摘要+top N K线

    输出: 所有币的评分摘要 + top N 的 K 线(供 LLM 分析)
    """
    print(f"\n{'='*70}")
    print(f"SCAN-SCORE - 全市场趋势评分 (top={top_n})")
    print(f"{'='*70}")

    blacklist = set()
    try:
        with open(os.path.join(os.path.dirname(__file__), 'blacklist.json')) as f:
            bl = json.load(f)
            blacklist.update(bl.get('permanent_delist', []))
            blacklist.update(bl.get('coins', []))
    except:
        pass

    _rate_limit()
    try:
        r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/exchangeInfo", endpoint='exchangeInfo', timeout=15)
        exchange_info = r.json()
    except Exception:
        try:
            r = _rl_request('GET', f"{PAPI_URL}/papi/v1/um/exchangeInfo", endpoint='um/exchangeInfo', timeout=15)
            exchange_info = r.json()
        except Exception:
            print("❌ exchangeInfo 获取失败")
            return []

    tradeable = set()
    for s in exchange_info.get('symbols', []):
        if s.get('status') == 'TRADING' and s.get('quoteAsset') == 'USDT':
            for f in s.get('filters', []):
                if f.get('filterType') == 'MIN_NOTIONAL':
                    if float(f.get('minNotional', 0)) < 50:
                        tradeable.add(s['symbol'])
                        break

    _rate_limit()
    try:
        r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr",
                        endpoint='ticker/24hr', params={"limit": 200}, timeout=15)
        all_tickers = r.json()
        if isinstance(all_tickers, dict) and all_tickers.get('code') == -1003:
            raise Exception("rate limited")
    except Exception:
        try:
            r = _rl_request('GET', f"{PAPI_URL}/papi/v1/um/ticker/24hr",
                            endpoint='um/ticker/24hr', params={"limit": 200}, timeout=15)
            all_tickers = r.json()
        except Exception:
            print("❌ ticker/24hr 获取失败")
            return []

    candidates = []
    for t in all_tickers:
        if not isinstance(t, dict):
            continue
        sym = t.get('symbol', '')
        if not sym.endswith('USDT') or sym in blacklist or sym not in tradeable:
            continue
        try:
            price  = float(t.get('lastPrice', 0))
            high   = float(t.get('highPrice', 0))
            low    = float(t.get('lowPrice', 0))
            change = float(t.get('priceChangePercent', 0))
            volume = float(t.get('quoteVolume', 0))
        except (ValueError, TypeError):
            continue
        if price == 0 or low == 0:
            continue
        if volume < min_vol_24h:
            continue
        vol_pct = (high - low) / price * 100
        if vol_pct < min_volatility or vol_pct > max_volatility:
            continue

        candidates.append({
            'symbol':     sym,
            'price':      price,
            'change_24h': round(change, 2),
            'volume_24h': volume,
            'vol_24h_pct': round(vol_pct, 2),
        })

    total_candidates = len(candidates)
    print(f"通过粗筛: {total_candidates} 个币种")

    scored = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_calc_trend_score, c['symbol']): c for c in candidates}
        for i, future in enumerate(as_completed(futures)):
            c = futures[future]
            result = future.result()
            if result is not None:
                result.update({
                    'price':        c['price'],
                    'change_24h':   c['change_24h'],
                    'volume_24h':   c['volume_24h'],
                    'vol_24h_pct':  c['vol_24h_pct'],
                })
                scored.append(result)
            if (i + 1) % 50 == 0:
                print(f"  已评分: {i+1}/{total_candidates} ...")

    if not scored:
        print("❌ 没有币种通过评分")
        return []

    scored.sort(key=lambda x: x['score'], reverse=True)

    print(f"\n{'='*70}")
    print(f"评分摘要 (共 {len(scored)} 个币)")
    print(f"{'='*70}")
    print(f"{'排名':>4}  {'币种':<16} {'综合分':>6} {'趋势':>5} {'位置':>6} {'量比':>6}  {'建议'}")
    print("-" * 70)

    for i, s in enumerate(scored):
        trend_sym = "+2" if s['trend'] == 2 else "-2" if s['trend'] == -2 else \
                    "+1" if s['trend'] == 1 else "-1" if s['trend'] == -1 else " 0"
        pos_bar = f"{s['position']:>5.1f}%"
        vol_bar = f"{s['vol_ratio']:>5.2f}x"
        score_str = f"{'+' if s['score'] > 0 else ''}{s['score']}"
        print(f"{i+1:>4}  {s['symbol']:<16} {score_str:>6} {trend_sym:>5} {pos_bar} {vol_bar}  {s['trend_label']}")

    print(f"\n趋势分说明: 综合分 = 趋势(+-2) + 位置(+-1) + 量能(+-1)")

    final_coins = scored[:top_n]
    print(f"\n{'='*70}")
    print(f"输出K线 (前{len(final_coins)}名按趋势分排序)")
    print(f"{'='*70}\n")

    llm_analyze_batch(final_coins)
    return scored


def _calc_trend_score(symbol: str):
    """计算单个币的趋势评分（纯程序化，无指标）"""
    try:
        klines_1h  = _get_klines_raw(symbol, '1h',  12)
        klines_30m = _get_klines_raw(symbol, '30m', 8)
    except Exception:
        return None

    if len(klines_1h) < 6 or len(klines_30m) < 3:
        return None

    closes_1h  = [float(k[4]) for k in klines_1h]
    highs_1h   = [float(k[2]) for k in klines_1h]
    lows_1h    = [float(k[3]) for k in klines_1h]

    current_price = closes_1h[-1]
    period_high   = max(highs_1h)
    period_low    = min(lows_1h)
    position_pct  = (current_price - period_low) / (period_high - period_low) * 100 \
                    if period_high != period_low else 50.0

    recent_highs = highs_1h[-2:]
    older_highs  = highs_1h[-4:-2]
    recent_lows  = lows_1h[-2:]
    older_lows   = lows_1h[-4:-2]

    h_up   = max(recent_highs)  > max(older_highs)
    h_down = max(recent_highs)  < max(older_highs)
    l_up   = min(recent_lows)   > min(older_lows)
    l_down = min(recent_lows)   < min(older_lows)

    trend_score = +2 if (h_up and l_up) else -2 if (h_down and l_down) else \
                  +1 if (h_up or l_up)  else -1 if (h_down or l_down) else 0

    position_score = +1 if position_pct < 25 else -1 if position_pct > 75 else 0

    volumes_30m = [float(k[5]) for k in klines_30m]
    if len(volumes_30m) >= 4:
        avg_vol  = sum(volumes_30m[-4:-1]) / 3
        vol_ratio = volumes_30m[-1] / avg_vol if avg_vol > 0 else 1.0
        vol_score = +1 if vol_ratio > 1.5 else -1 if vol_ratio < 0.6 else 0
    else:
        vol_ratio = 1.0
        vol_score = 0

    total_score = trend_score + position_score + vol_score

    trend_labels = {
        (True, False, False, False): "↑ 偏强",
        (False, True, False, False): "↓ 偏弱",
        (False, False, True, False): "↑ 偏强",
        (False, False, False, True): "↓ 偏弱",
    }

    if total_score >= 3:
        trend_label = "↑↑ LONG候选 ✅"
    elif total_score >= 1:
        trend_label = "↑ 偏强"
    elif total_score <= -3:
        trend_label = "↓↓ SHORT候选 ✅"
    elif total_score <= -1:
        trend_label = "↓ 偏弱"
    else:
        trend_label = "→ 观望"

    return {
        'symbol':      symbol,
        'score':       total_score,
        'trend':       trend_score,
        'position':    round(position_pct, 1),
        'vol_ratio':   round(vol_ratio, 2),
        'vol_score':   vol_score,
        'trend_label': trend_label,
        'klines_1h':   klines_1h,
        'klines_30m':  klines_30m,
    }


def get_market_data(symbol: str, kline_count: int = 15) -> Dict:
    """获取市场数据(优化:减少API调用,只取30m+1h两周期)"""
    trader = BinanceTrader()
    # 优化:只取30m(主指标)+ 1h(趋势验证),limit 50
    klines_data = {}
    for iv_name in ('30m', '1h'):
        klines_data[iv_name] = trader.get_klines(symbol, iv_name, limit=50)
    kl30 = klines_data.get('30m', [])
    if not kl30:
        raise Exception(f"无法获取 {symbol} 30m K线数据")
    closes = [float(k[4]) for k in kl30]
    highs = [float(k[2]) for k in kl30]
    lows = [float(k[3]) for k in kl30]
    volumes = [float(k[5]) for k in kl30]
    rsi = TechnicalIndicators.calculate_rsi(closes)
    macd = TechnicalIndicators.calculate_macd(closes)
    bb = TechnicalIndicators.calculate_bollinger_bands(closes)
    atr = TechnicalIndicators.calculate_atr(kl30)
    ma5 = TechnicalIndicators.calculate_ma(closes, 5)
    ma20 = TechnicalIndicators.calculate_ma(closes, 20)
    # ⭐ 用 get_ticker 的 lastPrice(最精确),不用 K线收盘价
    try:
        ticker = trader.get_ticker(symbol)
        current_price = float(ticker.get('lastPrice', closes[-1]))
    except Exception:
        current_price = closes[-1]
    atr_percent = (atr / current_price * 100) if current_price > 0 else 0
    avg_volume = sum(volumes[-5:]) / 5
    current_volume = volumes[-1]
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1
    try:
        funding = trader.get_funding_rate(symbol)
        funding_rate = funding.get('fundingRate', 0)
    except Exception:
        funding_rate = 0
    try:
        ls_ratio = trader.get_long_short_ratio(symbol)
    except Exception:
        ls_ratio = {'longRatio': 50, 'shortRatio': 50}
    return {
        'symbol': symbol,
        'current_price': current_price,
        'change_24h': float(trader.get_ticker(symbol).get('priceChangePercent', 0)),
        'klines_data': klines_data,
        'rsi': rsi,
        'macd': macd,
        'bollinger': bb,
        'atr': atr,
        'atr_percent': round(atr_percent, 2),
        'ma5': round(ma5, 2),
        'ma20': round(ma20, 2),
        'volume_ratio': round(volume_ratio, 2),
        'funding_rate': funding_rate,
        'long_ratio': ls_ratio.get('longRatio', 50),
        'short_ratio': ls_ratio.get('shortRatio', 50),
    }

def _format_klines_for_market(klines: List, interval: str) -> str:
    """Format klines for market command - matches scan-all format"""
    lines = []
    if not klines:
        return ""
    lines.append(f"## {interval} ({len(klines)} bars)")
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    current = closes[-1]
    period_high = max(highs)
    period_low = min(lows)
    pct_from_high = ((current - period_high) / period_high * 100) if period_high > 0 else 0
    pct_from_low = ((current - period_low) / period_low * 100) if period_low > 0 else 0
    lines.append(f"Current: {current:.6f} | Period High: {period_high:.6f}({pct_from_high:+.1f}%) | Period Low: {period_low:.6f}({pct_from_low:+.1f}%)")
    lines.append("")
    for k in klines[-15:]:
        ts = k[0] / 1000
        dt = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')
        o = float(k[1]); c = float(k[4]); h = float(k[2]); l = float(k[3]); v = float(k[5])
        chg = (c - o) / o * 100 if o > 0 else 0
        body = "UP" if c > o else "DOWN"
        upper_shadow = h - max(o, c)
        lower_shadow = min(o, c) - l
        lines.append(f"  {dt} O:{o:.5f} C:{c:.5f} H:{h:.5f} L:{l:.5f} {body}{chg:+.2f}% U:{upper_shadow:.5f} L:{lower_shadow:.5f} V:{v:.0f}")
    lines.append("")
    return "\n".join(lines)


def print_market_data(data: Dict):
    """打印市场数据 - scan-all格式"""
    print(f"\n{'='*60}")
    print(f"📊 {data['symbol']} 市场数据")
    print(f"{'='*60}")
    # 直接从API获取原始价格(保持精度, PM-safe)
    import requests
    try:
        r = requests.get(
            f"{PAPI_URL}/papi/v1/um/ticker/price",
            params={'symbol': data['symbol']},
            timeout=10,
            verify=certifi.where()
        )
        raw_price = r.json().get('price', str(data['current_price']))
    except Exception:
        try:
            r = requests.get(
                f"{FAPI_URL}/fapi/v1/ticker/price",
                params={'symbol': data['symbol']},
                timeout=10,
                verify=certifi.where()
            )
            raw_price = r.json().get('price', str(data['current_price']))
        except Exception:
            raw_price = str(data['current_price'])
    print(raw_price)
    sign = '+' if data['change_24h'] >= 0 else ''
    print(f"24h涨幅: {sign}{data['change_24h']:.2f}%")
    print()
    print(f"【技术指标】")
    print(f"  RSI(14):     {data['rsi']:.2f}")
    print(f"  MACD:        macd={data['macd']['macd']:.4f}")
    print(f"  布林带:      上 ${data['bollinger']['upper']:.2f} / 中 ${data['bollinger']['middle']:.2f} / 下 ${data['bollinger']['lower']:.2f}")
    print(f"  位置:        {data['bollinger']['position']:.1f}%")
    print(f"  ATR:         {data['atr']:.2f} ({data['atr_percent']}%)")
    print(f"  MA5:         ${data['ma5']}")
    print(f"  MA20:        ${data['ma20']}")
    print()
    print(f"【成交量】")
    print(f"  量比:        {data['volume_ratio']:.2f}x")
    print()
    print(f"【市场情绪】")
    print(f"  资金费率:    {data['funding_rate']:.4f}%")
    print(f"  多头比例:    {data['long_ratio']:.1f}%")
    print(f"  空头比例:    {data['short_ratio']:.1f}%")
    print()

    # 多周期K线 - 与scan-all一致格式
    # Bug 7 Fix: 只输出实际获取到的周期，避免空数据误导
    for interval, key in [('5m', '5m'), ('30m', '30m'), ('1h', '1h'), ('4h', '4h')]:
        kls = data['klines_data'].get(key, [])
        if kls:
            print(_format_klines_for_market(kls, interval))
        # 空周期不打印，避免误导用户
    if not any(data['klines_data'].get(k) for k in ('5m', '30m', '1h', '4h')):
        print("(K线数据仅包含30m+1h，如需其他周期请使用scan-all命令)")

def get_status(symbol: str = None) -> Dict:
    """获取账户状态"""
    trader = BinanceTrader()

    balance = trader.get_usdt_balance()
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
            print(f"  数量: {pos['amount']}")
            print(f"  开仓价: ${pos['entryPrice']:.4f}")
            pnl = pos.get('unrealizedProfit')
            if pnl is not None:
                print(f"  未实现盈亏: ${pnl:.2f}")
            else:
                print(f"  未实现盈亏: $0.00")
            print(f"  杠杆: {pos['leverage']}x")
            print(f"  方向: {pos['positionSide']}")
    else:
        print("持仓: 无")

    return result

def do_open_short(symbol: str, margin: float, leverage: int) -> Dict:
    """开空仓"""
    # ===== Bug P3 Fix: 开仓前强制检查黑名单 =====
    blacklist = set()
    try:
        with open(os.path.join(os.path.dirname(__file__), 'blacklist.json')) as f:
            bl = json.load(f)
            blacklist.update(bl.get('permanent_delist', []))
            blacklist.update(bl.get('coins', []))
    except Exception:
        pass
    if symbol in blacklist:
        raise Exception(f"[{symbol}] 在黑名单中，禁止开仓")

    trader = BinanceTrader()
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
    # Binance 最小名义价值 5 USDT, 低于此值直接拒绝
    if notional < 5:
        raise Exception(f"[{symbol}] 名义价值 ${notional:.2f} < 5 USDT (最小成交额限制)，无法开仓")

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

    result = trader.open_short(symbol, quantity, leverage)
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
        tp_trigger = _round_to_tick(sl_result['tp_trigger'], symbol)
        
        print(f"  📊 ATR {atr_data['atr_percent']:.2f}% → 波动率: {atr_data['volatility']}")
        print(f"  🔒 止损 @ ${sl_trigger:.{_get_price_decimals(symbol)}f} ({sl_result['sl_percent']:.2f}%)")
        print(f"  🎯 止盈 @ ${tp_trigger:.{_get_price_decimals(symbol)}f} ({sl_result['tp_percent']:.2f}%, R:R=1:1.5)")
        
        try:
            trader.set_stop_loss(symbol, 'BUY', qty_int, sl_trigger)
            print(f"  ✅ 止损单已设置")
        except Exception as e:
            print(f"  ⚠️ 止损设置失败: {e}")
        
        try:
            trader.set_take_profit(symbol, 'BUY', qty_int, tp_trigger)
            print(f"  ✅ 止盈单已设置")
        except Exception as e:
            print(f"  ⚠️ 止盈设置失败: {e}")
    else:
        print(f"\n❌ 开空仓失败: {result}")
    return result

def do_close_short(symbol: str, percent: float = 100) -> Dict:
    """平空仓 (支持部分平仓)"""
    trader = BinanceTrader()

    print(f"\n{'='*60}")
    print(f"🔚 平空仓: {symbol} ({percent}%)")
    print(f"{'='*60}")

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
    """\xe5\xbc\x80\xe5\xa4\x9a\xe4\xbb\x93"""
    # ===== Bug P3 Fix: \xe5\xbc\x80\xe4\xbb\x93\xe5\x89\x8d\xe5\xbc\xba\xe5\x88\xb6\xe6\xa3\x80\xe6\x9f\xa5\xe9\xbb\x91\xe5\x90\x8d\xe5\x8d\x95 =====
    blacklist = set()
    try:
        with open(os.path.join(os.path.dirname(__file__), 'blacklist.json')) as f:
            bl = json.load(f)
            blacklist.update(bl.get('permanent_delist', []))
            blacklist.update(bl.get('coins', []))
    except Exception:
        pass
    if symbol in blacklist:
        raise Exception(f"[{symbol}] \xe5\x9c\xa8\xe9\xbb\x91\xe5\x90\x8d\xe5\x8d\x95\xe4\xb8\xad\xe7\xa6\x81\xe6\xad\xa2\xe5\xbc\x80\xe4\xbb\x93")

    trader = BinanceTrader()
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

    # ===== Bug P2 Fix: \xe5\xbc\x80\xe4\xbb\x93\xe5\x89\x8d\xe6\xa3\x80\xe6\x9f\xa5\xe6\x9c\x80\xe5\xb0\x8f\xe5\x90\x8d\xe4\xb9\x89\xe4\xbb\xa3\xe4\xbb\xa7 =====
    notional = quantity * price
    if notional < 5:
        raise Exception(f"[{symbol}] \xe5\x90\x8d\xe4\xb9\x89\xe4\xbb\xa3\xe5\x80\xbc ${notional:.2f} < 5 USDT (\xe6\x9c\x80\xe5\xb0\x8f\xe6\x88\x90\xe4\xba\xa4\xe9\xa2\x9d\xe9\x99\x90\xe5\x88\xb6)\uff0c\xe6\x97\xa0\xe6\xb3\x95\xe5\xbc\x80\xe4\xbb\x93")

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
    result = trader.open_long(symbol, quantity, leverage)
    print(f"\xe8\xae\xa2\xe5\x8d\x95\xe7\xbb\x93\xe6\x9e\x9c: {json.dumps(result, indent=2)}")
    # Bug P1 Fix: \xe4\xbb\xa5 status \xe4\xb8\xba\xe5\x87\x86\xe5\x88\xa4\xe6\x96\xad\xe6\x98\xaf\xe5\x90\xa6\xe6\x88\x90\xe5\x8a\x9f
    order_status = result.get('status', '')
    is_success = order_status in ('NEW', 'FILLED', 'PARTIALLY_FILLED') or result.get('orderId') or result.get('clientOrderId')
    if is_success:
        print(f"\n\xe2\x9c\x85 \xe5\xbc\x80\xe5\xa4\x9a\xe4\xbb\x93\xe6\x88\x90\xe5\x8a\x9f")
        # ===== \xe8\x87\xaa\xe5\x8a\xa8\xe8\xae\xbe\xe7\xbd\xae\xe6\xad\xa2\xe6\x8d\x9f(\xe6\x9d\x83\xe6\x9d\x83\xe5\x89\x8d1%, \xe6\xad\xa2\xe7\x9b\x88\xe7\x94\xb1LLM\xe8\xae\xbe\xe7\xbd\xae)=====
        import math
        qty_int = max(1, math.ceil(quantity))
        sl_trigger = round(price * 0.97, 6)
        try:
            trader.set_stop_loss(symbol, 'SELL', qty_int, sl_trigger)
            print(f"  \xe2\x9c\x85 \xe6\xad\xa2\xe6\x8d\x9f @ ${sl_trigger} (\xe8\xb7\x8c3%\xe8\xa7\xa6\xe5\x8f\x91)")
        except Exception as e:
            print(f"  \xe2\x9a\xa0\xef\xb8\x8f \xe6\xad\xa2\xe6\x8d\x9f\xe8\xae\xbe\xe7\xbd\xae\xe5\xa4\xb1\xe8\xb4\xa5: {e}")
        print(f"  i\xef\xb8\x8f \xe6\xad\xa2\xe7\x9b\x88\xe7\x94\xb1LLM\xe5\x88\x86\xe6\x9e\x90\xe5\x90\x8e\xe8\xae\xbe\xe7\xbd\xae")
    else:
        print(f"\n\xe2\x9d\x8c \xe5\xbc\x80\xe5\xa4\x9a\xe4\xbb\x93\xe5\xa4\xb1\xe8\xb4\xa5: {result}")
    return result

def do_close_long(symbol: str, percent: float = 100) -> Dict:
    """平多仓 (支持部分平仓)"""
    trader = BinanceTrader()

    print(f"\n{'='*60}")
    print(f"🔚 平多仓: {symbol} ({percent}%)")
    print(f"{'='*60}")

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

    # scan
    scan_parser = subparsers.add_parser('scan', help='扫描涨幅币种')
    scan_parser.add_argument('--min-change', type=float, default=10, help='最小涨幅%')

    # scan-short
    scan_short_parser = subparsers.add_parser('scan-short', help='扫描做空候选')
    scan_short_parser.add_argument('--min-change', type=float, default=10, help='最小涨幅%')

    # scan-long
    scan_long_parser = subparsers.add_parser('scan-long', help='扫描做多候选')
    scan_long_parser.add_argument('--min-change', type=float, default=-10, help='最大跌幅%(负值)')
    scan_long_parser.add_argument('--max-change', type=float, default=-3, help='最小跌幅%(负值)')

    # scan-all(统一波动率扫描,多空一起)
    scan_all_parser = subparsers.add_parser('scan-all', help='统一波动率扫描(多空一起)')
    scan_all_parser.add_argument('--top', type=int, default=50, help='最终输出币种个数(默认50)')
    scan_all_parser.add_argument('--klines', type=int, default=None, help='取多少个候选币种拿K线(默认等于--top值)')
    scan_all_parser.add_argument('--min-vol', type=float, default=3.0, help='最低1h波动率%%')
    scan_all_parser.add_argument('--log', type=str, default=None, help='日志文件(覆盖模式,默认stdout)')

    # scan-pick(量价筛选,适合0策略)
    scan_pick_parser = subparsers.add_parser('scan-pick', help='量价筛选(两轮:24h粗筛→1h K线量比重排)')
    scan_pick_parser.add_argument('--top', type=int, default=30, help='输出币种个数(默认30)')
    scan_pick_parser.add_argument('--round1-count', type=int, default=30, help='第一轮粗筛取前N名(默认30)')
    scan_pick_parser.add_argument('--min-vol-24h', type=float, default=3000000, help='最低24h成交额USDT(默认300万)')
    scan_pick_parser.add_argument('--min-volatility', type=float, default=1.5, help='最低24h波动率(默认1.5%%)')
    scan_pick_parser.add_argument('--max-volatility', type=float, default=30.0, help='最高24h波动率(默认30.0%%)')

    scan_score_parser = subparsers.add_parser('scan-score', help='全市场趋势评分(程序初选→LLM分析)')
    scan_score_parser.add_argument('--top', type=int, default=30, help='输出K线个数(默认30)')
    scan_score_parser.add_argument('--min-vol-24h', type=float, default=3000000, help='最低24h成交额USDT(默认300万)')
    scan_score_parser.add_argument('--min-volatility', type=float, default=1.5, help='最低24h波动率(默认1.5%%)')
    scan_score_parser.add_argument('--max-volatility', type=float, default=30.0, help='最高24h波动率(默认30.0%%)')

    # market
    market_parser = subparsers.add_parser('market', help='市场数据')
    market_parser.add_argument('--symbol', type=str, required=True, help='币种')
    market_parser.add_argument('--kline-last', type=int, default=5, help='K线数量')

    # open-short
    open_parser = subparsers.add_parser('open-short', help='开空仓')
    open_parser.add_argument('margin', type=float, help='保证金')
    open_parser.add_argument('--symbol', type=str, required=True, help='币种')
    open_parser.add_argument('--leverage', type=int, default=10, help='杠杆')


    # close-short
    close_parser = subparsers.add_parser('close-short', help='平空仓')
    close_parser.add_argument('--symbol', type=str, required=True, help='币种')
    close_parser.add_argument('--percent', type=float, default=100, help='平仓比例(0-100),默认100%%全平')

    # open-long
    open_long_parser = subparsers.add_parser('open-long', help='开多仓')
    open_long_parser.add_argument('margin', type=float, help='保证金')
    open_long_parser.add_argument('--symbol', type=str, required=True, help='币种')
    open_long_parser.add_argument('--leverage', type=int, default=10, help='杠杆')

    # close-long
    close_long_parser = subparsers.add_parser('close-long', help='平多仓')
    close_long_parser.add_argument('--symbol', type=str, required=True, help='币种')
    close_long_parser.add_argument('--percent', type=float, default=100, help='平仓比例(0-100),默认100%全平')

    # llm-open (LLM分析做空)
    llm_open_parser = subparsers.add_parser('llm-open', help='LLM分析做空')
    llm_open_parser.add_argument('--symbol', type=str, required=True, help='币种')

    # llm-hold (LLM分析持仓)
    llm_hold_parser = subparsers.add_parser('llm-hold', help='LLM分析持仓')
    llm_hold_parser.add_argument('--symbol', type=str, required=True, help='币种')

    # factor-signal (因子信号)
    factor_parser = subparsers.add_parser('factor-signal', help='因子信号扫描')
    factor_parser.add_argument('--symbol', type=str, default='btcusdt', help='币种(默认btcusdt)')
    factor_parser.add_argument('--seconds', type=int, default=60, help='收集秒数(默认60)')

    # factor-trade (因子实盘交易)
    trade_parser = subparsers.add_parser('factor-trade', help='因子实盘交易')
    trade_parser.add_argument('--symbol', type=str, default='btcusdt', help='币种')
    trade_parser.add_argument('--seconds', type=int, default=60, help='收集秒数')
    trade_parser.add_argument('--margin', type=float, default=10, help='保证金USDT')
    trade_parser.add_argument('--leverage', type=int, default=10, help='杠杆')
    trade_parser.add_argument('--min-conf', type=float, default=0.6, help='最小置信度')

    # replace-order: 替换条件单(自动取消旧单+下新单)
    replace_parser = subparsers.add_parser('replace-order', help='替换条件单(自动取消旧单后下新单)')
    replace_parser.add_argument('--symbol', type=str, required=True, help='币种')
    replace_parser.add_argument('--side', type=str, required=True, help="SELL=平多/BUY=平空")
    replace_parser.add_argument('--type', type=str, default='STOP_MARKET',
                                  help="STOP_MARKET/TAKE_PROFIT_MARKET/STOP/TAKE_PROFIT")
    replace_parser.add_argument('--algo-id', type=int, default=None,
                                  help='旧条件单ID(可选)')

    # cancel-conditionals: 取消指定币种全部追踪的委托单
    cancel_cond_parser = subparsers.add_parser('cancel-conditionals', help='取消指定币种全部追踪的委托单')
    cancel_cond_parser.add_argument('--symbol', type=str, required=True, help='币种')
    cancel_cond_parser.add_argument('--side', type=str, default=None,
                                  help='SELL=平多/BUY=平空(可选,不填则取消该币种全部)')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == 'status':
        # ⚠️ 每次命令执行前都强制从磁盘加载最新状态
        # 防止不同进程/命令间状态不一致
        _load_sim_state()
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
                calc_positions[symbol].append({'margin': margin, 'qty': qty, 'entry': entry, 'side': side, 'leverage': leverage})
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

    elif args.command == 'scan':
        scan_candidates(args.min_change)

    elif args.command == 'scan-short':
        scan_short_candidates(args.min_change)

    elif args.command == 'scan-long':
        scan_long_candidates(args.min_change, args.max_change)

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
        scan_volatility_top(args.top, args.min_vol, args.klines if args.klines else args.top)
        if args.log:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(_orig_out, sys.stdout.fileno())
            os.dup2(_orig_err, sys.stderr.fileno())
            _log_fd.close()
            os.close(_orig_out)
            os.close(_orig_err)

    elif args.command == 'scan-pick':
        scan_pick_top(args.top, args.min_vol_24h, args.min_volatility, args.max_volatility, args.round1_count)

    elif args.command == 'scan-score':
        scan_score_top(args.top, args.min_vol_24h, args.min_volatility, args.max_volatility)

    elif args.command == 'market':
        if not args.symbol:
            print("Error: --symbol is required")
            return
        data = get_market_data(args.symbol, args.kline_last)
        print_market_data(data)

    elif args.command == 'open-short':
        if not args.symbol:
            print("Error: --symbol is required")
            return
        do_open_short(args.symbol, args.margin, args.leverage)

    elif args.command == 'close-short':
        if not args.symbol:
            print("Error: --symbol is required")
            return
        do_close_short(args.symbol, args.percent)

    elif args.command == 'open-long':
        if not args.symbol:
            print("Error: --symbol is required")
            return
        do_open_long(args.symbol, args.margin, args.leverage)

    elif args.command == 'close-long':
        if not args.symbol:
            print("Error: --symbol is required")
            return
        do_close_long(args.symbol, args.percent)

    elif args.command == 'llm-open':
        if not args.symbol:
            print("Error: --symbol is required")
            return
        do_llm_analysis(args.symbol, "open")

    elif args.command == 'llm-hold':
        if not args.symbol:
            print("Error: --symbol is required")
            return
        do_llm_analysis(args.symbol, "hold")

    elif args.command == 'factor-signal':
        # 因子信号
        import time
        from factor_engine import FactorEngine, set_factor_engine
        from binance_stream import DataManager
        
        print(f"\n{'='*50}")
        print(f"🔬 因子信号 - {args.symbol}")
        print(f"{'='*50}")
        
        fe = FactorEngine()
        set_factor_engine(fe)
        dm = DataManager(args.symbol)
        dm.start()
        
        print(f"收集数据中...")
        time.sleep(args.seconds)
        
        dm.stop()
        
        # 手动转换数据到因子引擎
        ticks = list(dm.stream.ticks)
        klines = list(dm.stream.klines)
        
        print(f"逐笔成交: {len(ticks)}, K线: {len(klines)}")
        
        # 添加到因子引擎
        for t in ticks:
            fe.add_tick(t['price'], t['qty'], t['is_buyer_maker'])
        
        for k in klines:
            fe.add_kline(k)
        
        if ticks:
            fe.set_last_price(ticks[-1]['price'])
        
        # 获取信号
        signal, conf, details = fe.generate_signal()
        
        signal_names = {1: 'LONG', -1: 'SHORT', 0: 'NEUTRAL'}
        
        print(f"\n{'='*50}")
        print(f"📊 因子信号: {signal_names.get(signal, 'NEUTRAL')}")
        print(f"🎯 置信度: {conf:.2f}")
        print(f"📝 详情: {details}")
        print(f"{'='*50}")

    elif args.command == 'factor-trade':
        # 因子实盘交易
        import time
        from factor_v3 import TickData, generate_signal
        from binance_stream import DataManager
        import requests
        
        symbol = args.symbol.upper()
        min_conf = args.min_conf
        
        print(f"\n{'='*60}")
        print(f"🚀 因子实盘交易 - {symbol}")
        print(f"{'='*60}")
        print(f"保证金: {args.margin} USDT, 杠杆: {args.leverage}x")
        print(f"最小置信度: {min_conf}")
        
        # 获取实时因子信号
        url = 'https://api.binance.com/api/v3/aggTrades'
        resp = requests.get(url, params={'symbol': symbol, 'limit': 800}, timeout=30, verify=False)
        data = resp.json()
        
        ticks = [TickData(
            price=float(t['p']),
            qty=float(t['q']),
            is_buyer_maker=t['m'],
            time=t['T']
        ) for t in data]
        
        signal, conf, details = generate_signal(ticks)
        
        signal_names = {1: 'LONG', -1: 'SHORT', 0: 'NEUTRAL'}
        
        print(f"\n📊 因子信号: {signal_names.get(signal, 'NEUTRAL')}")
        print(f"🎯 置信度: {conf:.2f}")
        print(f"📝 详情: {details}")
        
        # 检查是否开仓
        if signal == 0:
            print(f"⚠️ 信号中性，不开仓")
        elif conf < min_conf:
            print(f"⚠️ 置信度 {conf:.2f} < {min_conf}，不开仓")
        else:
            # 执行开仓
            print(f"\n✅ 执行开仓...")
            
            if signal == 1:
                result = do_open_long(symbol.replace('USDT', ''), args.margin, args.leverage)
            else:
                result = do_open_short(symbol.replace('USDT', ''), args.margin, args.leverage)
            
            print(f"开仓结果: {result}")

        print(f"{'='*60}")

    elif args.command == 'replace-order':
        trader = BinanceTrader()
        # 优先用命令行 --algo-id,其次从文件查找
        algo_id = args.algo_id
        if not algo_id:
            orders = _load_conditional_orders()
            algo_id = orders.get(args.symbol, {}).get(args.side)
        # 获取数量(优先查持仓,其次从文件)
        positions = trader.get_positions(args.symbol)
        if positions:
            qty = int(max(1, abs(positions[0]['amount'])))
        else:
            # 无持仓时qty传1(条件单数量不影响实际平仓)
            qty = 1
        # 触发价由replace_conditional_order内部基于实时市价自动计算
        result = trader.replace_conditional_order(
            args.symbol, args.side, qty, args.type, algo_id
        )
        print(f"✅ 条件单已替换: {result}")

    elif args.command == 'cancel-conditionals':
        trader = BinanceTrader()
        orders = _load_conditional_orders()
        sym = args.symbol
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
                    print(f"⚠️ 取消失败 {sym} {side}: {e}")
            if cancelled == 0:
                print(f"❌ 未找到匹配的追踪记录")

if __name__ == '__main__':
    main()