"""Budget-matched, unlevered research simulator (not an exchange execution model).

Orders use only prior-bar state. Each coin can buy one leg per bar; a bar with
any buy cannot take profit. Gap buys fill at their limit (no favorable improvement),
while gap take profits also fill at their limit. This deliberately penalizes ambiguous
intrabar sequences. Default drawdown uses close marks; optional low_proxy uses
simultaneous bar lows and liquidates there, a conservative stress bound rather than
an executable portfolio price path. No funding, liquidation borrowing, queue,
partial-fill or exchange size constraints are modeled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import pickle
import numpy as np

try:
    from numba import njit
except ImportError:
    def njit(*args, **kwargs):
        return lambda f: f


@dataclass
class MarketData:
    times: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    coins: tuple[str, ...]
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.times = np.ascontiguousarray(self.times, dtype=np.int64)
        self.coins = tuple(self.coins)
        shape = (len(self.times), len(self.coins))
        if not self.coins or len(set(self.coins)) != len(self.coins):
            raise ValueError('coins must be nonempty and unique')
        if np.any(np.diff(self.times) != 60000):
            raise ValueError('MarketData requires consecutive one-minute timestamps')
        for name in ('open', 'high', 'low', 'close'):
            values = np.ascontiguousarray(getattr(self, name), dtype=np.float64)
            if values.shape != shape or not np.all(np.isfinite(values)) or np.any(values <= 0):
                raise ValueError(f'invalid {name} array')
            setattr(self, name, values)
        if (np.any(self.high < np.maximum(self.open, self.close)) or
            np.any(self.low > np.minimum(self.open, self.close))):
            raise ValueError('inconsistent OHLC ranges')


@dataclass(frozen=True)
class SearchConfig:
    mode: str = 'qty'
    multiplier: float = 2.0
    drops_pct: tuple[float, ...] = (-5., -10., -15., -25.)
    tp_pct: float = .361
    coin_budget_fraction: float = .1
    initial_equity: float = 10000.
    maker_fee: float = .0002
    taker_fee: float = .0004
    hard_stop_pct: float = 15.
    low_proxy: bool = False
    fixed_multipliers: tuple[float, ...] = (2.78, 10.46, 38.68, 80.)
    last_multiplier: float | None = None
    risk_breakers: bool = False


def normalized_weights(config: SearchConfig) -> np.ndarray:
    """Entry notionals normalized to equal total budget at planned limit prices."""
    d = np.array(config.drops_pct, dtype=float)
    if len(d) == 0 or not np.all(np.isfinite(d)) or np.any(d >= 0) or np.any(d <= -100) or np.any(np.diff(d) >= 0):
        raise ValueError('drops_pct must be strictly descending percentages in (-100,0)')
    if config.mode == 'fixed':
        if len(config.fixed_multipliers) != len(d):
            raise ValueError('fixed multipliers must match add levels')
        w = np.array((1., *config.fixed_multipliers))
    elif config.mode in ('qty', 'cost'):
        cumulative = 1.
        w = [1.]
        for k, drop in enumerate(d):
            m = config.last_multiplier if k == len(d)-1 and config.last_multiplier is not None else config.multiplier
            if not np.isfinite(m) or m <= 0:
                raise ValueError('multipliers must be positive')
            leg = m * cumulative
            w.append(leg * (1. + drop/100.) if config.mode == 'qty' else leg)
            cumulative += leg
        w = np.array(w)
    else:
        raise ValueError('mode must be fixed, qty or cost')
    if np.any(w <= 0) or not np.all(np.isfinite(w)):
        raise ValueError('notional weights must be finite and positive')
    return w / w.sum()


def load_cached_data(coins=None, cache_dir='/tmp/martiquant_klines') -> MarketData:
    """Read trusted local Bar pickle caches only; intersect exact minute timestamps.

    Pick longest available cache per coin. Reject duplicate/gapped/malformed bars;
    report common data span instead of silently filling missing market observations.
    """
    from .config import DEFAULT_BASKET
    coins = tuple(coins or DEFAULT_BASKET.include)
    records = []
    sources = {}
    for coin in coins:
        paths = list(Path(cache_dir).glob(f'{coin}_1m_*.pkl'))
        if not paths:
            raise FileNotFoundError(f'No cached 1m data for {coin}')
        best = None
        best_path = None
        for path in paths:
            with path.open('rb') as f:
                bars = pickle.load(f)
            if best is None or len(bars) > len(best):
                best, best_path = bars, path
        t = np.array([b.open_time_ms for b in best], dtype=np.int64)
        p = np.array([[b.open,b.high,b.low,b.close] for b in best], dtype=float)
        if len(t)<2 or np.any(np.diff(t)!=60000):
            raise ValueError(f'{coin}: timestamps not contiguous unique minutes')
        if (not np.all(np.isfinite(p)) or np.any(p<=0) or
            np.any(p[:,1]<np.maximum(p[:,0],p[:,3])) or
            np.any(p[:,2]>np.minimum(p[:,0],p[:,3]))):
            raise ValueError(f'{coin}: invalid OHLC')
        records.append((t,p))
        sources[coin] = str(best_path)
    start = max(t[0] for t,p in records)
    end = min(t[-1] for t,p in records)
    if end <= start:
        raise ValueError('Caches have no common time interval')
    times = np.arange(start,end+1,60000,dtype=np.int64)
    aligned = []
    for t,p in records:
        idx = np.searchsorted(t,times)
        if not np.array_equal(t[idx],times):
            raise ValueError('Missing aligned timestamp')
        aligned.append(p[idx])
    a = np.stack(aligned,axis=1)
    return MarketData(times,*(np.ascontiguousarray(a[:,:,i]) for i in range(4)),coins,
                      {'sources':sources,'bars':len(times),'funding_rate':0.,
                       'start_ms':int(start),'end_ms':int(end)})


@njit(cache=True)
def _simulate(high,low,close,weights,drops,start,end,initial,budget,maker,taker,tp,stop,low_proxy,breakers):
    n = close.shape[1]
    qty=np.zeros(n)
    cost=np.zeros(n)
    anchor=np.zeros(n)
    level=np.zeros(n,np.int64)
    opened=np.zeros(n,np.int64)
    cash=initial
    peak=initial
    maxdd=0.
    peakcost=0.
    rounds=0
    buys=0
    fees=0.
    maxhold=0
    stopped=False
    last=start
    for t in range(start+1,end):
        last=t
        # Optional stress: pre-trade synchronous lows, deliberately conservative.
        if low_proxy:
            stress=cash
            for c in range(n):
                stress += qty[c]*low[t,c]*(1.-taker)
            dd=max(0.,(peak-stress)/peak*100.)
            maxdd=max(maxdd,dd)
            if dd>=stop:
                for c in range(n):
                    if qty[c]>0:
                        gross=qty[c]*low[t,c]
                        fees+=gross*taker
                        cash+=gross*(1.-taker)
                        maxhold=max(maxhold,t-opened[c])
                        qty[c]=0.
                        cost[c]=0.
                stopped=True
                break
        pause=False
        if breakers:
            deep=0
            for c in range(n):
                if level[c]>=3:
                    pause=True
                if level[c]>=2:
                    deep+=1
            if deep>=3:
                pause=True
        for c in range(n):
            bought=False
            if qty[c]==0.:
                px=close[t-1,c]
                amount=budget*weights[0]
                if not pause and low[t,c]<=px and cash+initial*1e-12>=amount*(1.+maker):
                    qty[c]=amount/px
                    cost[c]=amount
                    cash=max(0.,cash-amount*(1.+maker))
                    fees+=amount*maker
                    anchor[c]=px
                    level[c]=0
                    opened[c]=t
                    buys+=1
                    bought=True
            elif level[c]<len(drops):
                k=level[c]
                px=anchor[c]*(1.+drops[k]/100.)
                amount=budget*weights[k+1]
                if low[t,c]<=px and cash+initial*1e-12>=amount*(1.+maker):
                    qty[c]+=amount/px
                    cost[c]+=amount
                    cash=max(0.,cash-amount*(1.+maker))
                    fees+=amount*maker
                    level[c]+=1
                    buys+=1
                    bought=True
            if bought:
                committed_now=0.
                for j in range(n):
                    committed_now+=cost[j]
                peakcost=max(peakcost,committed_now)
            if qty[c]>0.:
                maxhold=max(maxhold,t-opened[c])
                target=cost[c]/qty[c]*(1.+tp/100.)
                if not bought and high[t,c]>=target:
                    gross=qty[c]*target
                    cash+=gross*(1.-maker)
                    fees+=gross*maker
                    qty[c]=0.
                    cost[c]=0.
                    level[c]=0
                    rounds+=1
        committed=0.
        equity=cash
        for c in range(n):
            committed+=cost[c]
            equity+=qty[c]*close[t,c]*(1.-taker)
        peakcost=max(peakcost,committed)
        peak=max(peak,equity)
        dd=max(0.,(peak-equity)/peak*100.)
        maxdd=max(maxdd,dd)
        if dd>=stop:
            for c in range(n):
                if qty[c]>0.:
                    gross=qty[c]*close[t,c]
                    cash+=gross*(1.-taker)
                    fees+=gross*taker
                    qty[c]=0.
                    cost[c]=0.
            stopped=True
            break
    equity=cash
    opencost=0.
    openvalue=0.
    totalqty=0.
    for c in range(n):
        value=qty[c]*close[last,c]*(1.-taker)
        equity+=value
        opencost+=cost[c]
        openvalue+=value
        totalqty+=qty[c]
    return (equity,maxdd,peakcost,openvalue-opencost,rounds,maxhold,fees,cash,
            opencost,totalqty,stopped,buys,last)


def run_backtest(data: MarketData, config: SearchConfig | dict, start=0,end=None) -> dict:
    """Run one flat-start chronological slice; ending equity includes exit taker fee.

    Budget is fixed at initial equity times coin_budget_fraction. All planned legs
    including maker fees fit this budget. No equity rescaling or borrowed balance.
    Reported open_pnl includes exit taker fees, but entry fees are in total fees/cash.
    """
    if isinstance(config,dict):
        config=SearchConfig(**config)
    end=len(data.times) if end is None else int(end)
    start=int(start)
    if not 0<=start<end-1<=len(data.times)-1:
        raise ValueError('slice needs at least two bars inside data')
    finite_values=(config.initial_equity,config.coin_budget_fraction,config.maker_fee,
                   config.taker_fee,config.tp_pct,config.hard_stop_pct)
    if (not np.all(np.isfinite(finite_values)) or
        config.initial_equity<=0 or not 0<config.coin_budget_fraction<=1 or
        config.coin_budget_fraction*len(data.coins)>1.+1e-12 or
        config.maker_fee<0 or config.taker_fee<0 or config.tp_pct<=0 or
        config.hard_stop_pct<=0):
        raise ValueError('invalid budget, fees, take profit or stop')
    w=normalized_weights(config)
    values=_simulate(data.high,data.low,data.close,w,np.array(config.drops_pct),start,end,
                     config.initial_equity,config.initial_equity*config.coin_budget_fraction/(1.+config.maker_fee),
                     config.maker_fee,config.taker_fee,config.tp_pct,config.hard_stop_pct,
                     config.low_proxy,config.risk_breakers)
    keys=('ending_equity','max_drawdown_pct','peak_committed_usd','open_pnl','rounds',
          'max_holding_minutes','fees','ending_cash','open_cost','open_qty_total',
          'hard_stopped','buy_count','last_bar_index')
    result=dict(zip(keys,values))
    for key in ('rounds','max_holding_minutes','buy_count','last_bar_index'):
        result[key]=int(result[key])
    result['hard_stopped']=bool(result['hard_stopped'])
    result['net_profit']=float(result['ending_equity']-config.initial_equity)
    result['return_pct']=result['net_profit']/config.initial_equity*100.
    result['peak_committed_pct']=result['peak_committed_usd']/config.initial_equity*100.
    result['net_pct']=result['return_pct']
    result['max_dd_pct']=result['max_drawdown_pct']
    result['stopped']=result['hard_stopped']
    result['max_committed_pct']=result['peak_committed_pct']
    result['max_hold_hours']=result['max_holding_minutes']/60.
    result['funding']=0.
    result['start_ms']=int(data.times[start])
    result['end_ms']=int(data.times[end-1])
    return result
