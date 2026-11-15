"""Claude as the bot's analyst: reviews quantitative signals, decides on entries and
writes post-trade lessons that are fed back into future decisions."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from ..config import LLMConfig
from ..models import Action, Decision, Signal

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the head analyst of an automated Solana memecoin trading bot.
The bot has already run hard safety filters (mint/freeze authority, LP lock, holder
concentration, liquidity, age) and computed a quantitative score. Your job is the
judgment layer: decide whether this specific setup is worth risking capital on right now.

How to think:
- Memecoins are extremely risky. Most go to zero. Default to SKIP unless the setup is
  genuinely good; missing a trade costs nothing, a bad entry costs money.
- Good setups: rising volume with sustained buyer dominance, a healthy liquidity/market
  cap ratio, a trend that is building rather than already parabolic, and a plausible
  reason for continued attention (socials, boosts, steady organic flow).
- Red flags: vertical candles (buying someone else's exit), volume spikes with sellers
  dominating, thin liquidity, extreme holder concentration, fading volume after a pump,
  very young pairs, anything that looks like a coordinated pump or wash trading.
- Set take-profit and stop-loss levels that fit the token's volatility. Stops between
  10% and 35%, take-profits between 25% and 300%.
- conviction is your probability-weighted confidence (0-100) that the trade has positive
  expected value. size_multiplier (0.25-1.5) scales position size; use < 1 when unsure.
- If an ml_model probability is given, it is the most validated signal you have: it passed
  an out-of-sample test on real trades. Weigh it heavily, and use your judgment mainly to
  veto setups with red flags the model cannot see.
- Use the lessons from past trades. They are the bot's own experience.
- If historical base rates are given, they are out-of-sample backtest results for setups
  that pass the same entry gate. Calibrate conviction against them: a typical setup
  should get a conviction close to what the base rate justifies, not higher.

Meme appeal: memecoins live on attention. Judge the name, ticker, description and image as
a meme: is it genuinely funny or culturally timely, instantly understandable, shareable, and
original rather than one of dozens of copies of the same trend? Give meme_score 0-10 and one
line in meme_reason. Treat it as a secondary signal: the numbers and the ML model come first,
and an obviously viral meme often means you are late.

Be concise and concrete in the thesis: name the specific numbers that drive the call."""

REFLECT_PROMPT = """You review closed trades for an automated Solana memecoin bot.
Write ONE short, concrete, reusable lesson (max 2 sentences) that would improve future
entry or exit decisions. Refer to measurable signals (e.g. buy pressure, volume
acceleration, age, momentum), not to this specific token's name. If the trade was
reasonable and the outcome was noise, say what confirmed the process was right."""


class EntryDecision(BaseModel):
    action: Literal["BUY", "SKIP"]
    conviction: float = Field(description="0-100")
    size_multiplier: float = Field(description="0.25-1.5")
    take_profit_pct: float
    stop_loss_pct: float
    thesis: str
    risks: list[str]
    meme_score: float = Field(description="0-10 meme appeal")
    meme_reason: str


class Lesson(BaseModel):
    lesson: str


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def signal_brief(sig: Signal) -> dict:
    s, i = sig.snapshot, sig.indicators
    return {
        "token": {"symbol": s.symbol, "name": s.name, "mint": s.mint, "dex": s.dex_id, "age_minutes": round(s.age_minutes)},
        "market": {
            "price_usd": s.price_usd,
            "market_cap_usd": round(s.market_cap_usd),
            "liquidity_usd": round(s.liquidity_usd),
            "volume_usd": {"m5": round(s.volume_m5), "h1": round(s.volume_h1), "h6": round(s.volume_h6), "h24": round(s.volume_h24)},
            "price_change_pct": {"m5": s.change_m5, "h1": s.change_h1, "h6": s.change_h6, "h24": s.change_h24},
            "txns": {"m5": {"buys": s.buys_m5, "sells": s.sells_m5}, "h1": {"buys": s.buys_h1, "sells": s.sells_h1}},
            "has_socials": s.has_socials, "has_website": s.has_website, "boosted": s.boosted,
        },
        "indicators": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(i).items()},
        "safety": {
            "score": sig.safety.score, "flags": sig.safety.flags,
            "top_holder_pct": sig.safety.top_holder_pct, "lp_locked_pct": sig.safety.lp_locked_pct,
        },
        "ml_model": None if sig.ml_prob is None else {
            "p_profitable_trade": round(sig.ml_prob, 3),
            "entry_threshold_top10pct": round(sig.ml_threshold, 3),
            "note": "trained on ~11k real memecoin trades; out of sample, the top 10% of scores "
                    "averaged about +14% per trade while all tokens averaged about -11%",
        },
        "quant_score": sig.score,
        "score_breakdown": {k: round(v, 1) for k, v in sig.breakdown.items()},
        "quant_reasons": sig.reasons,
    }


