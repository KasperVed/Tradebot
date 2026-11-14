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
- Use the lessons from past trades. They are the bot's own experience.

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
        "quant_score": sig.score,
        "score_breakdown": {k: round(v, 1) for k, v in sig.breakdown.items()},
        "quant_reasons": sig.reasons,
    }


class Analyst:
    def __init__(self, cfg: LLMConfig, client: anthropic.Anthropic | None = None):
        self.cfg = cfg
        self.client = client or anthropic.Anthropic()
        self._calls: list[float] = []

    def _budget_ok(self) -> bool:
        now = time.time()
        self._calls = [t for t in self._calls if now - t < 3600]
        return len(self._calls) < self.cfg.max_calls_per_hour

    def _parse(self, system: str, user: str, schema: type[BaseModel], max_tokens: int = 16000):
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
        user = (
            "Candidate token:\n" + json.dumps(signal_brief(sig), indent=1)
            + "\n\nPortfolio:\n" + json.dumps(portfolio, indent=1)
            + "\n\nLessons from past trades (newest first):\n"
            + ("\n".join(f"- {l}" for l in lessons) if lessons else "- none yet")
            + "\n\nDecide: BUY or SKIP."
        )
        try:
            out: EntryDecision | None = self._parse(SYSTEM_PROMPT, user, EntryDecision)
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
            thesis=out.thesis,
            risks=out.risks,
            source="llm",
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


def rule_decision(sig: Signal, threshold: float, tp: float, sl: float) -> Decision:
    """Deterministic fallback when Claude is disabled or unavailable."""
    vol = sig.indicators.volatility
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
