# Live Paper Trading — 设计文档

**日期**: 2026-10-22
**作者**: MartiQuant 项目
**状态**: Draft → 评审通过后进入实现阶段

---

## 1. 目标

把现有的"历史回测引擎"扩展成一个**长时间持续运行的内存纸盘**，用真实 Binance 行情驱动 `strategy.LadderStrategy` + `paper_broker.PaperBroker`，观察马丁格尔策略在实时市场数据下的真实行为。

**要解决的问题**：

- 历史回测证明了参数口径一致、月率量级在 10-17%/年。但回测里有两条乐观假设无法在历史数据上验证：
  1. "K 线触碰即成交"——真实挂单会不会被频繁打穿？
  2. "每小时整点撤单重挂"——撤挂时序在实时市场里是否稳健？
- 10 个币并发、每小时数十次撤挂重挂，这种吞吐在实时下才能观察到稳态。
- 要验证"策略在遇到快速下跌时的行为"（例如 BCH 在 2026-05/06 走到 L5 的那种场景），只有实时跑过一段时间（含周末、含资金费结算、含至少一次中等波动）才能有信心。

## 2. 非目标

**本阶段不做**：

- ❌ 不接 Binance Futures Testnet，不发真实下单请求
- ❌ 不处理 user data stream（没有真实订单也就没有真实 fill 推送需要处理）
- ❌ 不实现 live executor / order manager / 签名 / rate limit
- ❌ 不建模滑点、队列、部分成交、拒单
- ❌ 不建模资金费（paper_broker 本来就没有，保持一致）
- ❌ 不支持多实例并发运行

**原因**：核心风险是**策略在实时下的行为**，不是下单路径。下单路径在 OKX 源账户已经跑了 11 个月，不是新东西；而策略在实时下的行为是没验过的。先用内存纸盘消掉策略层的不确定性，再决定是否值得搞 Testnet。

## 3. 架构

```
┌──────────────────────────────────────────────────────────────┐
│ Binance Futures 公开 WebSocket                               │
│   wss://fstream.binance.com/market/ws/{coin}usdt@kline_1m    │
│   10 个 coin 并发订阅, 无需 API key, 无需 Testnet USDT       │
└──────────────────────────┬───────────────────────────────────┘
                           │ 1m kline 推送 (bar closed only)
                           ▼
┌──────────────────────────────────────────────────────────────┐
│ martiquant/live/feed.py — WSFeed                             │
│   • 10 个 WS 连接并发                                        │
│   • 自动重连 (指数退避 1s → 60s)                             │
│   • 消息计数 + 健康检查                                      │
│   • 回调签名: on_bar(coin, Bar)                              │
└──────────────────────────┬───────────────────────────────────┘
                           │ Bar(coin, open_time_ms, o/h/l/c)
                           ▼
┌──────────────────────────────────────────────────────────────┐
│ martiquant/live/runner.py — LiveRunner.on_bar()              │
│                                                              │
│   1. broker.match_bar(coin, high, low, ts)  → List[Fill]     │
│   2. for fill in fills:                                      │
│        pnl = strategy.on_fill(...)                           │
│        logger.event("fill", ...)                             │
│        if round closed: logger.round(...)                    │
│   3. mark[coin] = close                                      │
│   4. if hour changed or any fill:                            │
│        desired = strategy.desired_orders(mark)               │
│        broker.reconcile(desired)                             │
│   5. 每 60s: state.dump()  + equity 快照                     │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────┬───────────────────────────────────┐
│ runs/live_<ts>/          │                                   │
│   config.json            │ 运行参数                          │
│   state.json             │ 最新状态 (重启能恢复)             │
│   rounds.csv             │ 已关闭轮次 (格式同 paper_backtest)│
│   events.jsonl           │ 下单/成交/撤单/断线/告警          │
│   equity_hourly.csv      │ 每小时权益快照                    │
└──────────────────────────┴───────────────────────────────────┘
```

## 4. 模块职责

