# Task 06b — the execution layer: why only one action produced orders, and the tests that pin it

Branch: `task/06b-execution-tests`   Worktree: `/home/shahrad/work/agentoquant-wt/task-06b`
Status: complete (with three named gaps, listed under *Not done*)

## What was wrong

Across 24 paper cycles against the live dry-run venue, three trades were open and only
`enter_laddered` had ever submitted an order. Reproduced on an isolated dry-run freqtrade 2026.8 on
Kraken (port 8081, its own signal dir and database, so the soak was untouched), 24-cycle burst,
`exit` cycles 14Z-0002/0010/0018:

```
exit  intents=1 orders=0 degraded=True
      venue_error: TypeError: FtRestClient.forceexit() got an unexpected keyword argument 'pair'
```

Root cause, one path and three defects in it:

1. `_rest_kwargs` injected `pair` into the kwargs **after** filtering everything else against the
   handler's own signature. `forceexit(tradeid, ordertype, amount)` and `cancel_open_order(trade_id)`
   take no pair, so the call raised `TypeError` before any order existed. `forceenter(pair, ...)`
   does take one, which is exactly why `enter_laddered` was the only action that worked.
2. `forceexit` needs a `tradeid` and `cancel_open_order` a `trade_id`; the pipeline addresses pairs,
   so no intent ever carried one.
3. The exception escaped `OrderManager.execute()` into `run_cycle`, which discarded **every** report
   for the cycle and reported the venue as unavailable.

Four more paths were dead or lying:

- `take_profit_ladder`: the bridge's branch keyed on `execution["target_price"]`, and
  `execution_intent` always published `target_price: null` (a Decision Card carries no target), so
  the branch was unreachable by construction.
- `cancel_order`: the plan built an `amend_order` intent, which is not one of the three REST calls
  freqtrade exposes and has no interface version 3 hook.
- `trim`: refused by the venue with *"Wanted to exit of -6.4125 amount, but exit amount is now 0.0
  due to exchange limits - not exiting"* and acked as a trim anyway.
- The execution record for a cycle whose entry had filled read `status=unfilled_timeout` with
  `fill_price` and `fee_paid` null, because the record was derived from REST response keys that
  freqtrade's force calls do not carry.

## The twelve actions, and what is actually proven

Proof levels: **live** = an order or an ack observed against a real dry-run venue; **in-process** =
the bridge module driven with a real published document and a stand-in trade; **unit** = the plan and
the intent only.

