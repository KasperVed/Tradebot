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
    assert pair_to_snapshot(p) is None  # non-SOL quote needs a SOL/USD price to convert


def test_tokens_quoted_in_other_tokens_are_seen():
    sol_pair = make_pair(price=0.0001, mint="OTHER111")  # implies SOL = $150 (priceUsd = native * 150)
    alt = make_pair(price=0.5, liq=40_000, mint=MINT)
    alt["quoteToken"] = {"address": "ANTHROPICmint", "symbol": "ANTHROPIC"}
    alt["priceUsd"] = "0.003"  # $0.003 / $150 = 0.00002 SOL
    snaps = best_snapshots([sol_pair, alt])
    assert snaps[MINT].price_sol == pytest.approx(0.00002)
    # a SOL pair for the same token is preferred over a more liquid non-SOL pair
    own_sol = make_pair(price=0.000021, liq=20_000, mint=MINT)
    assert best_snapshots([sol_pair, alt, own_sol])[MINT].price_sol == pytest.approx(0.000021)


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
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=dex, rug=FakeRug(), analyst=None, ml=None, feed=None, quotes=None, prices=None)
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
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=dex, rug=FakeRug(), analyst=None, ml=None, feed=None, quotes=None, prices=None)
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
                 rug=FakeRug(dict(CLEAN_RUG, freezeAuthority="X")), analyst=None, ml=None, feed=None, quotes=None, prices=None)
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
               "stop_loss_pct": 18, "thesis": "volume 3x with 70% buys", "risks": ["young pair"],
               "meme_score": 7, "meme_reason": "funny, original"}
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": json.dumps(out)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    analyst = Analyst(LLMConfig(), client=client)
    snap = pair_to_snapshot(make_pair())
    snap.image_url, snap.description = "https://img/x.png", "a frog that surfs"
    rep = safety.assess(snap, parse_report(CLEAN_RUG), Config().safety)
    sig = scorer.score(snap, rep, indicators.compute(snap, []))
    d = analyst.decide(sig, {"cash_sol": 5}, ["lesson one"])
    assert d.action == Action.BUY and d.source == "llm"
    assert d.size_multiplier == 1.5  # clamped
    assert captured["model"] == "claude-opus-5-5"
    assert captured["output_config"]["effort"] == "high"
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["fallbacks"] == "default"
    assert d.meme_score == 7 and "meme 7/10" in d.thesis
    content = captured["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["url"] == "https://img/x.png"
    assert "description" in content[-1]["text"]


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
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=FakeDex(), rug=FakeRug(), analyst=None, ml=LowModel(), feed=None, quotes=None, prices=None)
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
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=FakeDex(), rug=FakeRug(), analyst=None, ml=HighModel(), feed=None, quotes=None, prices=None)
    eng.tick()  # FakeDex pair is 3h old, far outside the 30-minute training window
    assert store.open_positions() == []
    cfg.ml.max_age_minutes = 1e9
    eng.tick()
    assert len(store.open_positions()) == 1


def test_pumpportal_message_parsing():
    from fomobot.data.pumpportal import parse_message

    m = parse_message('{"signature": "x", "mint": "ABCpump", "txType": "migrate", "pool": "pump-amm"}')
    assert m and m[0] == "ABCpump"
    assert parse_message('{"message": "Subscribed"}') is None
    assert parse_message('{"mint": "X", "txType": "create"}') is None
    assert parse_message("not json") is None


def test_roundtrip_check_blocks_honeypot(cfg):
    from fomobot.data.jupiter_quotes import RoundTrip

    class Honeypot:
        def round_trip(self, mint, sol):
            return RoundTrip(False, None, None, "no sell route on Jupiter (possible honeypot)")

    class Fine:
        def round_trip(self, mint, sol):
            return RoundTrip(True, 4.0, 1.0)

    store = Store(cfg.db_path)
    cfg.strategy.rule_buy_threshold = 50
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=FakeDex(), rug=FakeRug(), analyst=None,
                 ml=None, feed=None, quotes=Honeypot(), prices=None)
    eng.tick()
    assert store.open_positions() == []
    store.db.execute("DELETE FROM decisions"); store.commit()
    eng.quotes = Fine()
    eng.tick()
    assert len(store.open_positions()) == 1


def test_feed_mints_enter_watchlist(cfg):
    class Feed:
        def drain(self):
            return [("NEWMINT111", time.time())]

    store = Store(cfg.db_path)
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=FakeDex(), rug=FakeRug(), analyst=None,
                 ml=None, feed=Feed(), quotes=None, prices=None)
    eng.discover([])
    assert "NEWMINT111" in store.get_state("watchlist")


