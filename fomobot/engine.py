"""The trading loop: discover -> enrich -> filter -> score -> think -> size -> execute -> manage."""

from __future__ import annotations

import logging
import time

from .analysis import gate, indicators, safety, scorer
from .config import Config
from .data.dexscreener import DexScreener, best_snapshots
from .data.rugcheck import RugCheck, parse_report
from .execution.base import ExecutionError, Executor
from .intelligence.analyst import Analyst, rule_decision
from .models import Action, Position, Signal, TokenSnapshot
from .risk import RiskManager
from .storage import Store

log = logging.getLogger("fomobot")
WATCH_SECONDS = 3 * 3600


class Engine:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        executor: Executor,
        dex: DexScreener | None = None,
        rug: RugCheck | None = None,
        gecko=None,
        analyst: Analyst | None = None,
        trade: bool = True,
        ml="auto",
        feed="auto",
        quotes="auto",
    ):
        self.cfg, self.store, self.ex = cfg, store, executor
        self.dex = dex or DexScreener()
        self.rug = rug or RugCheck()
        self.gecko = gecko
        if self.gecko is None and cfg.discovery.use_geckoterminal:
            from .data.geckoterminal import GeckoTerminal

            self.gecko = GeckoTerminal()
        self._ticks = 0
        if feed == "auto":
            from .data.pumpportal import MigrationFeed

            feed = MigrationFeed().start() if cfg.discovery.use_pumpportal else None
        self.feed = feed
        if quotes == "auto":
            from .data.jupiter_quotes import JupiterQuotes

            quotes = JupiterQuotes(cfg.execution.jupiter_base_url, slippage_bps=cfg.execution.slippage_bps) \
                if cfg.execution.roundtrip_check else None
        self.quotes = quotes
        self.analyst = analyst
        if ml == "auto":
            from .intelligence.ml import TradeModel

            ml = TradeModel.load(cfg.ml.model_path) if cfg.ml.enabled else None
        self.ml = ml
        self.ml_threshold = self.ml.threshold(cfg.ml.min_percentile) if self.ml else None
        self.risk = RiskManager(cfg.risk, cfg.strategy, store)
        self.trade = trade  # False = analyse only ("scan")

    # --- discovery ------------------------------------------------------------
    def discover(self, positions: list[Position]) -> dict[str, TokenSnapshot]:
        d = self.cfg.discovery
        mints: list[str] = [p.mint for p in positions]
        watch = {m: t for m, t in (self.store.get_state("watchlist", {}) or {}).items() if time.time() - t < WATCH_SECONDS}
        if self.feed:
            grads = self.store.get_state("graduated", {}) or {}
            for mint, ts in self.feed.drain():
                watch.setdefault(mint, ts)  # fresh graduation: exactly the model's domain
                grads[mint] = ts
            cutoff = time.time() - 24 * 3600
            self.store.set_state("graduated", {m: t for m, t in grads.items() if t > cutoff})
        mints += list(watch)
        if d.use_latest_profiles:
            mints += self.dex.latest_profile_mints()
        if d.use_boosted:
            mints += self.dex.boosted_mints()
        if d.use_top_boosts:
            mints += self.dex.top_boosted_mints()
        if self.gecko and self._ticks % max(1, d.geckoterminal_every_ticks) == 0:
            kinds = ["new_pools"] + (["trending_pools"] if self._ticks % (4 * max(1, d.geckoterminal_every_ticks)) == 0 else [])
            for kind in kinds:
                mints += [p["mint"] for p in self.gecko.pools(kind, 2 if kind == "new_pools" else 1)]
        self._ticks += 1
        snaps = self.dex.snapshots(mints)
        for q in d.search_queries:
            for mint, s in best_snapshots(self.dex.search_pairs(q)).items():
                snaps.setdefault(mint, s)
        self.store.set_state("watchlist", watch)
        return snaps

    # --- portfolio ----------------------------------------------------------
    def portfolio(self, positions: list[Position], snaps: dict[str, TokenSnapshot]) -> dict:
        cash = self.ex.cash_sol()
        exposure = sum(
            p.token_amount * (snaps[p.mint].price_sol if p.mint in snaps else p.entry_price) for p in positions
        )
        return {
            "cash_sol": round(cash, 4),
            "exposure_sol": round(exposure, 4),
            "equity_sol": round(cash + exposure, 4),
            "open_positions": [
                {"symbol": p.symbol, "pnl_pct": round(((snaps[p.mint].price_sol / p.entry_price) - 1) * 100, 1) if p.mint in snaps else None}
                for p in positions
            ],
        }

    # --- position management -----------------------------------------------
    def manage_positions(self, positions: list[Position], snaps: dict[str, TokenSnapshot]) -> None:
        for pos in positions:
            snap = snaps.get(pos.mint)
            order = self.risk.check_exit(pos, snap)
            self.store.update_position(pos)  # persists peak price
            if not order:
                continue
            try:
                fill = self.ex.sell(pos, order.fraction, snap)
            except ExecutionError as e:
                log.error("SELL %s failed: %s", pos.symbol, e)
                continue
            cost_part = pos.cost_sol * min(1.0, order.fraction)
            pos.realized_sol += fill.sol_amount
            pos.cost_sol -= cost_part
            pos.token_amount -= fill.token_amount
            if pos.token_raw is not None and fill.token_raw is not None:
                pos.token_raw -= fill.token_raw
            self.store.record_trade(
                position_id=pos.id, mint=pos.mint, symbol=pos.symbol, side="SELL", sol_amount=fill.sol_amount,
                token_amount=fill.token_amount, price_sol=fill.price_sol, reason=order.reason,
                signature=fill.signature, mode=self.ex.mode,
            )
            log.info("SELL %s %.0f%% -> %.4f SOL (%s)", pos.symbol, order.fraction * 100, fill.sol_amount, order.reason)
            if order.fraction >= 0.999 or pos.token_amount <= 0:
                self._close(pos, order.reason)
            else:
                pos.tp_hit = True
                self.store.update_position(pos)

    def _close(self, pos: Position, reason: str) -> None:
        buys = [t for t in self.store.trades_for(pos.id) if t["side"] == "BUY"]
        invested = sum(t["sol_amount"] for t in buys)
        pnl = pos.realized_sol - invested
        self.store.close_position(pos, pnl, reason)
        log.info("CLOSED %s pnl %+.4f SOL (%+.1f%%) - %s", pos.symbol, pnl, pnl / invested * 100 if invested else 0, reason)
        if self.analyst and self.cfg.llm.reflect_on_closed_trades:
            summary = {
                "symbol": pos.symbol,
                "entry_thesis": pos.thesis,
                "hold_minutes": round((time.time() - pos.opened_at) / 60),
                "pnl_sol": round(pnl, 4),
                "pnl_pct": round(pnl / invested * 100, 1) if invested else None,
                "exit_reason": reason,
                "trades": [dict(t) for t in self.store.trades_for(pos.id)],
            }
            lesson = self.analyst.reflect(summary)
            if lesson:
                self.store.add_lesson(pos.id, lesson)
                log.info("LESSON: %s", lesson)

    # --- analysis -------------------------------------------------------------
    def analyse(self, snaps: dict[str, TokenSnapshot], skip: set[str]) -> list[Signal]:
        pre_cfg = self.cfg.safety.model_copy(update={"use_rugcheck": False})
        signals: list[Signal] = []
        funnel = {"seen": 0, "market_filter": 0, "rugcheck": 0, "entry_gate": 0, "ml_model": 0, "candidates": 0}
        watch = self.store.get_state("watchlist", {}) or {}
        graduated = self.store.get_state("graduated", {}) or {}
        in_domain = lambda s: 0 if (s.pair_created_ms and s.age_minutes <= self.cfg.ml.max_age_minutes) else 1
        cands = sorted((s for m, s in snaps.items() if m not in skip), key=lambda s: (in_domain(s), -s.volume_h1))
        # The market filter needs no network calls, so run it on everything; only the
        # survivors (capped by max_candidates) cost RugCheck / model calls.
        survivors = []
        for snap in cands:
            funnel["seen"] += 1
            if snap.pair_created_ms and snap.age_minutes <= self.cfg.ml.max_age_minutes:
                # Young tokens are where the edge is: keep re-checking them while they mature
                # (e.g. still "too young", or liquidity not yet indexed after graduation).
                watch.setdefault(snap.mint, time.time())
            if safety.assess(snap, None, pre_cfg).passed:
                survivors.append(snap)
            else:
                funnel["market_filter"] += 1
        for snap in survivors[: self.cfg.discovery.max_candidates]:
            rug = None
            if self.cfg.safety.use_rugcheck:
                raw = self.rug.report(snap.mint)
                rug = parse_report(raw) if raw else None
            canonical = snap.mint in graduated and snap.dex_id == "pumpswap"
            rep = safety.assess(snap, rug, self.cfg.safety, canonical_graduation=canonical)
            if not rep.passed:
                funnel["rugcheck"] += 1
                log.debug("reject %s: %s", snap.symbol, rep.hard_fail_reasons)
                continue
            watch.setdefault(snap.mint, time.time())
            history = self.store.price_history(snap.mint)
            ok, gate_fails = gate.check(snap, history, self.cfg.strategy.entry_gate)
            if not ok:
                funnel["entry_gate"] += 1
                log.debug("gate %s: %s", snap.symbol, gate_fails)
                continue
            prob = None
            if self.ml and snap.age_minutes > self.cfg.ml.max_age_minutes:
                if self.cfg.ml.reject_out_of_domain:
                    funnel["ml_model"] += 1
                    continue
            elif self.ml:
                prob = self.ml.predict(snap, rug)
                if prob < self.ml_threshold:
                    funnel["ml_model"] += 1
                    log.debug("ml %s: p=%.2f < %.2f", snap.symbol, prob, self.ml_threshold)
                    continue
            ind = indicators.compute(snap, history)
            sig = scorer.score(snap, rep, ind)
            sig.ml_prob, sig.ml_threshold = prob, self.ml_threshold
            signals.append(sig)
        self.store.set_state("watchlist", watch)
        funnel["candidates"] = len(signals)
        log.info("funnel: %s seen -> rejected market %s, rugcheck %s, entry gate %s, ml model %s -> %s candidates",
                 *funnel.values())
        # The ML probability is the validated ranking; the rule score is a tie-breaker.
        return sorted(signals, key=lambda s: (-(s.ml_prob or 0), -s.score))

    def decide_and_enter(self, signals: list[Signal], positions: list[Position], snaps) -> None:
        st = self.cfg.strategy
        for sig in signals:
            snap = sig.snapshot
            min_score = st.llm_min_score if self.analyst else st.rule_buy_threshold
            if sig.ml_prob is None and sig.score < min_score:
                continue  # without the ML model, fall back to the rule-score threshold
            if self.store.recent_decision(snap.mint, self.cfg.llm.decision_cache_minutes * 60):
                continue
            pf = self.portfolio(positions, snaps)
            decision = None
            if self.analyst:
                decision = self.analyst.decide(sig, pf, self.store.lessons(self.cfg.llm.lessons_in_prompt))
            if decision is None:
                decision = rule_decision(sig, st.rule_buy_threshold, st.default_take_profit_pct, st.default_stop_loss_pct)
            if decision.source == "llm" and decision.conviction < st.llm_min_conviction:
                decision.action = Action.SKIP
            self.store.record_decision(
                snap.mint, snap.symbol, sig.score, decision.action.value, decision.conviction, decision.source,
                decision.thesis, {"risks": decision.risks, "reasons": sig.reasons, "breakdown": sig.breakdown},
            )
            log.info(
                "%s %s score=%.0f conviction=%.0f [%s] %s",
                decision.action.value, snap.symbol, sig.score, decision.conviction, decision.source, decision.thesis[:160],
            )
            if decision.action != Action.BUY or not self.trade:
                continue
            decision.size_multiplier *= sig.safety.size_factor
            check = self.risk.check_entry(
                decision, snap.mint, pf["equity_sol"], pf["cash_sol"], positions, pf["exposure_sol"]
            )
            if not check.allowed:
                log.info("  not entering %s: %s", snap.symbol, check.reason)
                continue
            rt_loss = None
            if self.quotes:
                rt = self.quotes.round_trip(snap.mint, check.size_sol)
                if not rt.ok or rt.loss_pct > self.cfg.execution.max_roundtrip_loss_pct:
                    why = rt.reason or f"round trip would lose {rt.loss_pct:.1f}%"
                    log.info("  not entering %s: %s", snap.symbol, why)
                    continue
                rt_loss = rt.loss_pct
            try:
                fill = self.ex.buy(snap, check.size_sol, expected_cost_pct=rt_loss)
            except ExecutionError as e:
                log.error("BUY %s failed: %s", snap.symbol, e)
                continue
            pos = Position(
                id=None, mint=snap.mint, symbol=snap.symbol, entry_price=fill.price_sol,
                token_amount=fill.token_amount, token_raw=fill.token_raw, cost_sol=fill.sol_amount,
                opened_at=time.time(), stop_loss_pct=decision.stop_loss_pct,
                take_profit_pct=decision.take_profit_pct, peak_price=fill.price_sol, thesis=decision.thesis,
            )
            self.store.insert_position(pos)
            self.store.record_trade(
                position_id=pos.id, mint=snap.mint, symbol=snap.symbol, side="BUY", sol_amount=fill.sol_amount,
                token_amount=fill.token_amount, price_sol=fill.price_sol, reason=decision.thesis[:500],
                signature=fill.signature, mode=self.ex.mode,
            )
            log.info("BUY %s %.4f SOL @ %.10f SOL (TP +%.0f%% / SL -%.0f%%)", snap.symbol, fill.sol_amount,
                     fill.price_sol, decision.take_profit_pct, decision.stop_loss_pct)
            positions.append(pos)

    # --- loop -----------------------------------------------------------------
    def tick(self) -> None:
        positions = self.store.open_positions()
        snaps = self.discover(positions)
        for s in snaps.values():
            self.store.add_price(s.mint, s.price_sol, s.liquidity_usd, s.fetched_at)
        self.store.commit()

        if self.trade:
            self.manage_positions(positions, snaps)
            positions = self.store.open_positions()

        pf = self.portfolio(positions, snaps)
        paused = self.risk.trading_paused(pf["equity_sol"]) if self.trade else None
        if paused:
            log.warning("Entries paused: %s", paused)
            return
        signals = self.analyse(snaps, skip={p.mint for p in positions})
        log.info("tick: %d tokens seen, %d candidates, equity %.4f SOL (%d open)",
                 len(snaps), len(signals), pf["equity_sol"], len(positions))
        self.decide_and_enter(signals, positions, snaps)

    def run(self) -> None:
        log.info("FOMO bot running in %s mode", self.ex.mode.upper())
        n = 0
        while True:
            start = time.time()
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("tick failed")
            n += 1
            if n % 100 == 0:
                self.store.prune_prices()
            time.sleep(max(1.0, self.cfg.tick_seconds - (time.time() - start)))