| 模块 | 职责 | 对外接口 | 行数 |
|---|---|---|---|
| `live/feed.py` | WS 订阅 + 重连 + 健康检查 | `WSFeed.start(on_bar)`, `WSFeed.stop()` | ~150 |
| `live/runner.py` | asyncio 编排、组合所有组件 | `LiveRunner.run()` | ~200 |
| `live/state.py` | 持久化 strategy/broker/pnl 到 JSON | `dump(path)`, `load(path)` | ~80 |
| `live/logger.py` | 结构化日志，三种输出 | `event(kind, **kw)`, `round(dict)`, `equity_snapshot(dict)` | ~100 |
| `live/__main__.py` | CLI 入口 + argparse + 信号处理 | `python3 -m martiquant.live` | ~50 |

**依赖关系**：

```
__main__ → runner → feed
                 → logger
                 → state
                 → (复用) strategy / paper_broker / config
```

## 5. 复用边界

**完全复用，不做任何改动**：

- `martiquant/strategy.py:LadderStrategy` — 策略逻辑是纯函数，live 下接口完全一致
- `martiquant/paper_broker.py:PaperBroker` — `match_bar(coin, h, l, ts) → fills` 和 `reconcile(desired)` 接口 live 下直接可用
- `martiquant/config.py` — B_PERIOD / DEFAULT_BASKET / DEFAULT_RISK 直接用

**原则**：live/ 下的所有新代码只做**数据接入和编排**。策略逻辑和撮合逻辑一个字不改。这样保证"离线回测验证过的行为"=="实时纸盘的行为"，否则 live 验证就没有意义。

参考 quant-analytics 项目的 CLAUDE.md 规约："实盘任何信号/阈值逻辑必须与 `btc_sim/simulator.py` 保持一致"——我们对标 `paper_backtest.py` 的撮合逻辑。

## 6. 关键决策

### 6.1 行情粒度：1m kline close（不用 aggTrade）

- quant-analytics 用 aggTrade + 自己聚合 6s bar，因为 LGBM 模型需要 6s 粒度
- 我们的马丁策略只需要 1m high/low 判断限价单成不成交
- 直接订 Binance 公开 `{coin}usdt@kline_1m`，只处理 `k.x == true`（bar closed）事件
- 节省带宽和 CPU，撮合逻辑和 paper_backtest 完全一致

### 6.2 整点撤单重挂（继承源策略）

- OKX 源账户每小时整点撤单重挂，paper_backtest 已实现（`paper_backtest.py:190-194`）
- live 下保持同样逻辑：`hour = ts // 3600_000`，`hour` 变化就 `strategy.desired_orders(mark) → broker.reconcile(desired)`
- 任何 fill 事件也触发 reconcile（保持 broker 状态清洁）

### 6.3 撮合假设继承：bar high/low 触碰即成交

- paper_broker 的乐观假设：bar 的 low ≤ 挂买单价 ⇒ 挂买单成交
- live 下这个假设仍然乐观（真实下单会排队、可能失败）
- **但 live 下的价值不在"撮合更真实"，而在"策略层行为能观察到"**
- 想验撮合真实度要等 Testnet 阶段。live 内存纸盘就是放大镜看策略逻辑

### 6.4 状态持久化策略

- 每 60s 把 strategy.states + broker.open_orders + 累计 PnL dump 到 `state.json`
- 启动时如果 `state.json` 存在且 timestamp 距今 <1 小时，自动加载恢复
- 超过 1 小时视为冷启动（避免错误复活过期状态）
- rounds.csv 和 events.jsonl 用 append 模式，自然 crash-safe

### 6.5 时间源

- 所有时间戳用 Binance 推送里的 `k.T`（bar close time, ms）
- 不用本地 `time.time()` 判断整点（避免本地时钟漂移）

## 7. 数据流详细

### 7.1 Bar close 事件处理（LiveRunner.on_bar）

```python
def on_bar(coin: str, bar: Bar):
    fills = broker.match_bar(coin, bar.high, bar.low, bar.open_time_ms)

    for fill in fills:
        was_open = strategy.states[coin].in_position
        pnl = strategy.on_fill(coin, fill.side, fill.px, fill.qty, ts=str(fill.ts))
        logger.event("fill", coin=coin, side=fill.side, px=fill.px, qty=fill.qty,
                     fee=fill.fee, pnl=pnl, level=strategy.states[coin].level)

        if fill.side == "sell" and not strategy.states[coin].in_position:
            logger.round({...})  # 轮次收尾

    mark[coin] = bar.close
    hour_now = bar.open_time_ms // 3_600_000

    if fills or hour_now != hour_last:
        desired = strategy.desired_orders(mark)
        broker.reconcile(desired)
        logger.event("reconcile", n_desired=len(desired))
        hour_last = hour_now
```

