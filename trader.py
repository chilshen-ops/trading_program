#!/usr/bin/env python3
"""
Binance 永续合约交易机器人 v16.0
顺势交易策略 v8.0：追涨杀跌，不抄底摸顶
自动1.5%止损 + 止损触发检测
"""

import os
import sys
import time
import argparse
import json
from datetime import datetime
from typing import Dict, List, Optional

try:
    import requests
except ImportError:
    print("Error: requests module not installed. Run: pip install requests")
    sys.exit(1)

# Fix SSL: use certifi CA bundle for macOS system SSL issues
import certifi
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()

# ========== 配置 ==========
API_KEY = ""
API_SECRET = ""
FAPI_URL = "https://fapi.binance.com"
PAPI_URL = "https://papi.binance.com"
PROXY = os.environ.get('http_proxy') or os.environ.get('HTTP_PROXY') or ''
SIMULATE = os.environ.get('SIMULATE', 'false').lower() == 'true'

# ========== 模拟交易状态 ==========
import json, os

# ========== 模拟交易状态持久化 ==========
SL_PCT = 0.015  # 止损 1.5%
TP_PCT = 0.05   # 止盈 5%（暂时只做止损）

SIM_STATE_FILE = os.path.join(os.path.dirname(__file__), '.sim_state.json')

def _load_sim_state():
    """从文件加载模拟状态"""
    if os.path.exists(SIM_STATE_FILE):
        try:
            with open(SIM_STATE_FILE, 'r') as f:
                state = json.load(f)
            return (state.get('balance', 1000.0),
                    state.get('positions', {}),
                    state.get('stop_losses', {}))
        except:
            pass
    return 1000.0, {}, {}

def _save_sim_state(balance, positions, stop_losses=None):
    """保存模拟状态到文件"""
    try:
        with open(SIM_STATE_FILE, 'w') as f:
            json.dump({'balance': balance, 'positions': positions, 'stop_losses': stop_losses or {}}, f)
    except:
        pass

# ========== 辅助函数：检查止损 ==========
def check_and_trigger_stops():
    """检查是否触发止损，被scan/llm-hold等命令调用"""
    if not SIMULATE:
        return []
    global _sim_balance, _sim_positions, _sim_stop_losses
    triggered = []
    for sym, sl_price in list(_sim_stop_losses.items()):
        if sym not in _sim_positions:
            del _sim_stop_losses[sym]
            continue
        try:
            klines = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                  params={'symbol': sym, 'interval': '1m', 'limit': 1}, timeout=10).json()
            current_price = float(klines[0][4])
        except:
            continue
        pos = _sim_positions[sym]
        entry = pos['entry_price']
        qty = pos['qty']
        lev = pos['leverage']
        margin = pos['margin']
        side = pos['side']
        # 判断是否触发止损
        triggered_flag = False
        if side == 'SHORT' and current_price >= sl_price:
            triggered_flag = True
        elif side == 'LONG' and current_price <= sl_price:
            triggered_flag = True
        if triggered_flag:
            # 计算止损亏损（固定1.5%，不区分方向）
            loss_pct = SL_PCT
            loss = margin * loss_pct
            # 做空：价格涨到止损价 = 亏；做多：价格跌到止损价 = 亏
            # 平仓价用止损价
            if side == 'SHORT':
                close_price = sl_price
                pnl = (entry - close_price) * qty * lev
            else:
                close_price = sl_price
                pnl = (close_price - entry) * qty * lev
            close_fee = qty * close_price * 0.001
            net_pnl = pnl - close_fee
            _sim_balance += margin - abs(net_pnl)  # 扣除止损损失
            _sim_balance = max(0, _sim_balance)
            del _sim_positions[sym]
            del _sim_stop_losses[sym]
            _save_sim_state(_sim_balance, _sim_positions, _sim_stop_losses)
            msg = f"[SIMULATE] 🔴 止损触发 {sym} @ {close_price:.6f} (entry={entry:.6f}, SL={sl_price:.6f}) 损失={abs(net_pnl):.4f} USDT, 余额={_sim_balance:.4f}"
            print(msg, file=sys.stderr)
            triggered.append(sym)
    return triggered


if SIMULATE:
    _sim_balance, _sim_positions, _sim_stop_losses = _load_sim_state()
    print(f"[SIMULATE] 模拟交易模式已启用 (余额: {_sim_balance:.2f} USDT, 持仓: {len(_sim_positions)}个, 止损单: {len(_sim_stop_losses)}个)", file=sys.stderr)

# ========== binance-connector 客户端（用于 PAPI 签名）==========
sys.path.insert(0, '/Library/Frameworks/Python.framework/Versions/3.13/lib/python3.13/site-packages')
try:
    from binance.client import Client as BinanceClient
except ImportError:
    BinanceClient = None

# PM 账户检测
_is_pm_account = None
_papi_client = None

def is_portfolio_margin() -> bool:
    """检测是否为 Portfolio Margin 账户"""
    global _is_pm_account
    if _is_pm_account is not None:
        return _is_pm_account
    # 直接尝试 PAPI（PM 账户可用，非 PM 账户会报错）
    try:
        client = _get_papi_client()
        if client:
            acct = client.papi_get_account()
            # PM 账户有 totalAvailableBalance 字段
            if 'totalAvailableBalance' in acct or 'uniMMR' in acct:
                _is_pm_account = True
                print(f"[PM] 检测到 Portfolio Margin 账户 (PM_2)", file=sys.stderr)
            else:
                _is_pm_account = False
        else:
            _is_pm_account = False
    except Exception as e:
        _is_pm_account = False
    return _is_pm_account


def _get_papi_client():
    """获取 PAPI 客户端（延迟初始化）"""
    global _papi_client
    if _papi_client is None and BinanceClient is not None:
        _papi_client = BinanceClient(API_KEY, API_SECRET)
    return _papi_client