class Analyst:
    def __init__(self, cfg: LLMConfig, client: anthropic.Anthropic | None = None):
        self.cfg = cfg
        self.client = client or anthropic.Anthropic()
        self._calls: list[float] = []
        self.base_rates = load_base_rates(cfg.backtest_result_path)

    def _budget_ok(self) -> bool:
        now = time.time()
        self._calls = [t for t in self._calls if now - t < 3600]
        return len(self._calls) < self.cfg.max_calls_per_hour

    def _parse(self, system: str, user, schema: type[BaseModel], max_tokens: int = 16000):
        self._calls.append(time.time())
        resp = self.client.beta.messages.parse(
            model=self.cfg.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": self.cfg.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            log.warning("Claude declined the request (%s)", getattr(resp.stop_details, "category", None))
            return None
        if resp.stop_reason == "max_tokens":
            log.warning("Claude response truncated (max_tokens)")
            return None
        return resp.parsed_output

    def decide(self, sig: Signal, portfolio: dict, lessons: list[str]) -> Decision | None:
        """Returns None if the LLM is unavailable (caller falls back to rules)."""
        if not self._budget_ok():
            log.info("LLM hourly call budget reached; using rules")
            return None
        brief = signal_brief(sig)
        if sig.snapshot.description:
            brief["token"]["description"] = sig.snapshot.description
        text = (
            "Candidate token:\n" + json.dumps(brief, indent=1)
            + "\n\nPortfolio:\n" + json.dumps(portfolio, indent=1)
            + "\n\nLessons from past trades (newest first):\n"
            + ("\n".join(f"- {l}" for l in lessons) if lessons else "- none yet")
            + ("\n\nHistorical base rates (out-of-sample backtest):\n" + json.dumps(self.base_rates, indent=1)
               if self.base_rates else "")
            + "\n\nDecide: BUY or SKIP."
        )
        user: list[dict] = []
        if sig.snapshot.image_url:  # let Claude see the meme itself
            user.append({"type": "image", "source": {"type": "url", "url": sig.snapshot.image_url}})
        user.append({"type": "text", "text": text})
        try:
            out: EntryDecision | None = self._parse(SYSTEM_PROMPT, user, EntryDecision)
        except anthropic.BadRequestError as e:
            if not sig.snapshot.image_url:
                log.warning("Claude API error: %s; using rules", e)
                return None
            log.info("image rejected (%s); retrying without it", e)  # dead or unsupported image URL
            try:
                out = self._parse(SYSTEM_PROMPT, [user[-1]], EntryDecision)
            except (anthropic.APIConnectionError, anthropic.APIStatusError) as e2:
                log.warning("Claude API error: %s; using rules", e2)
                return None
        except anthropic.RateLimitError:
            log.warning("Claude rate limited; using rules")
            return None
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            log.warning("Claude API error: %s; using rules", e)
            return None
        if out is None:
            return None
        return Decision(
            action=Action(out.action),
            conviction=_clamp(out.conviction, 0, 100),
            size_multiplier=_clamp(out.size_multiplier, 0.25, 1.5),
            take_profit_pct=_clamp(out.take_profit_pct, 15, 300),
            stop_loss_pct=_clamp(out.stop_loss_pct, 5, 40),
            thesis=out.thesis + f" | meme {out.meme_score:.0f}/10: {out.meme_reason}",
            risks=out.risks,
            source="llm",
            meme_score=_clamp(out.meme_score, 0, 10),
        )

    def reflect(self, trade_summary: dict) -> str | None:
        if not self._budget_ok():
            return None
        try:
            out: Lesson | None = self._parse(REFLECT_PROMPT, json.dumps(trade_summary, indent=1, default=str), Lesson, 4000)
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as e:
            log.warning("Reflection failed: %s", e)
            return None
        return out.lesson.strip() if out else None


def load_base_rates(path: str | None) -> dict | None:
    if not path:
        return None
    try:
        with open(path) as f:
            res = json.load(f)
    except (OSError, ValueError):
        return None
    test = (res.get("results") or {}).get("test") or {}
    rnd = (res.get("results") or {}).get("test_random_entry") or {}
    if not test.get("n"):
        return None
    keep = ("n", "tokens", "win_rate", "mean", "median", "profit_factor", "avg_win", "avg_loss", "avg_hold")
    return {
        "gate_signal_trades": {k: round(test[k], 4) if isinstance(test[k], float) else test[k] for k in keep if k in test},
        "random_entry_same_exits": {k: round(rnd[k], 4) for k in ("mean", "win_rate") if k in rnd},
        "exit_rules": res.get("exit"),
        "note": "returns are per trade on stake, after ~2.5% round-trip fees and slippage",
    }


def rule_decision(sig: Signal, threshold: float, tp: float, sl: float) -> Decision:
    """Deterministic fallback when Claude is disabled or unavailable."""
    vol = sig.indicators.volatility
    if sig.ml_prob is not None:  # ML gate already applied upstream; conviction from its probability
        return Decision(
            action=Action.BUY if sig.safety.passed else Action.SKIP,
            conviction=_clamp(50 + (sig.ml_prob - (sig.ml_threshold or 0.4)) * 150, 50, 95),
            size_multiplier=0.75, take_profit_pct=tp, stop_loss_pct=sl,
            thesis=f"ML p(profit)={sig.ml_prob:.2f}; " + ("; ".join(sig.reasons) or "rule score"),
            risks=sig.safety.flags, source="rules+ml",
        )
    buy = sig.safety.passed and sig.score >= threshold
    return Decision(
        action=Action.BUY if buy else Action.SKIP,
        conviction=_clamp(sig.score, 0, 100),
        size_multiplier=0.75,  # rules alone get smaller size than an LLM-confirmed entry
        take_profit_pct=tp,
        stop_loss_pct=_clamp(max(sl, vol * 3), 5, 35),
        thesis="; ".join(sig.reasons) or "rule-based score",
        risks=sig.safety.flags,
        source="rules",
    )