### 7.2 周期任务（独立 asyncio task）

- **每 60s**: `state.dump()` 到 `state.json`，`logger.equity_snapshot(...)`
- **每 3600s（整点）**: 即使没有 bar 推送也触发一次 reconcile，防止 WS 全断导致漏撤挂（兜底）
- **每 300s**: 健康检查，如果所有 10 个 WS 流 >60s 无消息则 log ERROR

## 8. 日志与产物

### 8.1 产物目录

```
runs/live_<YYYYMMDDTHHMMSSZ>/
  config.json          # LiveConfig + strategy params
  state.json           # 最新持久化状态
  rounds.csv           # 已关闭轮次, 列同 paper_backtest.py
  events.jsonl         # 所有事件, 每行一个 JSON
  equity_hourly.csv    # ts, mtm_eq, realized_eq, upl, dd_pct, open_coins
  startup.log          # 启动摘要
```

### 8.2 events.jsonl schema

```json
{"ts_ms": 1761408000000, "kind": "fill", "coin": "SOL", "side": "buy", "px": 192.3, "qty": 0.08, "fee": -0.015, "pnl": 0, "level": 1}
{"ts_ms": 1761408060000, "kind": "reconcile", "n_desired": 32, "n_cancel": 3, "n_place": 2}
{"ts_ms": 1761408120000, "kind": "ws_disconnect", "coin": "SOL", "reason": "ConnectionClosed"}
{"ts_ms": 1761408180000, "kind": "deep_level", "coin": "BCH", "level": 3, "warning": "close to L4"}
```

### 8.3 rounds.csv 格式

严格对齐 `paper_backtest.py` 产物（coin / open_time_ms / close_time_ms / hold_hours / first_entry_px / close_px / max_level / price_pnl / fee / realized_pnl），这样可以直接复用现有的分析脚本比较"回测产物" vs "实时产物"。

## 9. 异常与风控

### 9.1 WS 断线

- 单流断线：指数退避重连（1s → 2 → 4 → 8 → 16 → 32 → 60s cap）
- 50 次连续回调异常触发强制重连
- 所有 10 流同时断线 >60s：log ERROR 但不停机（继续等重连）

### 9.2 启动校验

启动时必须通过：

1. `python3 -c "import martiquant.live"` 不报错
2. WS 握手成功至少 1 个 coin（否则 10 分钟后退出）
3. 如果加载 state.json：时间戳 <1 小时内 + schema 匹配

### 9.3 硬门槛警告（源于 #1 分析）

- **10 币 B 期满档总开仓名义 $20,548 > $20,000 (2x 杠杆 $10k 上限)**
- live 下不会真正发单，所以保证金限制看不到
- 但要在 `events.jsonl` 里主动告警："模拟满档 N 币，实盘会有 M 币最深档加仓被 margin 拒绝"
- 触发条件：任何 N≥8 币同时在 level ≥ 3

### 9.4 熔断器配置

- 遵循 `config.DEFAULT_RISK`（max_level_any_coin=4, max_coins_at_deep=3, deep_level=3）
- CLI 支持 `--no-circuit-breaker` 关闭熔断，和 paper_backtest 一致
- 关闭熔断时在 events.jsonl 记录警告

### 9.5 不实现（显式决策）

- 不实现"回撤红线强平"——paper_backtest.py 也没有，保持一致
- 不实现"异常止损触发"
- 这些是风控策略层的事，live 阶段先不加

## 10. 启动与停止

### 10.1 启动

```bash
cd /Users/zhangjiawei/repos/MartiQuant
python3 -m martiquant.live --equity-usd 10000 --tag paper1
python3 -m martiquant.live --equity-usd 10000 --no-circuit-breaker --tag paper-nocb
python3 -m martiquant.live --equity-usd 10000 --resume-from runs/live_20261022T100000Z/
```

### 10.2 停止

