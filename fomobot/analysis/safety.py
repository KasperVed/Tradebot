"""Hard safety filters. A token that fails here is never traded, whatever the score."""

from __future__ import annotations

from ..config import SafetyConfig
from ..models import SafetyReport, TokenSnapshot


def assess(snap: TokenSnapshot, rug: dict | None, cfg: SafetyConfig) -> SafetyReport:
    hard: list[str] = []
    flags: list[str] = []
    score = 100.0

    if snap.liquidity_usd < cfg.min_liquidity_usd:
        hard.append(f"liquidity ${snap.liquidity_usd:,.0f} < ${cfg.min_liquidity_usd:,.0f}")
    if snap.liquidity_usd > cfg.max_liquidity_usd:
        hard.append("liquidity above max (outside strategy universe)")
    if snap.market_cap_usd < cfg.min_market_cap_usd:
        hard.append(f"market cap ${snap.market_cap_usd:,.0f} too small")
    if snap.market_cap_usd > cfg.max_market_cap_usd:
        hard.append("market cap above max")
    age = snap.age_minutes
    if snap.pair_created_ms and age < cfg.min_age_minutes:
        hard.append(f"pair only {age:.0f} min old")
    if snap.pair_created_ms and age > cfg.max_age_hours * 60:
        hard.append("pair older than strategy window")
    if snap.buys_h1 + snap.sells_h1 < cfg.min_txns_h1:
        hard.append(f"only {snap.buys_h1 + snap.sells_h1} txns last hour")
    if snap.liquidity_usd > 0 and snap.market_cap_usd / snap.liquidity_usd > 40:
        flags.append("thin liquidity vs market cap")
        score -= 15
    if not snap.has_socials and not snap.has_website:
        flags.append("no socials or website")
        score -= 10

    report = SafetyReport(mint=snap.mint, passed=False, score=0, flags=flags, hard_fail_reasons=hard)

    if cfg.use_rugcheck:
        if rug is None:
            hard.append("no RugCheck report available")
        else:
            report.mint_authority = rug["mint_authority"]
            report.freeze_authority = rug["freeze_authority"]
            report.top_holder_pct = rug["top_holder_pct"]
            report.lp_locked_pct = rug["lp_locked_pct"]
            if rug["rugged"]:
                hard.append("flagged as rugged")
            if cfg.reject_mint_authority and rug["mint_authority"]:
                hard.append("mint authority not revoked")
            if cfg.reject_freeze_authority and rug["freeze_authority"]:
                hard.append("freeze authority not revoked")
            if rug["top_holder_pct"] is not None and rug["top_holder_pct"] > cfg.max_top_holder_pct:
                hard.append(f"top holder owns {rug['top_holder_pct']:.1f}%")
            # Pump.fun / bonding-curve style tokens often have burned LP reported differently,
            # so a missing LP figure is a soft flag, a low figure is a hard fail.
            if rug["lp_locked_pct"] is None:
                flags.append("LP lock unknown")
                score -= 10
            elif rug["lp_locked_pct"] < cfg.min_lp_locked_pct:
                hard.append(f"only {rug['lp_locked_pct']:.0f}% LP locked")
            if len(rug["danger_risks"]) > cfg.max_rugcheck_danger_risks:
                hard.append("RugCheck danger: " + ", ".join(rug["danger_risks"]))
            for w in rug["warn_risks"]:
                flags.append(f"RugCheck warn: {w}")
                score -= 5

    report.score = max(0.0, score)
    report.passed = not hard
    return report