# ========== Binance API 封装（PAPI 版）==========
class BinanceTrader:
    def __init__(self):
        self.api_key = API_KEY
        self.api_secret = API_SECRET
        self.fapi_url = FAPI_URL
        self.papi_url = PAPI_URL
        self._papi = None  # lazy load

    @property
    def papi(self):
        """延迟加载 PAPI 客户端"""
        if self._papi is None:
            self._papi = _get_papi_client()
        return self._papi

    def _sign(self, params: Dict) -> str:
        """fapi 签名"""
        import hmac, hashlib
        parts = [f"{k}={v}" for k, v in sorted(params.items())]
        return hmac.new(self.api_secret.encode(), '&'.join(parts).encode(), hashlib.sha256).hexdigest()

    def _fapi_request(self, method: str, endpoint: str, params: Dict = None, signed: bool = False) -> Dict:
        """fapi 公共请求（市场数据）"""
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

    # ---- 市场数据（fapi 公开端点）----
    def get_price(self, symbol: str) -> float:
        r = requests.get(f"{self.fapi_url}/fapi/v1/ticker/price",
                        params={'symbol': symbol}, timeout=10)
        return float(r.json()['price'])

    def get_ticker(self, symbol: str) -> Dict:
        r = requests.get(f"{self.fapi_url}/fapi/v1/ticker/24hr",
                        params={'symbol': symbol}, timeout=10)
        return r.json()

    def get_klines(self, symbol: str, interval: str = "30m", limit: int = 100) -> List:
        r = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                        params={'symbol': symbol, 'interval': interval, 'limit': limit}, timeout=10)
        return r.json()

    def get_order_book(self, symbol: str, limit: int = 20) -> Dict:
        r = requests.get(f"{self.fapi_url}/fapi/v1/depth",
                        params={'symbol': symbol, 'limit': limit}, timeout=10)
        return r.json()

    def get_mark_price(self, symbol: str) -> float:
        r = requests.get(f"{self.fapi_url}/fapi/v1/premiumIndex",
                        params={'symbol': symbol}, timeout=10)
        return float(r.json()['markPrice'])

    def get_funding_rate(self, symbol: str) -> Dict:
        r = requests.get(f"{self.fapi_url}/fapi/v1/premiumIndex",
                        params={'symbol': symbol}, timeout=10)
        data = r.json()
        return {
            'fundingRate': float(data.get('lastFundingRate', 0)) * 100,
            'nextFundingTime': data.get('nextFundingTime', '')
        }

    def get_long_short_ratio(self, symbol: str) -> Dict:
        try:
            r = requests.get(f"{self.fapi_url}/futures/data/globalLongShortRatio",
                            params={'symbol': symbol, 'periodType': '1h', 'limit': 10}, timeout=10)
            data = r.json()
            if data:
                latest = data[-1]
                return {
                    'longRatio': float(latest.get('longAccount', 0)) * 100,
                    'shortRatio': float(latest.get('shortAccount', 0)) * 100
                }
        except:
            pass
        return {'longRatio': 50, 'shortRatio': 50}

    # ---- 账户操作（PAPI = 统一账户）----
    def get_account(self) -> Dict:
        """获取账户信息（自动选择 PAPI 或 fapi）"""
        if SIMULATE:
            return {
                'totalAvailableBalance': str(_sim_balance),
                'balances': [{'asset': 'USDT', 'free': str(_sim_balance)}],
                'positions': [{'symbol': s, 'positionAmt': str(-v['qty']), 'entryPrice': str(v['entry_price'])} 
                              for s, v in _sim_positions.items()]
            }
        if is_portfolio_margin():
            return self.papi.papi_get_account()
        params = {'timestamp': int(time.time()*1000)}
        params['signature'] = self._sign(params)
        r = requests.get(f"{self.fapi_url}/fapi/v2/account",
                         headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=10)
        if r.status_code != 200:
            raise Exception(f"fapi account Error {r.status_code}: {r.text}")
        return r.json()

    def get_positions(self, symbol: str = None) -> List[Dict]:
        """获取持仓（PAPI um_position_risk）"""
        if SIMULATE:
            result = []
            for s, v in _sim_positions.items():
                if symbol and s != symbol:
                    continue
                result.append({
                    'symbol': s,
                    'amount': v['qty'],  # 保留小数
                    'entryPrice': v['entry_price'],
                    'unrealizedProfit': 0.0,
                    'leverage': v.get('leverage', 3),
                    'positionSide': v.get('side', 'LONG'),
                    'margin': v.get('margin', 0)
                })
            return result
        if is_portfolio_margin():
            try:
                positions = self.papi.papi_get_um_position_risk()
                result = []
                for pos in positions:
                    amt = float(pos.get('positionAmt', 0))
                    if abs(amt) < 1e-9:  # 忽略 0 持仓
                        continue
                    if symbol and pos.get('symbol') != symbol:
                        continue
                    result.append({
                        'symbol': pos['symbol'],
                        'amount': int(abs(amt)),  # PAPI 数量为整数
                        'entryPrice': float(pos.get('entryPrice', 0)),
                        'unrealizedProfit': float(pos.get('unrealizedPL', 0)),
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
        """获取USDT余额"""
        if SIMULATE:
            return _sim_balance
        if is_portfolio_margin():
            account = self.papi.papi_get_account()
            return float(account.get('totalAvailableBalance', 0))
        account = self.get_account()
        if 'balances' in account:
            for bal in account.get('balances', []):
                if bal.get('asset') == 'USDT':
                    return float(bal.get('free', 0))
        if 'totalAvailableBalance' in account:
            return float(account.get('totalAvailableBalance', 0))
        return 0.0

    # ---- 交易操作（PAPI um_order）----
    def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """设置杠杆（PAPI）"""
        if SIMULATE:
            return {'leverage': leverage, 'symbol': symbol}
        if is_portfolio_margin():
            return self.papi.papi_set_um_leverage(symbol=symbol, leverage=leverage)
        params = {'symbol': symbol, 'leverage': leverage, 'timestamp': int(time.time()*1000)}
        params['signature'] = self._sign(params)
        r = requests.post(f"{self.fapi_url}/fapi/v1/leverage",
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=10)
        if r.status_code != 200:
            raise Exception(f"set_leverage Error {r.status_code}: {r.text}")
        return r.json()

# ========== BinanceTrader 交易方法（做空/做多）==========
    def open_short(self, symbol: str, quantity: float, leverage: int = 10) -> Dict:
        """开空仓（PAPI um_order，单向模式：side=SELL 无需positionSide）"""
        if SIMULATE:
            global _sim_balance, _sim_positions
            # 获取当前价格
            try:
                klines = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=10).json()
                price = float(klines[0][4])
            except:
                price = 0.001
            # 保证金模型: 使用80%余额作为保证金的上限
            available_margin = _sim_balance * 0.8
            # 根据可用保证金反推数量
            qty = max(quantity, 1)
            position_value = qty * price
            margin = position_value / leverage
            # 如果保证金超出可用额度，按比例缩减
            if margin > available_margin:
                qty = available_margin * leverage / price
                if qty < 0.0001:  # 金额太小无法交易
                    raise Exception(f"[SIMULATE] 余额不足: 可用 {available_margin:.2f} USDT 无法开仓 {symbol} (价格={price})")
                position_value = qty * price
                margin = position_value / leverage
            fee = position_value * 0.001  # 0.1% 开仓手续费
            _sim_positions[symbol] = {'qty': qty, 'entry_price': price, 'leverage': leverage, 'margin': margin, 'side': 'SHORT'}
            # 自动设置止损：做空 → 价格涨到 entry*(1+1.5%) 触发
            _sim_stop_losses[symbol] = price * (1 + SL_PCT)
            _sim_balance -= (margin + fee)  # 扣除保证金和手续费
            _save_sim_state(_sim_balance, _sim_positions, _sim_stop_losses)  # 持久化
            print(f"[SIMULATE] 开空 {symbol} x{qty:.4f} @ {price}, 保证金={margin:.2f} USDT, 手续费={fee:.4f} USDT, 余额={_sim_balance:.4f}", file=sys.stderr)
            return {'orderId': 'sim_' + str(time.time()), 'symbol': symbol, 'side': 'SELL', 'origQty': str(qty), 'margin': margin}
        try:
            self.set_leverage(symbol, leverage)
            time.sleep(0.2)
        except Exception as e:
            print(f"[WARN] 设置杠杆失败（继续开仓）: {e}", file=sys.stderr)

        if is_portfolio_margin():
            # 单向模式：side=SELL 开空，side=BUY 平空
            # PAPI 要求整数
            qty_int = int(quantity)
            if qty_int == 0:
                qty_int = 1
            return self.papi.papi_create_um_order(
                symbol=symbol,
                side='SELL',
                type='MARKET',
                quantity=qty_int
            )
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
        return r.json()

    def open_long(self, symbol: str, quantity: float, leverage: int = 10) -> Dict:
        """开多仓（PAPI um_order，单向模式：side=BUY 开多）"""
        if SIMULATE:
            global _sim_balance, _sim_positions
            try:
                klines = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=10).json()
                price = float(klines[0][4])
            except:
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
            _sim_positions[symbol] = {'qty': qty, 'entry_price': price, 'leverage': leverage, 'margin': margin, 'side': 'LONG'}
            # 自动设置止损：做多 → 价格跌到 entry*(1-1.5%) 触发
            _sim_stop_losses[symbol] = price * (1 - SL_PCT)
            _sim_balance -= (margin + fee)
            _save_sim_state(_sim_balance, _sim_positions, _sim_stop_losses)
            print(f"[SIMULATE] 开多 {symbol} x{qty:.4f} @ {price}, 保证金={margin:.2f} USDT, 手续费={fee:.4f} USDT, 余额={_sim_balance:.4f}", file=sys.stderr)
            return {'orderId': 'sim_' + str(time.time()), 'symbol': symbol, 'side': 'BUY', 'origQty': str(qty), 'margin': margin}
        try:
            self.set_leverage(symbol, leverage)
            time.sleep(0.2)
        except Exception as e:
            print(f"[WARN] 设置杠杆失败（继续开仓）: {e}", file=sys.stderr)

        if is_portfolio_margin():
            qty_int = int(quantity)
            if qty_int == 0:
                qty_int = 1
            return self.papi.papi_create_um_order(
                symbol=symbol,
                side='BUY',
                type='MARKET',
                quantity=qty_int
            )
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
        return r.json()

    def close_long(self, symbol: str, quantity: float = None) -> Dict:
        """平多仓（PAPI um_order，单向模式：side=SELL 平多）"""
        if quantity is None:
            positions = self.get_positions(symbol)
            for pos in positions:
                if pos.get('positionSide') == 'LONG' or (pos['amount'] > 0):
                    quantity = abs(pos['amount'])
                    break
        if quantity is None or quantity <= 0:
            raise Exception(f"No long position found for {symbol}")

        qty_int = int(quantity)
        if qty_int == 0:
            qty_int = 1

        if SIMULATE:
            global _sim_balance, _sim_positions
            if symbol not in _sim_positions:
                raise Exception(f"[SIMULATE] 无持仓: {symbol}")
            pos = _sim_positions[symbol]
            entry = pos['entry_price']
            margin = pos['margin']
            lev = pos['leverage']
            qty = pos['qty']
            try:
                klines = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=10).json()
                price = float(klines[0][4])
            except:
                price = entry
            pnl = (price - entry) * qty * lev
            close_fee = (qty * price) * 0.001
            net_pnl = pnl - close_fee
            _sim_balance += margin + net_pnl
            del _sim_positions[symbol]
            _sim_stop_losses.pop(symbol, None)
            _save_sim_state(_sim_balance, _sim_positions, _sim_stop_losses)
            print(f"[SIMULATE] 平多 {symbol} x{qty:.4f} @ {price}, 盈亏={net_pnl:.4f} USDT, 余额={_sim_balance:.4f}", file=sys.stderr)
            return {'orderId': 'sim_' + str(time.time()), 'symbol': symbol, 'side': 'SELL', 'origQty': str(qty), 'pnl': net_pnl, 'margin': margin}
        if is_portfolio_margin():
            return self.papi.papi_create_um_order(
                symbol=symbol,
                side='SELL',
                type='MARKET',
                quantity=qty_int
            )
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

    def close_short(self, symbol: str, quantity: float = None) -> Dict:
        """平空仓（PAPI um_order，单向模式：side=BUY 平空）"""
        if quantity is None:
            positions = self.get_positions(symbol)
            for pos in positions:
                # PM账户amount取绝对值，用positionSide判断
                if pos.get('positionSide') == 'SHORT' or (pos['amount'] < 0):
                    quantity = abs(pos['amount'])
                    break
        if quantity is None or quantity <= 0:
            raise Exception(f"No short position found for {symbol}")

        # PAPI 要求整数数量
        qty_int = int(quantity)
        if qty_int == 0:
            qty_int = 1

        if SIMULATE:
            global _sim_balance, _sim_positions
            if symbol not in _sim_positions:
                raise Exception(f"[SIMULATE] 无持仓: {symbol}")
            pos = _sim_positions[symbol]
            entry = pos['entry_price']
            margin = pos['margin']
            lev = pos['leverage']
            qty = pos['qty']  # 使用持仓中的数量（可能是小数）
            try:
                klines = requests.get(f"{self.fapi_url}/fapi/v1/klines",
                                      params={'symbol': symbol, 'interval': '1m', 'limit': 1}, timeout=10).json()
                price = float(klines[0][4])
            except:
                price = entry
            # 空仓盈亏: (开仓价 - 平仓价) × 数量 × 杠杆
            pnl = (entry - price) * qty * lev
            close_fee = (qty * price) * 0.001  # 平仓手续费
            net_pnl = pnl - close_fee
            _sim_balance += margin + net_pnl  # 退回保证金 + 盈亏 - 平仓费
            del _sim_positions[symbol]
            _sim_stop_losses.pop(symbol, None)
            _save_sim_state(_sim_balance, _sim_positions, _sim_stop_losses)  # 持久化
            print(f"[SIMULATE] 平空 {symbol} x{qty:.4f} @ {price}, 盈亏={net_pnl:.4f} USDT (PnL={pnl:.4f}, 平仓费={close_fee:.4f}), 余额={_sim_balance:.4f}", file=sys.stderr)
            return {'orderId': 'sim_' + str(time.time()), 'symbol': symbol, 'side': 'BUY', 'origQty': str(qty), 'pnl': net_pnl, 'margin': margin}
        if is_portfolio_margin():
            # 单向模式：side=BUY 平空
            return self.papi.papi_create_um_order(
                symbol=symbol,
                side='BUY',
                type='MARKET',
                quantity=qty_int
            )
        params = {
            'symbol': symbol,
            'side': 'BUY',
            'positionSide': 'SHORT',
            'type': 'MARKET',
            'quantity': quantity,
            'timestamp': int(time.time()*1000)
        }
        params['signature'] = self._sign(params)
        r = requests.post(f"{self.fapi_url}/fapi/v1/order",
                          headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=10)
        if r.status_code != 200:
            raise Exception(f"close_short Error {r.status_code}: {r.text}")
        return r.json()

    def get_position_mode(self) -> Dict:
        """获取持仓模式"""
        if is_portfolio_margin():
            return self.papi.papi_get_um_position_side_dual()
        params = {'timestamp': int(time.time()*1000)}
        params['signature'] = self._sign(params)
        r = requests.get(f"{self.fapi_url}/fapi/v1/positionSide/dual",
                         headers={'X-MBX-APIKEY': self.api_key}, params=params, timeout=10)
        if r.status_code != 200:
            raise Exception(f"positionMode Error {r.status_code}: {r.text}")
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

        # Signal线 (EMA9 of MACD)
        # 简化计算
        signal = macd_line * 0.9  # 近似

        histogram = macd_line - signal

        return {
            'macd': round(macd_line, 4),
            'signal': round(signal, 4),
            'histogram': round(histogram, 4)
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
    """获取历史高点（ALL-TIME HIGH）"""
    try:
        # 用1D K线取最大范围（limit=500，约2年）
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
    """获取近期高点（7天、30天）"""
    result = {}
    for days in periods:
        try:
            limit = min(days * 24, 500)  # 1h周期，最多500根
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
        except:
            result[f'{days}d_high'] = 0
            result[f'{days}d_pct'] = 0
    return result


def detect_reversal_signals(symbol: str) -> Dict:
    """检测见顶回落信号（只用5m+30m，不足时fallback到1h）"""
    _empty = {'score': 0, 'reasons': [], 'rsi_14': 50, 'rsi_7': 50, 'rsi_5m': 50,
              'ma5_dev': 0, 'ma20_dev': 0, 'vol_ratio': 1, 'waterfall': False,
              'macd_dead_cross': False, 'multi_rsi_overbought': 0}
    try:
        klines_5m = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                  params={'symbol': symbol, 'interval': '5m', 'limit': 20}, timeout=10).json()
        klines_30m = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                  params={'symbol': symbol, 'interval': '30m', 'limit': 10}, timeout=10).json()

        if len(klines_5m) < 10 or len(klines_30m) < 5:
            return _empty

        closes_5m = [float(k[4]) for k in klines_5m]
        closes_30m = [float(k[4]) for k in klines_30m]
        volumes_5m = [float(k[5]) for k in klines_5m]

        rsi_14 = TechnicalIndicators.calculate_rsi(closes_30m, 14)
        rsi_7 = TechnicalIndicators.calculate_rsi(closes_30m, 7)
        rsi_5m = TechnicalIndicators.calculate_rsi(closes_5m, 14)

        # MACD 30M
        macd_30m = TechnicalIndicators.calculate_macd(closes_30m)

        # 短期均线偏离
        ma5 = TechnicalIndicators.calculate_ma(closes_30m, 5)
        ma20 = TechnicalIndicators.calculate_ma(closes_30m, 20)
        current = closes_30m[-1]

        ma5_dev = ((current - ma5) / ma5 * 100) if ma5 > 0 else 0
        ma20_dev = ((current - ma20) / ma20 * 100) if ma20 > 0 else 0

        # 成交量衰竭检测（最近5根 vs 前5根）
        recent_vol = sum(volumes_5m[-5:]) / 5
        prev_vol = sum(volumes_5m[-10:-5]) / 5
        vol_ratio = recent_vol / prev_vol if prev_vol > 0 else 1

        # 价格创新高但RSI背离（价格新高但RSI低于前期高点）
        rsi_swing_high = max([TechnicalIndicators.calculate_rsi(closes_30m[:i+14], 14)
                             for i in range(50, len(closes_30m)-14)])

        # 多周期RSI共振超买
        multi_rsi_overbought = (rsi_14 > 70) + (rsi_7 > 75) + (rsi_5m > 70)

        # 瀑布信号：最近3根K线收盘价连续下降
        last_3 = closes_30m[-3:]
        waterfall = all(last_3[i] > last_3[i+1] for i in range(2))

        # MACD死叉信号
        macd_dead_cross = macd_30m['macd'] < macd_30m['signal'] and macd_30m['histogram'] < 0

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
        if macd_dead_cross:
            score += 10
            reasons.append('MACD死叉')
        if rsi_5m > 70:
            score += 10
            reasons.append(f'5m_RSI超买')

        return {
            'score': score,
            'reasons': reasons,
            'rsi_14': rsi_14,
            'rsi_7': rsi_7,
            'rsi_5m': rsi_5m,
            'ma5_dev': round(ma5_dev, 2),
            'ma20_dev': round(ma20_dev, 2),
            'vol_ratio': round(vol_ratio, 2),
            'waterfall': waterfall,
            'macd_dead_cross': macd_dead_cross,
            'multi_rsi_overbought': multi_rsi_overbought
        }
    except Exception as e:
        return {'score': 0, 'reasons': [], 'rsi_14': 50, 'rsi_7': 50, 'rsi_5m': 50,
                'ma5_dev': 0, 'ma20_dev': 0, 'vol_ratio': 1, 'waterfall': False,
                'macd_dead_cross': False, 'multi_rsi_overbought': 0}