def test_lp_lag_waived_only_for_canonical_graduation(cfg):
    snap = pair_to_snapshot(make_pair())
    fresh = dict(CLEAN_RUG, markets=[{"marketType": "pump_fun_amm", "lp": {"lpLockedPct": 1e-10}}],
                 risks=[{"name": "Large Amount of LP Unlocked", "level": "danger"}])
    rug = parse_report(fresh)
    strict = cfg.safety.model_copy(update={"require_lp_locked": True})
    assert not safety.assess(snap, rug, strict).passed
    soft = safety.assess(snap, rug, cfg.safety)  # default: LP is a soft risk -> half size
    assert soft.passed and soft.size_factor == 0.5
    rep = safety.assess(snap, rug, strict, canonical_graduation=True)
    assert rep.passed and rep.size_factor == 1.0 and any("not yet indexed" in f for f in rep.flags)
    other = dict(fresh, risks=[{"name": "Freeze Authority still enabled", "level": "danger"}])
    assert not safety.assess(snap, parse_report(other), strict, canonical_graduation=True).passed


def test_live_executor_closes_only_empty_token_accounts(monkeypatch):
    import base64 as b64

    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction

    from fomobot.execution.jupiter import JupiterExecutor

    kp = Keypair()
    sent = []
    empty, full = str(Keypair().pubkey()), str(Keypair().pubkey())
    token_prog = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

    def acc(pubkey, amount):
        return {"pubkey": pubkey, "account": {"owner": token_prog, "data": {"parsed": {"info": {
            "tokenAmount": {"amount": str(amount)}}}}}}

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        m = body["method"]
        result = {
            "getTokenAccountsByOwner": {"value": [acc(empty, 0), acc(full, 5)]},
            "getLatestBlockhash": {"value": {"blockhash": "11111111111111111111111111111111"}},
            "getSignatureStatuses": {"value": [{"confirmationStatus": "confirmed", "err": None}]},
        }.get(m)
        if m == "sendTransaction":
            sent.append(VersionedTransaction.from_bytes(b64.b64decode(body["params"][0])))
            result = "SIG"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    monkeypatch.setenv("WALLET_PRIVATE_KEY", str(kp))
    ex = JupiterExecutor(Config(), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert ex.close_empty_accounts(MINT) == 1
    ix = sent[0].message.instructions[0]
    keys = sent[0].message.account_keys
    assert bytes(ix.data) == bytes([9])
    assert str(keys[ix.accounts[0]]) == empty  # only the empty account is closed


def test_telegram_notifications_on_buy_and_close(cfg):
    sent = []

    def handler(request: httpx.Request):
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    from fomobot.notify import Notifier

    n = Notifier(token="T", chat_id="1", client=httpx.Client(transport=httpx.MockTransport(handler)))
    n.send = lambda text, blocking=False, _orig=n.send: _orig(text, blocking=True)
    store = Store(cfg.db_path)
    dex = FakeDex()
    cfg.strategy.rule_buy_threshold = 50
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=dex, rug=FakeRug(), analyst=None,
                 ml=None, feed=None, quotes=None, notifier=n, prices=None)
    eng.tick()
    entry = store.open_positions()[0].entry_price
    dex.pairs = [make_pair(price=entry * 0.6)]
    eng.tick()
    assert any("KØB TEST" in t for t in sent)
    assert any("LUKKET TEST" in t and "stop loss" in t for t in sent)
    assert any("Status" in t for t in sent)
    assert all(t.startswith("📝 PAPER") for t in sent)
    assert not Notifier(token="", chat_id="").enabled


def test_wallet_command_creates_key_once(tmp_path, monkeypatch, capsys):
    from fomobot import wallet

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WALLET_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(wallet, "ENV", tmp_path / ".env")
    monkeypatch.setattr(wallet, "balance_sol", lambda a, r: 0.0)
    wallet.wallet_command()
    env = (tmp_path / ".env").read_text()
    key = env.split("WALLET_PRIVATE_KEY=")[1].strip()
    out = capsys.readouterr().out
    assert key and key not in out  # private key never printed
    monkeypatch.setenv("WALLET_PRIVATE_KEY", key)
    wallet.wallet_command()
    assert (tmp_path / ".env").read_text().count("WALLET_PRIVATE_KEY=") == 1


def test_dashboard_snapshot_after_trading(cfg):
    from fomobot.dashboard import snapshot

    store = Store(cfg.db_path)
    dex = FakeDex()
    cfg.strategy.rule_buy_threshold = 50
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=dex, rug=FakeRug(), analyst=None,
                 ml=None, feed=None, quotes=None, prices=None)
    eng.tick()
    eng.tick()
    snap = snapshot(cfg.db_path)
    assert snap["agents"]["mode"] == "paper"
    assert snap["agents"]["scanner"]["tokens_seen"] >= 1
    assert len(snap["equity"]) >= 2 and snap["start_equity"]
    assert snap["open_positions"] and snap["trades"][0]["side"] == "BUY"