- Ctrl+C 捕获 SIGINT：先 `ws.stop()`，再 `state.dump()`，再退出
- SIGTERM 同上
- Crash（未捕获异常）：rounds.csv/events.jsonl append 模式 → 不丢数据；下次用 `--resume-from` 恢复 state.json

### 10.3 run.sh 包装（可选，后续加）

参考 quant-analytics 的 `live/run.sh {start,stop,status,restart}`，暂不实现。

## 11. 验证计划

### 11.1 必过门槛

1. **启动 5 分钟内**：所有 10 个 coin WS 流至少收到 1 根 bar
2. **第 1 小时**：至少触发 1 次整点 reconcile，events.jsonl 可见
3. **第 1 天**：rounds.csv 至少有 1 条已关闭轮次（任何币）
4. **第 1 周**：
   - 所有 10 个 coin 都至少开过 1 次仓
   - 累计 rounds > 100
   - WS 累计断线 <5 次（每天 <1 次）
   - state.json dump 成功 >95% 次（每 60s 一次应有 >9k 次）
5. **第 2 周**：
   - 跨周末验证（Binance 周末不停）
   - 至少跨过 2 次资金费结算（UTC 00/08/16）—— paper_broker 不处理资金费所以只是不吃不赔
   - 中位 bar-to-reconcile 延迟 <2 秒

### 11.2 对比 paper_backtest 回放

- 跑完后：拿 live 的 rounds.csv 和同窗口 Binance 行情 → paper_backtest → 另一份 rounds.csv
- 两份对比：轮数、净 PnL、per-coin max_level 分布
- **期望**：差距 <5%（live 和回测的区别只有"每小时整点时钟漂移"的几秒延迟）
- 如果 >5%：找原因（可能是 reconcile 时序 bug）

### 11.3 实时期间人工观察

- 每天看一眼 `events.jsonl` 的 deep_level 告警
- 每天看 equity_hourly.csv 的 MTM dd 曲线
- 如果出现 BCH 2026-05/06 那种单币 L5 场景：观察策略行为是否符合预期，不干预

## 12. 开放问题（需要用户决策）

1. **跑多久算验证通过**：2 周？4 周？到满足一次中等下跌？
2. **熔断 ON/OFF**：先跑哪个？建议**同时起两个实例**（一个 ON 一个 OFF）对照跑，不同 `--tag`
3. **本金口径**：`--equity-usd 10000` 保持和回测一致？还是用 OKX 当前权益 $207k？
4. **告警机制**：现在只写 events.jsonl，要不要加 webhook / 微信 / 邮件推送？

默认选项（如果没进一步输入）：

- 跑 2 周
- 同时跑熔断 ON 和 OFF 两个实例
- $10k 本金
- 不加推送，只写文件

## 13. 工程估算

| 阶段 | 产出 | 工时 |
|---|---|---|
| 代码 | 5 个新文件 ~580 行 | 1 天 |
| 本地冒烟 | 跑 1 小时验证 WS 连通 + 至少 1 次 fill | 2 小时 |
| 持续运行 | 2 周观察期 | 自动 |
| 结果分析 | live rounds.csv vs backtest 对账 + 报告 | 0.5 天 |

## 14. 后续阶段（不在本次范围）

本设计完成并验证通过后，才会进入下一阶段：

- **Testnet 阶段**：接 Binance Futures Testnet，加 live executor + user data stream
- **预估**：5-7 天代码 + 1-2 周 Testnet 观察
- 触发条件：live 内存纸盘 2 周跑下来策略行为符合预期、没有新发现的 bug

---

**附录 A: 相关 memory 引用**

- `[[project-martiquant-goal]]` — 项目总体阶段
- `[[project-okx-strategy-verified]]` — B 期参数来源与风险发现
- `[[feedback-no-oneshot-scripts]]` — live/ 不是一次性脚本，是长期功能
- `[[feedback-announce-output-paths]]` — 遵循约定

**附录 B: 参考资料**

- quant-analytics `live/ws_feed.py` — WS 重连 pattern
- quant-analytics `live/main.py` — asyncio 编排 pattern
- quant-analytics `live/health.py` — 健康检查 pattern
- Binance Futures WS 文档: https://binance-docs.github.io/apidocs/futures/en/#websocket-market-streams