# ========== LLM 分析模块 ==========
def format_for_llm(symbol: str, action: str = "open") -> str:
    """格式化 K 线数据供 LLM 分析（只用5m+30m+1h）"""
    from datetime import datetime
    
    # 只用5m+30m+1h
    # 5m:10根, 30m:40根, 1h:80根
    intervals = ["5m", "30m", "1h"]
    limits = {"5m": 10, "30m": 40, "1h": 80}
    result = {}
    
    for interval in intervals:
        try:
            r = requests.get(
                f"{FAPI_URL}/fapi/v1/klines",
                params={'symbol': symbol, 'interval': interval, 'limit': limits[interval]},
                timeout=10
            )
            result[interval] = r.json()
        except:
            result[interval] = []
    
    lines = [f"# {symbol} {'开仓' if action == 'open' else '持仓'}分析", ""]
    lines.append(f"现在需要你判断：{'是否应该做空' if action == 'open' else '是否应该平仓'}")
    lines.append("")
    
    for interval, klines in result.items():
        if not klines:
            continue
        
        closes = [float(k[4]) for k in klines]
        opens = [float(k[1]) for k in klines]
        highs = [float(k[2]) for k in klines]
        
        total_change = ((closes[-1] - opens[0]) / opens[0] * 100) if opens[0] > 0 else 0
        
        consecutive = 0
        trend_desc = ""
        for i in range(len(closes)-1, 0, -1):
            if closes[i] > closes[i-1]:
                consecutive += 1
                trend_desc = "连涨"
            elif closes[i] < closes[i-1]:
                consecutive -= 1
                trend_desc = "连跌"
            else:
                break
        
        recent5 = closes[-5:]
        recent_trend = "震荡"
        if len(recent5) >= 3:
            if recent5[-1] > recent5[-2] > recent5[-3]:
                recent_trend = "上涨中"
            elif recent5[-1] < recent5[-2] < recent5[-3]:
                recent_trend = "下跌中"
        
        current = closes[-1]
        period_high = max(highs)
        dist_from_high = ((current - period_high) / period_high * 100) if period_high > 0 else 0
        
        lines.append(f"## {interval} K线 ({len(klines)}根)")
        lines.append(f"总变化: {total_change:+.2f}% | 距周期最高: {dist_from_high:+.2f}%")
        lines.append(f"近期趋势: {recent_trend} | 连续: {consecutive}根 {trend_desc}")
        lines.append("")
        
        recent = klines[-20:] if len(klines) >= 20 else klines
        lines.append(f"{'时间':<12} {'开盘':>10} {'收盘':>10} {'涨跌':>6}")
        lines.append("-" * 45)
        
        for k in recent:
            ts = k[0] / 1000
            dt = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')
            o = float(k[1]); c = float(k[4])
            change = c - o
            change_pct = (change / o * 100) if o > 0 else 0
            emoji = "📈" if change >= 0 else "📉"
            lines.append(f"{dt:<12} {o:>10.4f} {c:>10.4f} {emoji}{change_pct:+5.1f}%")
        lines.append("")
    
    if action == "open":
        lines.append("请分析以上数据，判断是否应该做空：1. 是否在高位刚开始下跌？2. 形态是否出现顶部信号？3. 预期跌幅多大？决策格式：{'decision': 'YES/NO', 'reason': '...', 'confidence': 60-100, 'expected_drop': '5-20%或不确定'}")
    else:
        lines.append("请分析空仓是否应该平仓：1. 下跌是否已经完成？2. 是否出现止跌信号？决策格式：{'decision': 'YES/NO', 'reason': '...', 'confidence': 60-100}")
    
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