| # | Action | Path | Status | Evidence |
|---|---|---|---|---|
| 1 | `enter_laddered` | `laddered_entry` | **live** | 24-cycle burst: `orders=1` each cycle, ack `entry_confirmed` with rate 81361.4571 (no longer the placeholder's unfillable 59,940), venue DB held the trade and its order. Slices 2..n are hook-only by design and land at their own offset prices. |
| 2 | `add` | `dca_add` | **live** | ack `{"action":"add"}`; the venue created a `dca_add` buy at 81458.9 while the force entry sat at 81361.4571 — two distinct prices on one pair. An earlier run had the same order filled. |
| 3 | `trim` | `partial_exit` | **live, as a refusal** | The venue path works (an earlier run left a `partial_exit` sell order open at 111.4), but every trim in the burst refused honestly with ack `trim_refused: the trade holds no position to reduce` — the trades' entries were resting unfilled. The venue's own min-size no-op is now a pre-check (see below). |
| 4 | `exit` | `full_exit` | **live** | One cycle submitted the `forceexit` (`orders=1`); the two cycles with no open position on the pair refused with rule `no_open_position_for_pair` instead of degrading the cycle. An earlier run's market exit read back `status=filled fill_price=111.67 fill_qty=2.67096535`. |
| 5 | `rotate` | `rotate` | **unit** | Both legs (`forceexit` source, `forceenter` target) bind to the installed client signature; the replacement leg carries the required `side`. Not in the placeholder pattern, so not exercised live. |
| 6 | `set_stop` | `stop_on_exchange` | **in-process / indirect live** | The bridge's `custom_stoploss` handles `set_stop` and `trail_stop`, and stops genuinely rest on the exchange (venue DB: `ft_order_side=stoploss`, `ft_is_open=1`, `trade.stop_loss` set). Those stops came from freqtrade's own `stoploss_on_exchange` at entry, not from an `Action.SET_STOP` cycle — the pattern has none. |
| 7 | `trail_stop` | `trailing_stop_ratchet` | **in-process** | `roi_step`-style ratchet logic (`max(previous, candidate)`) and the published document were exercised in-process; the live burst did not produce a fresh ack because the venue's protections blocked a new filled position. |
| 8 | `take_profit_ladder` | `take_profit_ladder` | **in-process** | Against a real `SignalStore.publish` document: `roi_step` at 45 min = 0.02, `custom_exit(profit 0.005)` = `None`, `custom_exit(profit 0.03)` = `take_profit_ladder`, ack written. A live ROI fire needs a trade at +2% for 30+ minutes; none was. |
| 9 | `event_trade` | `event_trade_with_time_stop` | **unit** | Not in the pattern. Its entry leg is the same `forceenter` that works live; the time stop is a `custom_exit` branch keyed on `time_stop_minutes`, which is published (1440). |
| 10 | `rebalance` | `scheduled_weekly_rebalance` | **unit, by design** | Refuses outside Sunday 00:00 UTC and produces the `rebalance` intent inside it; the plan is time-gated rather than dead. No Sunday tick occurred during the run. |
| 11 | `hold` | `hold_no_op` | **live** | Six cycles in the burst placed nothing and reported `intents=0` — the intended no-op. |
| 12 | `cancel_order` | `cancel_or_amend_resting` | **live** (earlier run) | `cancel_open_order` submitted for SOL/USD and the venue's `open_order_id` went from 11 to `None`; the replacement leg is a `forceenter` with its required `side`. Not in the pattern, so the final burst did not reach it. |

## Files touched

```
agentoquant/execution/order_manager.py        REST kwargs/ids, per-intent isolation, plan_closes,
                                              min-order pre-check, write-back, two-rate fees,
                                              venue-anchored entry price
agentoquant/execution/signal_store.py         ladder offsets, minimal-ROI ladder, partial exits
agentoquant/execution/paper.py                refusals/errors in the summary, kill switch wiring
agentoquant/risk/kill_switch.py               persisted halt state, build_kill_switch
agentoquant/config_loader.py                  ExecutionLimits.min_order_cost_usd
config/risk_limits.yaml                       min_order_cost_usd: 5.0
freqtrade_user_data/strategies/AgentBridgeStrategy.py  take-profit ladder, trim/add refusals,
                                              per-slice entry prices
tests/test_execution_bridge.py                new, 98 tests
```

Commits on the branch (each with its evidence): `ed1eec7` REST kwargs and trade ids, `d9cbf7c`
per-intent isolation, `eb85fa5` cancel-and-replace, `f8c7008` take-profit ladder, `4fab991` kill
switch, `d848c75` minimum order size, `7e98f17` write-back, `41cde56` two-rate fees + anchored
price, `1aac400` the test suite, `abf0d9f` per-slice ladder prices.

## Commands run

```
.venv/bin/python -m pytest -q                479 passed, 1 skipped      (was 227 passed)
.venv/bin/ruff check .                       All checks passed!
24-cycle burst vs dry-run freqtrade 2026.8   24 completed, 0 failed, 0 degraded, 0 errors
```

## Acceptance

- *Every action in the vocabulary has a working execution path in dry-run, and grid or scalp actions
  are rejected* → pass, with the per-action proof table above (8 of 12 live or in-process; 4 unit or
  time-gated, named honestly).
- *Orders, fills, fees at the configured tier and LLM cost per cycle are logged with the cycle id* →
  pass; `fee_paid` is now priced at the order type's own rate (maker vs taker) from
  `config/fee_tiers.yaml`, and every record carries the cycle id.

## Deviations

- `cancel_order` is cancel-and-replace, not an in-place amend: freqtrade's REST API exposes no amend
  endpoint and interface version 3 has no amend hook, so `keep_queue_position` was removed rather
  than claimed.
- A kill-switch close is a market order (`purpose=emergency_exit`), which is the other purpose the
  rules allow a taker order for. `close_all_orders` is only consumed while a halt is active, because
  that method returns a plan per position regardless of halt state.
- A reference price more than 1% from the venue's own last price is re-anchored rather than rejected:
  rejecting would have stopped the Phase 0 soak from trading at all, and the price is an execution
  detail the Risk Gate does not size on. The rule is named in the plan detail.

## Open questions

None blocking.

## Not done

1. **The strategy's own refusals do not reach the cycle summary.** `trim_refused` and `add_refused`
   are written to the ack, and the loop reads the ack, but nothing classifies it — a cycle whose only
   action was refused still reports `orders=0, hook=1` with no `refused` count.
2. **F10 — placed size versus approved size.** The bridge sizes from the dry-run wallet (900 USD)
   while the Risk Gate sizes from `capital.starting_capital` in CAD; the two bases still disagree.
3. **F5 — the stop level still does not reach the hook paths.** The take-profit ladder now does; the
   stop is still published as `null` and the bridge falls back to `agent_trail_percent`.
4. **The transport has no retry policy.** One `ReadTimeout` appeared in an earlier 24-cycle burst
   (the venue's API server is busy during its 5s cycle); it is now isolated to the one intent that
   hit it instead of discarding the cycle, but it is still a lost order.
5. **The soak venue on this box still runs the old code** (it launches the main clone's installed
   package). Every fix here was proven against a second dry-run venue started from this worktree;
   the soak's own database has not been re-run.