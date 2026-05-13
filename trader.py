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

# Module-level simulation state
_sim_balance = 1000.0
_sim_positions = {}

def _load_sim_state():
    """从文件加载模拟状态
    
    Returns:
        tuple: (balance, positions) — 总是返回 tuple，加载失败时返回默认值
    """
    if os.path.exists(SIM_STATE_FILE):
        try:
            with open(SIM_STATE_FILE, 'r') as f:
                state = json.load(f)
            global _sim_balance, _sim_positions
            _sim_balance = state.get('balance', 1000.0)
            _sim_positions = state.get('positions', {})
            return (_sim_balance, _sim_positions)
        except Exception:
            pass  # 文件损坏时仍返回默认值，保持一致性
    return (1000.0, {})

def _save_sim_state(balance, positions):
    """保存模拟状态到文件"""
    global _sim_balance, _sim_positions
    _sim_balance = balance
    _sim_positions = positions
    try:
        with open(SIM_STATE_FILE, 'w') as f:
            json.dump({'balance': balance, 'positions': positions}, f)
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


# ========== PM 账户检测（纯 requests，无第三方客户端依赖）==========
_is_pm_account = None


def _papi_get_account(retries: int = 3) -> Dict:
    """直接用 requests 发 papi 签名请求获取账户信息（不依赖 BinanceClient），带重试"""
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
    """检测是否为 Portfolio Margin 账户（检查 tradeGroupId 字段）
    遇到异常时不缓存结果，持续重试（避免网络抖动导致永久误判）"""
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
        """PAPI 签名请求 (PM 统一账户)，带重试"""
        headers = {'X-MBX-APIKEY': self.api_key}
        if params is None:
            params = {}
        signed_params = self._papi_sign(params)
        url = f"{self.papi_url}{endpoint}?{signed_params}"
        last_err = None
        for attempt in range(retries):
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
        """获取当前价格（优先 papi，降级 fapi，再降级 klines）"""
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
        # 3. 最终降级：从 klines 取最新收盘价
        try:
            r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/klines", endpoint='klines', params={'symbol': symbol, 'interval': '1m', 'limit': 1})
            klines = r.json()
            if klines:
                return float(klines[-1][4])  # 收盘价
        except Exception:
            pass
        raise Exception(f"无法获取 {symbol} 价格（所有端点均失败）")

    def get_ticker(self, symbol: str) -> Dict:
        r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={'symbol': symbol})
        return r.json()

    def set_stop_loss(self, symbol: str, side: str, quantity: float, trigger_price: float) -> Dict:
        """PM账户设置止损条件单 (STOP_MARKET - 触发后市价平仓)
        
        side: 'SELL'=平多, 'BUY'=平空
        trigger_price: 触发价格（跌破此价触发）
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
        trigger_price: 触发价格（跌破此价触发）
        order_price: 下单价格（通常低于触发价）
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
        trigger_price: 触发价格（涨到此价触发）
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
        trigger_price: 触发价格（涨到此价触发）
        order_price: 下单价格（通常等于或略低于触发价）
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
        """查询PM账户活跃条件单（PM账户不支持此接口，始终返回空列表）"""
        # PM账户 /papi/v1/um/conditional/openOrders 返回 404，无法查询
        # 使用 .conditional_orders.json 文件追踪活跃条件单
        return []

    def replace_conditional_order(self, symbol: str, side: str, quantity: int,
                                   new_trigger_price: float, order_type: str = 'STOP_MARKET',
                                   algo_id: int = None) -> Dict:
        """下新的条件单，自动追踪algo_id
        
        流程：1)从文件查找旧algo_id 2)尝试取消旧单 3)下新单 4)保存新algo_id
        
        Args:
            symbol: 币种，如 NILUSDT
            side: 'SELL'=平多仓/'BUY'=平空仓
            quantity: 数量（整数）
            new_trigger_price: 新触发价格
            order_type: 'STOP_MARKET'(止损) / 'TAKE_PROFIT_MARKET'(止盈)
            algo_id: 可选，直接指定旧条件单ID
        """
        # 1. 查找旧algo_id（从文件追踪）
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
        
        # 3. 下新单
        algo_type_map = {
            'STOP_MARKET': 'STOP_MARKET',
            'TAKE_PROFIT_MARKET': 'TAKE_PROFIT_MARKET',
            'STOP': 'STOP',
            'TAKE_PROFIT': 'TAKE_PROFIT',
        }
        algo_type_val = algo_type_map.get(order_type, 'STOP_MARKET')
        
        result = self._papi_request('POST', '/papi/v1/um/algo/order', {
            'symbol': symbol,
            'side': side,
            'algoType': 'CONDITIONAL',
            'type': algo_type_val,
            'quantity': str(quantity),
            'triggerPrice': str(new_trigger_price),
            'reduceOnly': 'true',
        })
        
        # 4. 保存新algo_id到文件追踪
        new_algo_id = result.get('algoId')
        if new_algo_id:
            orders = _load_conditional_orders()
            if symbol not in orders:
                orders[symbol] = {}
            orders[symbol][side] = new_algo_id
            _save_conditional_orders(orders)
            print(f"[replace] ✅ 新条件单已设置 algo_id={new_algo_id}")
        
        return result


    def _clear_conditional_orders(self, symbol: str):
        """平仓后清除符号的所有条件单追踪"""
        orders = _load_conditional_orders()
        if symbol in orders:
            del orders[symbol]
            _save_conditional_orders(orders)
            print(f"[条件单] 已清除 {symbol} 追踪记录")

    def cancel_conditional_order(self, symbol: str, algo_id: int) -> Dict:
        """取消PM账户条件单，并清除文件追踪记录"""
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
        r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/klines", endpoint='klines', params={'symbol': symbol, 'interval': interval, 'limit': limit})
        return r.json()

    def get_order_book(self, symbol: str, limit: int = 20) -> Dict:
        r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/depth", endpoint='depth', params={'symbol': symbol, 'limit': limit})
        return r.json()

    def get_mark_price(self, symbol: str) -> float:
        r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/premiumIndex", endpoint='premiumIndex', params={'symbol': symbol})
        return float(r.json()['markPrice'])

    def get_funding_rate(self, symbol: str) -> Dict:
        r = _rl_request('GET', f"{self.fapi_url}/fapi/v1/premiumIndex", endpoint='premiumIndex', params={'symbol': symbol})
        data = r.json()
        return {
            'fundingRate': float(data.get('lastFundingRate', 0)) * 100,
            'nextFundingTime': data.get('nextFundingTime', '')
        }

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
        return {'longRatio': 50, 'shortRatio': 50}

    # ---- 账户操作(PAPI = 统一账户)----
    def get_account(self) -> Dict:
        """获取账户信息(自动选择 PAPI 或 fapi)"""
        if SIMULATE:
            return {
                'totalAvailableBalance': str(_sim_balance),
                'balances': [{'asset': 'USDT', 'free': str(_sim_balance)}],
                'positions': [{'symbol': s, 'positionAmt': str(-v['qty']), 'entryPrice': str(v['entry_price'])}
                              for s, v in _sim_positions.items()]
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
            for s, v in _sim_positions.items():
                if symbol and s != symbol:
                    continue
                result.append({
                    'symbol': s,
                    'amount': v['qty'],  # 保留小数
                    'entryPrice': v['entry_price'],
                    # 模拟模式不连接交易所，unrealizedProfit 无法实时计算
                # 如需精确浮盈，请使用 SIMULATE=true + trader.py status 获取快照
                    'unrealizedProfit': None,
                    'leverage': v.get('leverage', 3),
                    'positionSide': v.get('side', 'LONG'),
                    'margin': v.get('margin', 0)
                })
            return result
        if is_portfolio_margin():
            try:
                # 直接复用 account 数据中的 positions（避免多一次 HTTP 请求）
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
        """获取USDT余额（PM 统一账户）

        PM 账户：使用 /papi/v1/balance → totalWalletBalance（统一账户总余额）
        非 PM 账户：使用标准现货 account → free balance
        """
        if SIMULATE:
            _load_sim_state()
            return _sim_balance
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
        """平仓（通用：多头用SELL，空头用BUY）"""
        global _sim_balance, _sim_positions
        if SIMULATE and not _sim_positions:
            _load_sim_state()
        if SIMULATE:
            if symbol not in _sim_positions:
                raise Exception(f"[SIMULATE] 无持仓: {symbol}")
            pos = _sim_positions[symbol]
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
                pnl = (pos['entry_price'] - price) * qty
            else:
                pnl = (price - pos['entry_price']) * qty
            close_fee = qty * price * 0.001
            net_pnl = pnl - close_fee
            _sim_balance += net_pnl
            del _sim_positions[symbol]
            _save_sim_state(_sim_balance, _sim_positions)
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
            try:
                klines = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=10).json()
                price = float(klines[0][4])
            except Exception:
                price = 0.001
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
            _sim_positions[symbol] = {'qty': qty, 'entry_price': price, 'leverage': leverage, 'margin': margin, 'side': 'SHORT', }
            _sim_balance -= (margin + fee)  # 扣除保证金和手续费
            _save_sim_state(_sim_balance, _sim_positions)
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
            try:
                klines = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=10).json()
                price = float(klines[0][4])
            except Exception:
                price = 0.001
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
            _sim_positions[symbol] = {'qty': qty, 'entry_price': price, 'leverage': leverage, 'margin': margin, 'side': 'LONG', }
            _sim_balance -= (margin + fee)
            _save_sim_state(_sim_balance, _sim_positions)
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


    # get_position_mode 已删除 — fapi接口不可用，PM账户用单向持仓无需查询



    def close_long(self, symbol: str, quantity: float = None) -> Dict:
        """平多仓 — 只接受LONG持仓(PAPI/fapi)"""
        if SIMULATE:
            global _sim_balance, _sim_positions
            _load_sim_state()
            if symbol not in _sim_positions or _sim_positions[symbol].get('side') != 'LONG':
                raise Exception(f"[SIMULATE] No LONG position found for {symbol}")
            pos = _sim_positions[symbol]
            entry = pos['entry_price']
            margin = pos['margin']
            qty = pos['qty']
            try:
                klines = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=10).json()
                price = float(klines[0][4])
            except Exception:
                price = entry
            pnl = (price - entry) * qty
            close_fee = qty * price * 0.001
            net_pnl = pnl - close_fee
            _sim_balance += net_pnl
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
    """格式化 K 线原始数据供 LLM 分析（无指标、无方向提示）"""

    intervals = ["5m", "30m", "1h", "4h"]
    limits = {"5m": 12, "30m": 10, "1h": 12, "4h": 8}
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
        # 检查是否为空或非列表（可能是rate limit错误响应）
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
        intervals = ["5m", "30m", "1h", "4h"]
    limits = {"5m": 12, "30m": 10, "1h": 12, "4h": 8}
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
            klines_data = _fetch_klines_multi(symbol)
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
        for iv in ["5m", "30m", "1h", "4h"]:
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
    """滑动窗口速率限制器 — 按权重计数"""
    def __init__(self, max_weight_per_sec: int = 60, max_weight_per_min: int = 1200):
        self.max_weight_per_sec = max_weight_per_sec
        self.max_weight_per_min = max_weight_per_min
        self._sec_timestamps = []  # [timestamp, ...] last-second window
        self._min_timestamps = []  # [timestamp, ...] last-minute window
        self._lock = __import__('threading').Lock()

    def acquire(self, weight: int, wait: bool = True, timeout: float = 30.0) -> bool:
        """请求 weight 单位，必要时阻塞等待配额返回"""
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
            # 等待最近一个请求过期（优先等秒级）
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
    """带速率限制的 requests 封装 — 自动扣权重"""
    weight = _binance_weight(endpoint or url)
    # READ 超时较长
    kwargs.setdefault('timeout', 30 if method == 'GET' else 10)
    if not _rl.acquire(weight, wait=True, timeout=30.0):
        raise Exception(f"Rate limit timeout after 30s (weight={weight}) for {endpoint or url}")
    r = requests.request(method, url, **kwargs)
    return r


def _rate_limit():
    """旧兼容函数 — 扣1个默认权重"""
    _rl.acquire(_WEIGHT_CONFIG['default'][0])


def _get_klines_raw(symbol: str, interval: str, limit: int) -> List:
    """Get raw klines without computing indicators (with retry)"""
    _rate_limit()
    for attempt in range(3):  # 最多重试3次
        try:
            r = requests.get(
                f"{FAPI_URL}/fapi/v1/klines",
                params={'symbol': symbol, 'interval': interval, 'limit': limit},
                timeout=10
            )
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return data
            # 空数据,重试
            time.sleep(0.2)
            continue
        except Exception as e:
            # 网络错误,重试
            time.sleep(0.3)
            continue
    # 3次都失败,打印警告
    print(f"  ⚠️ {symbol} {interval} K线获取失败", file=sys.stderr)
    return []

def _format_klines_for_llm(klines: List, interval: str) -> str:
    """Format klines into LLM-readable text"""
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

def llm_analyze_batch(coins: List[Dict]) -> Dict[str, Dict]:
    """
    打印原始K线数据供LLM分析，程序不做任何计算
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

        for interval, key in [('5m', 'klines_5m'), ('30m', 'klines_30m'), ('1h', 'klines_1h'), ('4h', 'klines_4h')]:
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

    # 获取可交易币种(只取 TRADING 状态)
    _rate_limit()
    r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/exchangeInfo", endpoint='exchangeInfo')
    exchange_info = r.json()
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

    # 获取24h行情
    _rate_limit()
    r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={"limit": top_n * 3, "sortBy": "priceChangePercent", "sortType": "DESC"})
    all_tickers_raw = r.json()
    # 处理正常响应(list)或rate limit错误(dict)
    if isinstance(all_tickers_raw, dict) and all_tickers_raw.get('code') == -1003:
        print(f"⚠️ Binance 24hr API 速率限制，切换到本地排序模式")
        # 回退：获取全部24hr数据用本地Python排序
        _rate_limit()
        r2 = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={"limit": 200})
        all_tickers = r2.json()
        if isinstance(all_tickers, dict) and all_tickers.get('code') == -1003:
            print(f"⚠️ Binance API 全面受限，等待60秒后重试")
            time.sleep(60)
            r2 = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/24hr", endpoint='ticker/24hr', params={"limit": 200})
            all_tickers = r2.json()
        if isinstance(all_tickers, dict) and all_tickers.get('code') == -1003:
            print(f"⚠️ Binance API 仍然受限，尝试 PAPI 末端...")
            _rate_limit()
            try:
                r3 = _rl_request('GET', f"{PAPI_URL}/papi/v1/um/ticker/24hr", endpoint='um/ticker/24hr', params={"limit": 200})
                papi_data = r3.json()
                if isinstance(papi_data, list):
                    all_tickers = papi_data
                    print(f"⚠️ PAPI 末端成功，获取 {len(papi_data)} 条")
            except Exception as e:
                print(f"⚠️ PAPI 也失败: {e}")
        # 最终检查
        if isinstance(all_tickers, dict) and all_tickers.get('code') == -1003:
            print(f"⚠️ 所有API均受限，尝试用 exchangeInfo + klines 备选方案...")
            # 备选：直接从各币种K线数据获取价格信息
            _rate_limit()
            try:
                # 获取 BTCUSDT 价格作为市场参考
                btc_r = _rl_request('GET', f"{FAPI_URL}/fapi/v1/ticker/price", endpoint='ticker/price', params={"symbol": "BTCUSDT"})
                btc_data = btc_r.json()
                if isinstance(btc_data, dict) and btc_data.get('symbol'):
                    # 用 BTC 涨幅近似市场情绪
                    btc_change = 0.0
                else:
                    btc_change = 0.0
            except:
                btc_change = 0.0
            print(f"⚠️ API 受限期间无法获取完整市场数据，请稍后重试")
            print(f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')}，Binance 限制可能持续 1-2 分钟")
            # 返回空结果，程序会优雅退出
            candidates = []
            return candidates
        usdt_pairs = [t for t in all_tickers if isinstance(t, dict) and t.get('symbol', '').endswith('USDT')]
        # 本地按涨幅排序
        usdt_pairs.sort(key=lambda x: float(x.get('priceChangePercent', 0) or 0), reverse=True)
        usdt_pairs = usdt_pairs[:top_n * 3]
    else:
        all_tickers = all_tickers_raw
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

    # 批量获取K线(每次2个API:5m+30m),避免超限
    # top_klines 控制实际取K线的数量
    kline_coins = top_vol[:top_klines]

    def fetch_coin_klines(c):
        sym = c['symbol']
        try:
            klines_5m  = _get_klines_raw(sym, '5m', 12)
            klines_30m = _get_klines_raw(sym, '30m', 10)
            klines_1h  = _get_klines_raw(sym, '1h', 12)
            klines_4h  = _get_klines_raw(sym, '4h', 8)
            return sym, {
                'klines_5m':  klines_5m,
                'klines_30m': klines_30m,
                'klines_1h':  klines_1h,
                'klines_4h':  klines_4h,
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

def get_market_data(symbol: str, kline_count: int = 15) -> Dict:
    """获取市场数据 - 多周期K线格式"""
    trader = BinanceTrader()

    # 多周期K线数据 - 需要足够数据计算指标
    intervals = {'5m': 12, '30m': 10, '1h': 12, '4h': 8}
    klines_data = {}
    for iv_name, iv_min in intervals.items():
        klines_data[iv_name] = trader.get_klines(symbol, iv_name, limit=100)

    # 提取价格数据
    closes = [float(k[4]) for k in klines_data['30m']]
    highs = [float(k[2]) for k in klines_data['30m']]
    lows = [float(k[3]) for k in klines_data['30m']]
    volumes = [float(k[5]) for k in klines_data['30m']]

    # 技术指标
    rsi = TechnicalIndicators.calculate_rsi(closes)
    macd = TechnicalIndicators.calculate_macd(closes)
    bb = TechnicalIndicators.calculate_bollinger_bands(closes)
    atr = TechnicalIndicators.calculate_atr(klines_data['30m'])
    ma5 = TechnicalIndicators.calculate_ma(closes, 5)
    ma20 = TechnicalIndicators.calculate_ma(closes, 20)

    # 最新价格
    current_price = closes[-1]
    current_price_raw = None
    atr_percent = (atr / current_price * 100) if current_price > 0 else 0

    # 成交量变化
    avg_volume = sum(volumes[-5:]) / 5
    current_volume = volumes[-1]
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1

    # 资金费率
    try:
        funding = trader.get_funding_rate(symbol)
        funding_rate = funding.get('fundingRate', 0)
    except:
        funding_rate = 0

    # 多空比
    try:
        ls_ratio = trader.get_long_short_ratio(symbol)
    except:
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
    # 直接从API获取原始价格（保持精度）
    import requests
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={data['symbol']}")
        raw_price = r.json()['price']
    except:
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
    for interval, key in [('5m', '5m'), ('30m', '30m'), ('1h', '1h'), ('4h', '4h')]:
        kls = data['klines_data'].get(key, [])
        if kls:
            print(_format_klines_for_market(kls, interval))

def get_status(symbol: str = None) -> Dict:
    """获取账户状态"""
    trader = BinanceTrader()

    balance = trader.get_usdt_balance()
    positions = trader.get_positions(symbol)

    result = {
        'balance': balance,
        'positions': positions
    }

    print(f"\n{'='*60}")
    print(f"💰 账户状态")
    print(f"{'='*60}")
    print(f"USDT 余额: ${balance:.2f}")
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
    trader = BinanceTrader()

    # 获取当前价格
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

    # 计算开仓数量(round 到 stepSize 的整数倍)
    quantity = (margin * leverage) / price
    if step_size and step_size > 0:
        quantity = round(round(quantity / step_size) * step_size, 8)
    else:
        quantity = round(quantity, 8)  # 无法获取step_size时，保留8位小数
    if quantity <= 0:
        quantity = 0.00000001

    print(f"\n{'='*60}")
    print(f"🔴 开空仓: {symbol}")
    print(f"{'='*60}")
    print(f"保证金: ${margin}")
    print(f"杠杆: {leverage}x")
    print(f"价格: ${price:.4f}")
    print(f"数量: {quantity}")
    print()

    result = trader.open_short(symbol, quantity, leverage)
    print(f"订单结果: {json.dumps(result, indent=2)}")
    # 判断是否成功
    if result.get('orderId') or result.get('clientOrderId') or result.get('symbol'):
        print(f"\n✅ 开空仓成功")
        # ===== 自动设置止损（杠杆前1%，止盈由LLM设置）=====
        import math
        qty_int = max(1, math.ceil(quantity))
        sl_trigger = round(price * 1.02, 6)   # 止损: 涨2%触发（市价平）
        try:
            trader.set_stop_loss(symbol, 'BUY', qty_int, sl_trigger)
            print(f"  ✅ 止损 @ ${sl_trigger} (涨1%触发，杠杆前1%止损")
        except Exception as e:
            print(f"  ⚠️ 止损设置失败: {e}")
        print(f"  ℹ️ 止盈由LLM分析后设置")
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
    """开多仓"""
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

    print(f"\n{'='*60}")
    print(f"🟢 开多仓: {symbol}")
    print(f"{'='*60}")
    print(f"保证金: ${margin}")
    print(f"杠杆: {leverage}x")
    print(f"价格: ${price:.4f}")
    print(f"数量: {quantity}")
    print()

    result = trader.open_long(symbol, quantity, leverage)
    print(f"订单结果: {json.dumps(result, indent=2)}")
    # 判断是否成功
    if result.get('orderId') or result.get('clientOrderId') or result.get('symbol'):
        print(f"\n✅ 开多仓成功")
        # ===== 自动设置止损（杠杆前1%，止盈由LLM设置）=====
        import math
        qty_int = max(1, math.ceil(quantity))
        sl_trigger = round(price * 0.98, 6)   # 止损: 跌2%触发（市价平）
        try:
            trader.set_stop_loss(symbol, 'SELL', qty_int, sl_trigger)
            print(f"  ✅ 止损 @ ${sl_trigger} (跌1%触发，杠杆前1%止损")
        except Exception as e:
            print(f"  ⚠️ 止损设置失败: {e}")
        print(f"  ℹ️ 止盈由LLM分析后设置")
    else:
        print(f"\n❌ 开多仓失败: {result}")
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
    scan_all_parser.add_argument('--top', type=int, default=20, help='取前N个高波动币种')
    scan_all_parser.add_argument('--min-vol', type=float, default=3.0, help='最低1h波动率%')
    scan_all_parser.add_argument('--klines', type=int, default=30, help='给多少个候选拿K线')
    scan_all_parser.add_argument('--log', type=str, default=None, help='日志文件（覆盖模式，默认stdout）')

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

    # replace-order: 替换条件单（自动取消旧单+下新单）
    replace_parser = subparsers.add_parser('replace-order', help='替换条件单(自动取消旧单后下新单)')
    replace_parser.add_argument('--symbol', type=str, required=True, help='币种')
    replace_parser.add_argument('--side', type=str, required=True, help="SELL=平多/BUY=平空")
    replace_parser.add_argument('--price', type=str, required=True, help='新触发价格或百分比，如 50000 或 1.01%（相对于当前价，自动确定方向）')
    replace_parser.add_argument('--type', type=str, default='STOP_MARKET',
                                  help="STOP_MARKET/TAKE_PROFIT_MARKET/STOP/TAKE_PROFIT")
    replace_parser.add_argument('--algo-id', type=int, default=None,
                                  help='旧条件单ID（可选）')

    # cancel-conditionals: 取消指定币种全部追踪的委托单
    cancel_cond_parser = subparsers.add_parser('cancel-conditionals', help='取消指定币种全部追踪的委托单')
    cancel_cond_parser.add_argument('--symbol', type=str, required=True, help='币种')
    cancel_cond_parser.add_argument('--side', type=str, default=None,
                                  help='SELL=平多/BUY=平空（可选，不填则取消该币种全部）')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == 'status':
        get_status(args.symbol)

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
        scan_volatility_top(args.top, args.min_vol, args.klines)
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

    elif args.command == 'replace-order':
        trader = BinanceTrader()
        # 优先用命令行 --algo-id，其次从文件查找
        algo_id = args.algo_id
        if not algo_id:
            orders = _load_conditional_orders()
            algo_id = orders.get(args.symbol, {}).get(args.side)
        # 获取数量（优先查持仓，其次从文件）
        positions = trader.get_positions(args.symbol)
        if positions:
            qty = int(max(1, abs(positions[0]['amount'])))
        else:
            # 无持仓时qty传1（条件单数量不影响实际平仓）
            qty = 1
        # 解析 --price：支持绝对价格或百分比
        # 百分比基于：不含杠杆的浮盈%（相对于入场价）
        # SELL（平多）: --price=2% → 触发价 = 入场价 × (1 - 2/100)，即 -2% 浮亏止损
        # BUY  （平空）: --price=2% → 触发价 = 入场价 × (1 + 2/100)，即 +2% 浮亏止损
        price_arg = args.price
        if isinstance(price_arg, str) and price_arg.endswith('%'):
            pct = float(price_arg[:-1])
            if not positions:
                print(f"❌ 无持仓，无法用百分比设置止损（需要入场价）")
                return
            entry_price = float(positions[0].get('entryPrice', 0))
            if entry_price <= 0:
                print(f"❌ 入场价无效: {entry_price}")
                return
            if args.side.upper() == 'SELL':
                trigger_price = entry_price * (1 - pct / 100)
            else:  # BUY
                trigger_price = entry_price * (1 + pct / 100)
            print(f"[replace-order] 入场价={entry_price} → 触发价={trigger_price:.6f} ({pct}%{'亏损' if args.side.upper()=='SELL' else '亏损'}) [基于入场价]")
        else:
            trigger_price = float(price_arg)
        result = trader.replace_conditional_order(
            args.symbol, args.side, qty, trigger_price, args.type, algo_id
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