def scan_short_candidates(min_change: float = 10) -> List[Dict]:
    """
    扫描做空候选币种
    逻辑：涨幅最大 → 确认新高 → 检测见顶信号
    """
    print(f"\n{'='*60}")
    print(f"📈 做空候选扫描（涨幅 >= {min_change}%，找见顶信号）")
    print(f"{'='*60}")

    # 获取所有24h行情（只取10个）
    r = requests.get(f"{FAPI_URL}/fapi/v1/ticker/24hr", params={"limit": 10}, timeout=30)
    all_tickers = r.json()

    # 按涨幅排序，取Top 10
    usdt_pairs = [t for t in all_tickers if isinstance(t, dict) and t.get('symbol', '').endswith('USDT')]
    sorted_tickers = sorted(usdt_pairs, key=lambda x: float(x.get('priceChangePercent', 0)), reverse=True)
    top_tickers = sorted_tickers[:10]

    candidates = []

    for ticker in top_tickers:
        symbol = ticker.get('symbol', '')
        change_24h = float(ticker.get('priceChangePercent', 0))

        if change_24h < min_change:
            continue

        try:
            # 1. 检查是否在历史高点附近
            ath_data = get_ath_price(symbol)
            recent_data = get_recent_highs(symbol)

            # 2. 检测见顶信号
            reversal = detect_reversal_signals(symbol)

            # 综合评分：24h涨幅 + 见顶信号分
            # 涨幅权重：每1%涨幅 = 1分
            # ATH接近度：距离-5%内 = +30分
            # 反转信号：直接加分
            ath_score = 0
            if ath_data['ath'] > 0:
                ath_distance_pct = ((ath_data['current'] - ath_data['ath']) / ath_data['ath'] * 100)
                if ath_distance_pct >= -2:  # 距离ATH 2%以内
                    ath_score = 40
                elif ath_distance_pct >= -5:
                    ath_score = 25
                elif ath_distance_pct >= -10:
                    ath_score = 15

            # 7日高点接近度
            day7_score = 0
            if recent_data.get('7d_high', 0) > 0:
                if recent_data['7d_pct'] >= -2:
                    day7_score = 20
                elif recent_data['7d_pct'] >= -5:
                    day7_score = 10

            total_score = change_24h + ath_score + day7_score + reversal['score']

            candidate = {
                'symbol': symbol,
                'price': ath_data.get('current', 0),
                'change_24h': round(change_24h, 2),
                'ath': ath_data.get('ath', 0),
                'ath_distance_pct': round(((ath_data.get('current', 0) - ath_data.get('ath', 1)) / ath_data.get('ath', 1) * 100), 3) if ath_data.get('ath', 0) > 0 else 0,
                '7d_high': recent_data.get('7d_high', 0),
                '7d_pct': recent_data.get('7d_pct', 0),
                'reversal_score': reversal['score'],
                'reversal_reasons': reversal['reasons'],
                'total_score': round(total_score, 1),
                'rsi_14': reversal.get('rsi_14', 0),
                'vol_ratio': reversal.get('vol_ratio', 1),
                'is_waterfall': reversal.get('waterfall', False),
                'macd_dead_cross': reversal.get('macd_dead_cross', False)
            }

            candidates.append(candidate)

        except Exception as e:
            continue

    # 按总分排序
    candidates.sort(key=lambda x: x['total_score'], reverse=True)

    print(f"\n扫描完成，找到 {len(candidates)} 个候选做空币种")
    print(f"\n{'='*60}")
    print(f"🏆 做空候选 TOP 10（按综合评分排序）")
    print(f"{'='*60}")
    print(f"{'排名':<4} {'币种':<14} {'24h涨幅':>8} {'ATH距离':>8} {'7日距离':>8} {'反转分':>6} {'总分':>6}")
    print(f"{'-'*60}")

    for i, c in enumerate(candidates[:10]):
        ath_str = f"{c['ath_distance_pct']:+.2f}%" if c['ath_distance_pct'] != 0 else "N/A"
        d7_str = f"{c['7d_pct']:+.2f}%" if c['7d_pct'] != 0 else "N/A"
        print(f"{i+1:<4} {c['symbol']:<14} {c['change_24h']:>+7.2f}% {ath_str:>8} {d7_str:>8} {c['reversal_score']:>5} {c['total_score']:>6.1f}")

    # 打印Top 3详细信息
    for i, c in enumerate(candidates[:3]):
        print(f"\n{'='*60}")
        print(f"📊 #{i+1} {c['symbol']} 详细分析")
        print(f"{'='*60}")
        print(f"价格: ${c['price']:.6f} | 24h涨幅: +{c['change_24h']:.2f}%")
        print(f"ATH: ${c['ath']:.6f} (距离: {c['ath_distance_pct']:+.2f}%)")
        if c['7d_high'] > 0:
            print(f"7日高: ${c['7d_high']:.6f} (距离: {c['7d_pct']:+.2f}%)")
        print(f"反转信号分: {c['reversal_score']} / 100")
        print(f"RSI(14): {c['rsi_14']}")
        print(f"量比: {c['vol_ratio']:.2f}x")
        print(f"瀑布形态: {'✅' if c['is_waterfall'] else '❌'} | MACD死叉: {'✅' if c['macd_dead_cross'] else '❌'}")
        if c['reversal_reasons']:
            print(f"做空理由: {' | '.join(c['reversal_reasons'][:5])}")

    return candidates


