"""Command line: python -m fomobot {run,scan,status,report}"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from .config import load_config
from .storage import Store


def build_engine(cfg, store, live: bool, trade: bool):
    from .engine import Engine
    from .intelligence.analyst import Analyst

    if live:
        from .execution.jupiter import JupiterExecutor

        executor = JupiterExecutor(cfg)
    else:
        from .execution.paper import PaperExecutor

        executor = PaperExecutor(cfg.execution, store)

    analyst = None
    if cfg.llm.enabled:
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            analyst = Analyst(cfg.llm)
        else:
            logging.warning("LLM enabled but no ANTHROPIC_API_KEY set - running on rules only")
    return Engine(cfg, store, executor, analyst=analyst, trade=trade)


def cmd_status(store: Store) -> None:
    cash = store.get_state("paper_cash")
    if cash is not None:
        print(f"Paper cash: {cash:.4f} SOL")
    pos = store.open_positions()
    print(f"Open positions: {len(pos)}")
    for p in pos:
        mins = (time.time() - p.opened_at) / 60
        print(f"  {p.symbol:<12} cost {p.cost_sol:.4f} SOL  entry {p.entry_price:.10f}  "
              f"SL -{p.stop_loss_pct:.0f}% TP +{p.take_profit_pct:.0f}%  {mins:.0f} min  {'TP-hit ' if p.tp_hit else ''}")


def cmd_report(store: Store) -> None:
    closed = store.closed_positions()
    if not closed:
        print("No closed trades yet.")
        return
    pnls = [r["pnl_sol"] or 0 for r in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    print(f"Closed trades: {len(pnls)}  win rate {len(wins) / len(pnls):.0%}")
    print(f"Total PnL: {sum(pnls):+.4f} SOL")
    if wins:
        print(f"Avg win:  {sum(wins) / len(wins):+.4f} SOL")
    if losses:
        print(f"Avg loss: {sum(losses) / len(losses):+.4f} SOL")
    if losses and sum(losses) != 0:
        print(f"Profit factor: {sum(wins) / -sum(losses):.2f}")
    print("\nLast 10:")
    for r in closed[:10]:
        print(f"  {r['symbol']:<12} {r['pnl_sol']:+.4f} SOL  {r['exit_reason']}")
    lessons = store.lessons(10)
    if lessons:
        print("\nLessons learned:")
        for l in lessons:
            print(f"  - {l}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="fomobot", description="Solana memecoin trading bot (FOMO wallet compatible)")
    ap.add_argument("command", choices=["run", "scan", "status", "report", "collect", "backtest"])
    ap.add_argument("--data", default="data/history", help="dataset dir for collect/backtest")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--live", action="store_true", help="trade real funds (also requires execution.mode: live)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpx2", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.command == "collect":
        from .backtest.dataset import collect

        collect(args.data)
        return
    if args.command == "backtest":
        from .backtest.report import backtest

        backtest(args.data, out_file=f"{args.data}/backtest_result.json")
        return

    cfg = load_config(args.config)
    live = args.live and cfg.execution.mode == "live"
    if args.live and not live:
        sys.exit("--live given but config execution.mode is not 'live'. Refusing to trade real funds.")
    db = cfg.db_path if not live else cfg.db_path.replace(".db", "") + "-live.db"
    store = Store(db)

    if args.command == "status":
        return cmd_status(store)
    if args.command == "report":
        return cmd_report(store)

    engine = build_engine(cfg, store, live=live, trade=args.command == "run")
    if args.command == "scan":
        engine.tick()
        return
    try:
        engine.run()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
