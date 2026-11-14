import json
import time

import anthropic
import httpx2 as httpx
import pytest

from fomobot.analysis import indicators, safety, scorer
from fomobot.config import Config, LLMConfig
from fomobot.data.dexscreener import best_snapshots, pair_to_snapshot
from fomobot.data.rugcheck import parse_report
from fomobot.engine import Engine
from fomobot.execution.paper import PaperExecutor
from fomobot.intelligence.analyst import Analyst, rule_decision
from fomobot.models import WSOL_MINT, Action, Decision
from fomobot.risk import RiskManager
from fomobot.storage import Store

MINT = "Tok1111111111111111111111111111111111111111"


def make_pair(price=0.0001, liq=60_000, mcap=400_000, m5=4.0, h1=25.0, buys=300, sells=150, vol_m5=12_000, mint=MINT):
    return {
        "chainId": "solana", "dexId": "raydium", "pairAddress": "PAIR" + mint[:6],
        "baseToken": {"address": mint, "symbol": "TEST", "name": "Test Coin"},
        "quoteToken": {"address": WSOL_MINT, "symbol": "SOL"},
        "priceNative": str(price), "priceUsd": str(price * 150),
        "txns": {"m5": {"buys": buys // 10, "sells": sells // 10}, "h1": {"buys": buys, "sells": sells}},
        "volume": {"m5": vol_m5, "h1": 60_000, "h6": 200_000, "h24": 500_000},
        "priceChange": {"m5": m5, "h1": h1, "h6": 40, "h24": 80},
        "liquidity": {"usd": liq}, "fdv": mcap, "marketCap": mcap,
        "pairCreatedAt": int((time.time() - 3 * 3600) * 1000),
        "info": {"socials": [{"type": "twitter"}], "websites": [{"url": "x"}]},
    }


CLEAN_RUG = {
    "mintAuthority": None, "freezeAuthority": None,
    "topHolders": [{"pct": 6.0}, {"pct": 4.0}],
    "markets": [{"lp": {"lpLockedPct": 100}}],
    "risks": [], "rugged": False,
}


class FakeDex:
    def __init__(self):
        self.pairs = [make_pair()]

    def latest_profile_mints(self):
        return [MINT]

    def boosted_mints(self):
        return []

    def top_boosted_mints(self):
        return []

    def search_pairs(self, q):
        return []

    def snapshots(self, mints):
        return best_snapshots(self.pairs)


class FakeRug:
    def __init__(self, report=CLEAN_RUG):
        self.r = report

    def report(self, mint):
        return self.r


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.db_path = str(tmp_path / "t.db")
    c.discovery.search_queries = []
    c.discovery.use_geckoterminal = False
    c.strategy.entry_gate.enabled = True
    c.llm.enabled = False
    return c


def test_pair_parsing_requires_sol_quote():
    assert pair_to_snapshot(make_pair()).price_sol == pytest.approx(0.0001)
    p = make_pair()
    p["quoteToken"]["address"] = "USDC"
    assert pair_to_snapshot(p) is None


def test_safety_rejects_mint_authority_and_whales(cfg):
    snap = pair_to_snapshot(make_pair())
    bad = dict(CLEAN_RUG, mintAuthority="Abc", topHolders=[{"pct": 60}])
    rep = safety.assess(snap, parse_report(bad), cfg.safety)
    assert not rep.passed
    assert any("mint authority" in r for r in rep.hard_fail_reasons)
    assert any("top holder" in r for r in rep.hard_fail_reasons)
    assert safety.assess(snap, parse_report(CLEAN_RUG), cfg.safety).passed


def test_scorer_rewards_momentum_and_penalizes_parabolic(cfg):
    rep = safety.assess(pair_to_snapshot(make_pair()), parse_report(CLEAN_RUG), cfg.safety)
    good = pair_to_snapshot(make_pair())
    dump = pair_to_snapshot(make_pair(m5=-8, h1=-30, buys=100, sells=300, vol_m5=2000))
    para = pair_to_snapshot(make_pair(m5=60, h1=400))
    s = lambda snap: scorer.score(snap, rep, indicators.compute(snap, [])).score
    assert s(good) > s(dump)
    assert s(good) > s(para)


def test_rsi_and_ema():
    up = [1 + i * 0.01 for i in range(30)]
    assert indicators.rsi(up) == 100.0
    assert indicators.ema(up, 5) > indicators.ema(up, 15)


def test_risk_sizing_and_limits(cfg, tmp_path):
    store = Store(cfg.db_path)
    rm = RiskManager(cfg.risk, cfg.strategy, store)
    d = Decision(Action.BUY, 80, 1.0, 50, 20, "", [], "llm")
    chk = rm.check_entry(d, MINT, equity_sol=5, cash_sol=5, positions=[], exposure_sol=0)
    assert chk.allowed and 0 < chk.size_sol <= cfg.risk.max_position_sol
    chk = rm.check_entry(d, MINT, equity_sol=5, cash_sol=0.05, positions=[], exposure_sol=0)
    assert not chk.allowed  # reserve protects fee money


def test_full_paper_cycle_buy_take_profit_trail_close(cfg):
    store = Store(cfg.db_path)
    dex = FakeDex()
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=dex, rug=FakeRug(), analyst=None, ml=None)
    cfg.strategy.rule_buy_threshold = 50

    eng.tick()
    pos = store.open_positions()
    assert len(pos) == 1, "expected the bot to buy the strong setup"
    entry = pos[0].entry_price
    assert store.get_state("paper_cash") < cfg.execution.paper_starting_sol

    # Price runs past take-profit -> partial sell, remainder trails.
    dex.pairs = [make_pair(price=entry * 2.2)]
    eng.tick()
    pos = store.open_positions()
    assert len(pos) == 1 and pos[0].tp_hit

    # Price falls 40% from peak -> trailing stop closes the rest.
    dex.pairs = [make_pair(price=entry * 2.2 * 0.6)]
    eng.tick()
    assert not [p for p in store.open_positions() if p.mint == MINT]
    closed = store.closed_positions()
    assert closed[0]["pnl_sol"] > 0
    assert store.get_state("paper_cash") > cfg.execution.paper_starting_sol


def test_stop_loss(cfg):
    store = Store(cfg.db_path)
    dex = FakeDex()
    cfg.strategy.rule_buy_threshold = 50
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=dex, rug=FakeRug(), analyst=None, ml=None)
    eng.tick()
    entry = store.open_positions()[0].entry_price
    dex.pairs = [make_pair(price=entry * 0.7)]
    eng.tick()
    closed = store.closed_positions()
    assert closed and "stop loss" in closed[0]["exit_reason"] and closed[0]["pnl_sol"] < 0