# ========== 主程序（扫描 + 指标 + 账户操作 + 命令执行）==========

def get_all_perpetual_symbols() -> List[str]:
    """获取所有USDT永续合约"""
    r = requests.get(f"{FAPI_URL}/fapi/v1/exchangeInfo", timeout=10)
    data = r.json()

    symbols = []
    for s in data.get('symbols', []):
        if (s.get('contractType') == 'PERPETUAL' and
            s.get('quoteAsset') == 'USDT' and
            s.get('status') == 'TRADING'):
            symbols.append(s.get('symbol'))

    return symbols


def detect_bottom_signals(symbol: str) -> Dict:
    """检测见底反弹信号（只用5m+30m，不足时fallback到1h）"""
    _empty = {'score': 0, 'reasons': [], 'rsi_14': 50, 'rsi_7': 50, 'rsi_5m': 50,
              'ma5_dev': 0, 'ma20_dev': 0, 'vol_ratio': 1, 'rebound': False,
              'macd_golden_cross': False, 'multi_rsi_oversold': 0}
    try:
        klines_5m = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                  params={'symbol': symbol, 'interval': '5m', 'limit': 20}, timeout=10).json()
        klines_30m = requests.get(f"{FAPI_URL}/fapi/v1/klines",
                                   params={'symbol': symbol, 'interval': '30m', 'limit': 10}, timeout=10).json()

        if len(klines_5m) < 10 or len(klines_30m) < 5:
            return _empty

        closes_5m = [float(k[4]) for k in klines_5m]
        closes_30m = [float(k[4]) for k in klines_30m]
        volumes_5m = [float(k[5]) for k in klines_5m]

        rsi_14 = TechnicalIndicators.calculate_rsi(closes_30m, 14)
        rsi_7 = TechnicalIndicators.calculate_rsi(closes_30m, 7)
        rsi_5m = TechnicalIndicators.calculate_rsi(closes_5m, 14)

        macd_30m = TechnicalIndicators.calculate_macd(closes_30m)
        ma5 = TechnicalIndicators.calculate_ma(closes_30m, 5)
        ma20 = TechnicalIndicators.calculate_ma(closes_30m, 20)
        current = closes_30m[-1]

        ma5_dev = ((current - ma5) / ma5 * 100) if ma5 > 0 else 0
        ma20_dev = ((current - ma20) / ma20 * 100) if ma20 > 0 else 0

        recent_vol = sum(volumes_5m[-5:]) / 5
        prev_vol = sum(volumes_5m[-10:-5]) / 5
        vol_ratio = recent_vol / prev_vol if prev_vol > 0 else 1

        # 瀑布反弹信号：最近3根K线收盘价连续上升
        last_3 = closes_30m[-3:]
        rebound = all(last_3[i] < last_3[i+1] for i in range(2))

        # MACD金叉信号
        macd_golden_cross = macd_30m['macd'] > macd_30m['signal'] and macd_30m['histogram'] > 0

        # 多周期RSI共振超卖
        multi_rsi_oversold = (rsi_14 < 30) + (rsi_7 < 25) + (rsi_5m < 30)

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
        if macd_golden_cross:
            score += 10
            reasons.append('MACD金叉')
        if rsi_5m < 30:
            score += 10
            reasons.append(f'5m_RSI超卖')

        return {
            'score': score,
            'reasons': reasons,
            'rsi_14': rsi_14,
            'rsi_7': rsi_7,
            'rsi_5m': rsi_5m,
            'ma5_dev': round(ma5_dev, 2),
            'ma20_dev': round(ma20_dev, 2),
            'vol_ratio': round(vol_ratio, 2),
            'rebound': rebound,
            'macd_golden_cross': macd_golden_cross,
            'multi_rsi_oversold': multi_rsi_oversold
        }
    except Exception as e:
        return {'score': 0, 'reasons': [], 'rsi_14': 50, 'rsi_7': 50, 'rsi_5m': 50,
                'ma5_dev': 0, 'ma20_dev': 0, 'vol_ratio': 1, 'rebound': False,
                'macd_golden_cross': False, 'multi_rsi_oversold': 0}