def test_take_profit_ladder_sells_in_steps(cfg):
    store = Store(cfg.db_path)
    dex = FakeDex()
    cfg.strategy.rule_buy_threshold = 50
    cfg.strategy.trailing_stop_pct = 90  # isolate the ladder
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=dex, rug=FakeRug(), analyst=None,
                 ml=None, feed=None, quotes=None, prices=None)
    eng.tick()
    pos = store.open_positions()[0]
    entry, start = pos.entry_price, pos.token_amount
    for mult, steps, left in ((1.6, 1, 0.75), (2.1, 2, 0.50), (3.1, 3, 0.25)):
        dex.pairs = [make_pair(price=entry * mult)]
        eng.tick()
        pos = store.open_positions()[0]
        assert pos.tp_steps == steps
        assert pos.token_amount == pytest.approx(start * left)
    sells = [t for t in store.trades_for(pos.id) if t["side"] == "SELL"]
    assert len(sells) == 3


def test_old_database_is_upgraded(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT NOT NULL, symbol TEXT, "
               "entry_price REAL, token_amount REAL, token_raw INTEGER, cost_sol REAL, opened_at REAL, "
               "stop_loss_pct REAL, take_profit_pct REAL, peak_price REAL, tp_hit INTEGER DEFAULT 0, "
               "realized_sol REAL DEFAULT 0, thesis TEXT, status TEXT DEFAULT 'open', closed_at REAL, "
               "pnl_sol REAL, exit_reason TEXT)")
    db.commit()
    db.close()
    assert Store(str(path)).open_positions() == []


def test_fast_exit_fires_between_scans(cfg):
    class FastPrices:
        price = None

        def prices(self, mints):
            return {m: (self.price, None) for m in mints} if self.price else {}

    fp = FastPrices()
    store = Store(cfg.db_path)
    cfg.strategy.rule_buy_threshold = 50
    eng = Engine(cfg, store, PaperExecutor(cfg.execution, store), dex=FakeDex(), rug=FakeRug(), analyst=None,
                 ml=None, feed=None, quotes=None, prices=fp)
    eng.tick()
    entry = store.open_positions()[0].entry_price
    fp.price = entry * 0.6  # crash between scans: the market scan hasn't seen it yet
    eng.fast_exits()
    closed = store.closed_positions()
    assert closed and "stop loss" in closed[0]["exit_reason"]


def test_ml_confidence_tier_sizes_positions(cfg):
    class Model:
        def __init__(self, p):
            self.p = p

        def predict(self, snap, rug):
            return self.p

        def threshold(self, q):
            return {0.9: 0.3, 0.95: 0.5}.get(q, 0.3)

    sizes = {}
    for label, p in (("low", 0.35), ("top", 0.6)):
        c = cfg.model_copy(deep=True)
        c.db_path = cfg.db_path.replace(".db", f"-{label}.db")
        c.ml.max_age_minutes = 1e9
        store = Store(c.db_path)
        eng = Engine(c, store, PaperExecutor(c.execution, store), dex=FakeDex(), rug=FakeRug(), analyst=None,
                     ml=Model(p), feed=None, quotes=None, prices=None)
        eng.tick()
        sizes[label] = store.open_positions()[0].cost_sol
    assert sizes["top"] == pytest.approx(sizes["low"] * 1.25 / 0.75, rel=0.01)


def test_learn_simulate_matches_ladder_rules():
    from fomobot.learn import simulate

    strat = Config().strategy
    flat = [[i * 60, 1, 1, 1, 1, 1] for i in range(10)]
    assert simulate(flat, strat) == pytest.approx((1 - 0.025) / 1.025 - 1, rel=1e-3)
    crash = [[0, 1, 1, 1, 1, 1], [60, 1, 1, 0.5, 0.5, 1]]
    assert simulate(crash, strat) < -0.25


def test_retrain_needs_data_then_only_keeps_a_better_model(cfg, tmp_path):
    import random

    from fomobot.learn import retrain

    store = Store(cfg.db_path)
    assert retrain(store, cfg)["status"] == "not enough data"
    random.seed(1)
    feats = ["time_since_launch_sec", "market_cap_usd", "liquidity_usd", "holder_count", "top10_holder_pct",
             "dev_remaining_pct", "mint_disabled", "freeze_disabled", "lp_burned_pct", "price_change_1h",
             "price_change_6h", "has_twitter", "has_website", "has_telegram", "is_boosted",
             "trade_buy_sell_ratio", "liq_mcap"]
    for i in range(400):
        f = {k: random.random() for k in feats}
        label = 0.3 if f["liq_mcap"] > 0.7 else -0.2  # a pattern the new model can learn
        store.db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
                         (i, f"M{i}", "P", 1.0, random.random(), json.dumps(f), label, i))
    store.commit()
    res = retrain(store, cfg, out_path=str(tmp_path / "m.joblib"))
    assert res["new_top_mean"] > res["holdout_all_mean"]
    assert res["status"].startswith("new model") and (tmp_path / "m.joblib").exists()