def test_rugged_token_never_bought(cfg):
    store = Store(cfg.db_path)
    cfg.strategy.rule_buy_threshold = 0
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=FakeDex(),
                 rug=FakeRug(dict(CLEAN_RUG, freezeAuthority="X")), analyst=None, ml=None)
    eng.tick()
    assert store.open_positions() == []


def test_rule_decision():
    snap = pair_to_snapshot(make_pair())
    rep = safety.assess(snap, parse_report(CLEAN_RUG), Config().safety)
    sig = scorer.score(snap, rep, indicators.compute(snap, []))
    assert rule_decision(sig, 0, 50, 20).action == Action.BUY
    assert rule_decision(sig, 101, 50, 20).action == Action.SKIP


def test_analyst_parses_structured_decision():
    captured = {}

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        captured.update(body)
        out = {"action": "BUY", "conviction": 82, "size_multiplier": 3.0, "take_profit_pct": 80,
               "stop_loss_pct": 18, "thesis": "volume 3x with 70% buys", "risks": ["young pair"]}
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": json.dumps(out)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    analyst = Analyst(LLMConfig(), client=client)
    snap = pair_to_snapshot(make_pair())
    rep = safety.assess(snap, parse_report(CLEAN_RUG), Config().safety)
    sig = scorer.score(snap, rep, indicators.compute(snap, []))
    d = analyst.decide(sig, {"cash_sol": 5}, ["lesson one"])
    assert d.action == Action.BUY and d.source == "llm"
    assert d.size_multiplier == 1.5  # clamped
    assert captured["model"] == "claude-opus-5-5"
    assert captured["output_config"]["effort"] == "high"
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["fallbacks"] == "default"


def test_ml_model_loads_and_predicts():
    from fomobot.intelligence.ml import TradeModel, live_features

    m = TradeModel.load()
    assert m is not None
    snap = pair_to_snapshot(make_pair())
    rug = parse_report(CLEAN_RUG)
    p = m.predict(snap, rug)
    assert 0.0 <= p <= 1.0
    f = live_features(snap, rug)
    assert set(m.features) <= set(f)
    assert f["top10_holder_pct"] == pytest.approx(0.10)  # fraction, as in training data
    assert 0 < m.threshold(0.9) < 1


def test_ml_gate_blocks_low_probability(cfg):
    class LowModel:
        def predict(self, snap, rug):
            return 0.1

        def threshold(self, q):
            return 0.5

    store = Store(cfg.db_path)
    cfg.strategy.rule_buy_threshold = 0
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=FakeDex(), rug=FakeRug(), analyst=None, ml=LowModel())
    eng.tick()
    assert store.open_positions() == []


def test_ml_rejects_tokens_outside_training_domain(cfg):
    class HighModel:
        def predict(self, snap, rug):
            return 0.99

        def threshold(self, q):
            return 0.5

    store = Store(cfg.db_path)
    cfg.strategy.rule_buy_threshold = 0
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=FakeDex(), rug=FakeRug(), analyst=None, ml=HighModel())
    eng.tick()  # FakeDex pair is 3h old, far outside the 30-minute training window
    assert store.open_positions() == []
    cfg.ml.max_age_minutes = 1e9
    eng.tick()
    assert len(store.open_positions()) == 1