def scan_long_candidates(min_change: float = -10, max_change: float = -3) -> List[Dict]:
    """
    扫描做多候选币种（两阶段：快速初筛 + 精细分析）
    优化：250个24h ticker → 取跌幅最大的50个 → 精细K线分析
    """
    print(f"\n{'='*60}")
    print(f"📉 做多候选扫描（跌幅 {max_change}% ~ {min_change}%，找超跌反弹）")
    print(f"{'='*60}")

    r = requests.get(f"{FAPI_URL}/fapi/v1/ticker/24hr", params={"limit": 10}, timeout=30)
    all_tickers = r.json()

    usdt_pairs = [t for t in all_tickers if isinstance(t, dict) and t.get('symbol', '').endswith('USDT')]
    sorted_tickers = sorted(usdt_pairs, key=lambda x: float(x.get('priceChangePercent', 0)))

    # 第一阶段：快速筛选（只取10个）
    quick_candidates = []
    for ticker in sorted_tickers[:10]:
        symbol = ticker.get('symbol', '')
        change_24h = float(ticker.get('priceChangePercent', 0))
        if not (min_change <= change_24h <= max_change):
            continue
        # 跌幅基础分：-3%→13分，-10%→45分
        drop_score = min(abs(change_24h) * 4.5, 45)
        quick_candidates.append({
            'symbol': symbol,
            'price': float(ticker.get('lastPrice', 0)),
            'change_24h': round(change_24h, 2),
            'drop_score': round(drop_score, 1),
        })

    print(f"初筛完成，找到 {len(quick_candidates)} 个候选（耗时<1s）")
    print(f"正在进行精细K线分析（只分析Top 50）...")

    # 第二阶段：按跌幅排序，取分数最高的50个做精细分析
    quick_candidates.sort(key=lambda x: x['drop_score'], reverse=True)
    top_candidates = quick_candidates[:50]

    results = []
    for c in top_candidates:
        try:
            bottom = detect_bottom_signals(c['symbol'])
            bottom_score = bottom['score']
            # 综合分 = 跌幅分×0.4 + 反弹信号分
            total_score = c['drop_score'] * 0.4 + bottom_score

            results.append({
                'symbol': c['symbol'],
                'price': c['price'],
                'change_24h': c['change_24h'],
                'bottom_score': bottom_score,
                'bottom_reasons': bottom['reasons'],
                'total_score': round(total_score, 1),
                'rsi_14': bottom.get('rsi_14', 50),
                'rsi_7': bottom.get('rsi_7', 50),
                'vol_ratio': bottom.get('vol_ratio', 1),
                'is_rebound': bottom.get('rebound', False),
                'macd_golden_cross': bottom.get('macd_golden_cross', False)
            })
        except Exception as e:
            continue

    results.sort(key=lambda x: x['total_score'], reverse=True)

    print(f"\n扫描完成，找到 {len(results)} 个候选做多币种")
    print(f"\n{'='*60}")
    print(f"🏆 做多候选 TOP 10（按综合评分排序）")
    print(f"{'='*60}")
    print(f"{'排名':<4} {'币种':<14} {'24h跌幅':>8} {'跌幅分':>8} {'反弹分':>8} {'总分':>6}")
    print(f"{'-'*60}")

    for i, c in enumerate(results[:10]):
        print(f"{i+1:<4} {c['symbol']:<14} {c['change_24h']:>+7.2f}% {c['change_24h']*4.5:>8.1f} {c['bottom_score']:>8} {c['total_score']:>6.1f}")

    for i, c in enumerate(results[:3]):
        print(f"\n--- TOP {i+1}: {c['symbol']} ---")
        print(f"  跌幅: {c['change_24h']:+.2f}%")
        print(f"  RSI14: {c['rsi_14']:.1f}, RSI7: {c['rsi_7']:.1f}")
        print(f"  量比: {c['vol_ratio']:.2f}x")
        print(f"  反弹信号: {c['is_rebound']}, MACD金叉: {c['macd_golden_cross']}")
        print(f"  原因: {', '.join(c['bottom_reasons'])}")

    return results


def scan_candidates(min_change: float = 10) -> List[Dict]:
    """扫描涨幅超过门槛的币种（只获取10个）"""
    print(f"\n{'='*60}")
    print(f"🔍 扫描候选币种(涨幅 >= {min_change}%，只分析10个)")
    print(f"{'='*60}")

    r = requests.get(f"{FAPI_URL}/fapi/v1/ticker/24hr", params={"limit": 10}, timeout=30)
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


# ========== 统一波动率扫描（多空一起获取）==========

# 速率限制：防止IP被封
_REQUEST_INTERVAL = 0.1  # 每次请求间隔(秒)，币安限制约1200/分钟
_last_request_time = 0

def _rate_limit():
    """简单速率限制"""
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if elapsed < _REQUEST_INTERVAL:
        time.sleep(_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.time()


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
            # 空数据，重试
            time.sleep(0.2)
            continue
        except Exception as e:
            # 网络错误，重试
            time.sleep(0.3)
            continue
    # 3次都失败，打印警告
    print(f"  ⚠️ {symbol} {interval} K线获取失败", file=sys.stderr)
    return []

def _format_klines_for_llm(klines: List, interval: str) -> str:
    """Format klines into LLM-readable text"""
    from datetime import datetime
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
        o = float(k[1]); c = float(k[4]); h = float(k[2]); l = float(k[3])
        chg = (c - o) / o * 100 if o > 0 else 0
        body = "UP" if c > o else "DOWN"
        upper_shadow = h - max(o, c)
        lower_shadow = min(o, c) - l
        lines.append(f"  {dt} O:{o:.5f} C:{c:.5f} H:{h:.5f} L:{l:.5f} {body}{chg:+.2f}% U:{upper_shadow:.5f} L:{lower_shadow:.5f}")
    lines.append("")
    return "\n".join(lines)

def llm_analyze_batch(coins: List[Dict]) -> Dict[str, Dict]:
    """
    Format all coin data and PRINT for the LLM (this OpenClaw session) to analyze.
    No external API calls. LLM analysis happens in the OpenClaw conversation.
    """
    print("\n" + "="*70)
    print("ANALYSIS DATA - COPY THIS FOR LLM ANALYSIS")
    print("="*70)

    for c in coins:
        sym = c['symbol']
        price = c['price']
        change = c['change_24h']
        vol = c['vol_24h_pct']
        high = c['high_24h']
        low = c['low_24h']
        pos = ((price - low) / (high - low) * 100) if (high - low) > 0 else 50

        print(f"\n{'='*60}")
        print(f"[COIN] {sym}")
        print(f"Price={price:.6f} | 24h Change={change:+.2f}% | Vol={vol:.1f}%")
        print(f"24h Range: High={high:.6f} Low={low:.6f} | Position={pos:.0f}% of range")
        print(f"\n--- 5m Klines ---")
        formatted_5m = _format_klines_for_llm(c.get('klines_5m', []), "5m")
        if formatted_5m:
            print(formatted_5m)
        else:
            print("  (no data)")
        print(f"\n--- 30m Klines ---")
        formatted_30m = _format_klines_for_llm(c.get('klines_30m', []), "30m")
        if formatted_30m:
            print(formatted_30m)
        else:
            print("  (no data)")

    print(f"\n{'='*70}")
    print("ANALYSIS QUESTION")
    print("="*70)
    print("""
For each coin above, decide: LONG / SHORT / PASS

⚠️ 顺势交易规则（v8.1）：趋势是你的朋友，不要抄底摸顶

判断规则：
- 涨了还会涨：24h涨幅≥20% + 位置>70% + 无见顶信号 → LONG（追涨）
- 跌了还会跌：24h跌幅≥15% + 位置<30% + 无见底信号 → SHORT（杀跌）
- 高位有见顶信号 → PASS（不做空）
- 低位有见底信号 → PASS（不做多）
- 逆势（高位做空/低位做多）→ PASS

⚠️ 禁止：高位做空、低位做多

只推荐置信度≥50的机会，最多2个

输出格式：
  [COIN] SYMBOL
  Direction: LONG/SHORT | Confidence: HIGH/MEDIUM/LOW | Reason: ...
""")

    # 程序只返回原始数据，决策全交给LLM
    # 不做任何指标计算或方向判断
    print("\n[DATA READY] 所有K线数据已获取，等待LLM分析...")
    return {}


def scan_volatility_top(top_n: int = 10, min_vol: float = 3.0, top_klines: int = 30) -> List[Dict]:
    """
    Unified scan: Binance sortBy server-side ranking
    Get coins, fetch klines, hand to LLM for unified LONG/SHORT analysis
    
    过滤逻辑：
    1. 只取 status=TRADING 的币种
    2. 排除 blacklist.json 黑名单
    3. 过滤 minNotional < 50U 的币（可交易）
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

    # 获取可交易币种（只取 TRADING 状态）
    _rate_limit()
    r = requests.get(f"{FAPI_URL}/fapi/v1/exchangeInfo", timeout=10)
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
    r = requests.get(
        f"{FAPI_URL}/fapi/v1/ticker/24hr",
        params={"limit": top_n * 3, "sortBy": "priceChangePercent", "sortType": "DESC"},
        timeout=30
    )
    all_tickers: List[Dict] = r.json()
    usdt_pairs = [t for t in all_tickers if isinstance(t, dict) and t.get('symbol', '').endswith('USDT')]
    print(f"Binance sorted: {len(usdt_pairs)} candidates")

    candidates = []
    for ticker in usdt_pairs:
        sym = ticker.get('symbol', '')
        
        # 过滤：黑名单 + 非交易状态
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

    # 批量获取K线（每次2个API：5m+30m），避免超限
    # top_klines 控制实际取K线的数量
    kline_coins = top_vol[:top_klines]
    for i, c in enumerate(kline_coins):
        sym = c['symbol']
        print(f"  [{i+1}/{len(top_vol)}] {sym}...")
        klines_5m  = _get_klines_raw(sym, '5m', 20)
        klines_30m = _get_klines_raw(sym, '30m', 10)
        c['klines_5m'] = klines_5m
        c['klines_30m'] = klines_30m

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

    # Final report
    print(f"\n{'='*70}")
    print(f"FINAL RECOMMENDATIONS")
    print(f"{'='*70}")
    print(f"{'Rank':<4} {'Symbol':<14} {'24h Chg':>8} {'Vol%':>7} {'Direction':>7} {'Conf':>5}")
    print(f"{'-'*70}")
    top_vol.sort(key=lambda x: x.get('confidence', 0), reverse=True)
    for i, c in enumerate(top_vol):
        emoji = '🔴' if c.get('direction') == 'SHORT' else ('🟢' if c.get('direction') == 'LONG' else '⚪')
        print(f"{i+1:<4} {emoji} {c['symbol']:<20} {c['change_24h']:>+8.2f}% | 波动{c['vol_24h_pct']:>5.1f}% | {c.get('direction','NEUTRAL'):>7} | 置信{c.get('confidence',0):>3}")

    print(f"\n{'='*70}")
    print("TOP TRADE OPPORTUNITIES (TOP 2 ONLY)")
    print(f"{'='*70}")
    actionables = [c for c in top_vol if c.get('direction') != 'NEUTRAL' and c.get('confidence', 0) >= 50]
    actionables.sort(key=lambda x: x.get('confidence', 0), reverse=True)
    for i, c in enumerate(actionables[:2]):  # Max 2
        emoji = 'SHORT' if c.get('direction') == 'SHORT' else 'LONG'
        print(f"  {i+1}. [{emoji}] {c['symbol']} | Conf:{c.get('confidence',0)} | {c.get('llm_reason','')}")

    return top_vol




def get_market_data(symbol: str, kline_count: int = 5) -> Dict:
    """获取市场数据""" 
    trader = BinanceTrader()

    # K线数据
    klines = trader.get_klines(symbol, "30m", limit=100)

    # 提取价格数据
    closes = [float(k[4]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]

    # 技术指标
    rsi = TechnicalIndicators.calculate_rsi(closes)
    macd = TechnicalIndicators.calculate_macd(closes)
    bb = TechnicalIndicators.calculate_bollinger_bands(closes)
    atr = TechnicalIndicators.calculate_atr(klines)

    # MA
    ma5 = TechnicalIndicators.calculate_ma(closes, 5)
    ma20 = TechnicalIndicators.calculate_ma(closes, 20)

    # 最新价格
    current_price = closes[-1]

    # ATR百分比
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
        'klines': klines[-kline_count:],
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
        'closes': closes,
        'highs': highs,
        'lows': lows,
        'volumes': volumes
    }



def print_market_data(data: Dict):
    """打印市场数据"""
    print(f"\n{'='*60}")
    print(f"📊 {data['symbol']} 市场数据")
    print(f"{'='*60}")
    print(f"当前价格: ${data['current_price']:.4f}")
    print(f"24h涨幅: +{data['change_24h']:.2f}%")
    print()
    print(f"【技术指标】")
    print(f"  RSI(14):     {data['rsi']}")
    print(f"  MACD:        {data['macd']}")
    print(f"  Signal:      {data['macd']['signal']}")
    print(f"  布林带:      上 ${data['bollinger']['upper']:.2f} / 中 ${data['bollinger']['middle']:.2f} / 下 ${data['bollinger']['lower']:.2f}")
    print(f"  位置:        {data['bollinger']['position']:.1f}%")
    print(f"  ATR:         {data['atr']} ({data['atr_percent']}%)")
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

    # K线形态
    print(f"【K线形态】(最近5根)")
    klines = data['klines']
    for i, k in enumerate(klines):
        ts = datetime.fromtimestamp(k[0]/1000).strftime('%H:%M')
        o = float(k[1]); h = float(k[2]); l = float(k[3]); c = float(k[4])
        v = float(k[5])

        # 判断涨跌
        change = c - o
        if change > 0:
            color = '🟢'
        elif change < 0:
            color = '🔴'
        else:
            color = '⚪'

        # 上下影线
        upper_shadow = h - max(o, c)
        lower_shadow = min(o, c) - l
        body = abs(change)

        # 形态判断
        pattern = ""
        if upper_shadow > body * 0.5:
            pattern += "上影长 "
        if lower_shadow > body * 0.5:
            pattern += "下影长 "
        if upper_shadow > body and lower_shadow > body:
            pattern += "十字星 "
        if i > 0:
            prev_c = float(klines[i-1][4])
            if c < prev_c and c < o:
                pattern += "下跌 "

        print(f"  {i+1}. {ts} {color} 开:{o:.2f} 高:{h:.2f} 低:{l:.2f} 收:{c:.2f} 量:{v:.0f} {pattern}")


def get_status(symbol: str = None) -> Dict:
    """获取账户状态"""
    trader = BinanceTrader()
    
    # 检查止损触发（模拟模式）
    check_and_trigger_stops()
    
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
            print(f"  未实现盈亏: ${pos['unrealizedProfit']:.2f}")
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
    step_size = 1
    try:
        r = requests.get(f"{FAPI_URL}/fapi/v1/exchangeInfo", timeout=10)
        for s in r.json().get('symbols', []):
            if s['symbol'] == symbol:
                for f in s.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        break
                break
    except:
        pass

    # 计算开仓数量（round 到 stepSize 的整数倍）
    quantity = (margin * leverage) / price
    quantity = round(round(quantity / step_size) * step_size, 8)  # 保留精度
    if quantity <= 0:
        quantity = step_size

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

    return result


def do_close_short(symbol: str, percent: float = 100) -> Dict:
    """平空仓 (支持部分平仓)"""
    trader = BinanceTrader()

    print(f"\n{'='*60}")
    print(f"🔚 平空仓: {symbol} ({percent}%)")
    print(f"{'='*60}")
    
    # 支持部分平仓
    if percent < 100:
        # 获取当前持仓量，计算要平的数量
        positions = trader.get_positions(symbol)
        for pos in positions:
            if pos.get('positionSide') == 'SHORT' or (pos['amount'] < 0):
                total_qty = abs(pos['amount'])
                close_qty = total_qty * (percent / 100)
                print(f"部分平仓: {percent}% = {close_qty:.4f} / {total_qty:.4f} (全仓)")
                result = trader.close_short(symbol, close_qty)
                print(f"订单结果: {json.dumps(result, indent=2)}")
                return result
        raise Exception(f"No short position found for {symbol}")
    else:
        # 全平仓
        result = trader.close_short(symbol)
        print(f"订单结果: {json.dumps(result, indent=2)}")
        return result


def do_open_long(symbol: str, margin: float, leverage: int) -> Dict:
    """开多仓"""
    trader = BinanceTrader()
    price = trader.get_price(symbol)

    step_size = 1
    try:
        r = requests.get(f"{FAPI_URL}/fapi/v1/exchangeInfo", timeout=10)
        for s in r.json().get('symbols', []):
            if s['symbol'] == symbol:
                for f in s.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        break
                break
    except:
        pass

    quantity = (margin * leverage) / price
    quantity = round(round(quantity / step_size) * step_size, 8)
    if quantity <= 0:
        quantity = step_size

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

    return result


def do_close_long(symbol: str, percent: float = 100) -> Dict:
    """平多仓 (支持部分平仓)"""
    trader = BinanceTrader()

    print(f"\n{'='*60}")
    print(f"🔚 平多仓: {symbol} ({percent}%)")
    print(f"{'='*60}")
    
    # 支持部分平仓
    if percent < 100:
        # 获取当前持仓量，计算要平的数量
        positions = trader.get_positions(symbol)
        for pos in positions:
            if pos.get('positionSide') == 'LONG' or (pos['amount'] > 0):
                total_qty = abs(pos['amount'])
                close_qty = total_qty * (percent / 100)
                print(f"部分平仓: {percent}% = {close_qty:.4f} / {total_qty:.4f} (全仓)")
                result = trader.close_long(symbol, close_qty)
                print(f"订单结果: {json.dumps(result, indent=2)}")
                return result
        raise Exception(f"No long position found for {symbol}")
    else:
        # 全平仓
        result = trader.close_long(symbol)
        print(f"订单结果: {json.dumps(result, indent=2)}")
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
    scan_long_parser.add_argument('--min-change', type=float, default=-10, help='最大跌幅%（负值）')
    scan_long_parser.add_argument('--max-change', type=float, default=-3, help='最小跌幅%（负值）')

    # scan-all（统一波动率扫描，多空一起）
    scan_all_parser = subparsers.add_parser('scan-all', help='统一波动率扫描（多空一起）')
    scan_all_parser.add_argument('--top', type=int, default=20, help='取前N个高波动币种')
    scan_all_parser.add_argument('--min-vol', type=float, default=3.0, help='最低1h波动率%')
    scan_all_parser.add_argument('--klines', type=int, default=30, help='给多少个候选拿K线')

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
    close_parser.add_argument('--percent', type=float, default=100, help='平仓比例(0-100),默认100%全平')

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

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    trader = BinanceTrader()

    if args.command == 'status':
        get_status(args.symbol)

    elif args.command == 'scan':
        check_and_trigger_stops()  # 先检查止损
        scan_candidates(args.min_change)

    elif args.command == 'scan-short':
        check_and_trigger_stops()
        scan_short_candidates(args.min_change)

    elif args.command == 'scan-long':
        check_and_trigger_stops()
        scan_long_candidates(args.min_change, args.max_change)

    elif args.command == 'scan-all':
        check_and_trigger_stops()  # 先检查止损
        scan_volatility_top(args.top, args.min_vol, args.klines)

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


if __name__ == '__main__':
    main()