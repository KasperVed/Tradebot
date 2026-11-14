"""Hard safety filters. A token that fails here is never traded, whatever the score."""

from __future__ import annotations

from ..config import SafetyConfig
from ..models import SafetyReport, TokenSnapshot


# RugCheck flags fresh PumpSwap pools as "LP unlocked" until it re-indexes the burn done by
# pump.fun's migration (older pools then show 100% locked). For a graduation we saw happen
# live on PumpPortal, these LP findings are reporting lag, not risk.
LP_LAG_RISKS = {"Large Amount of LP Unlocked", "Low amount of LP Providers"}


def assess(snap: TokenSnapshot, rug: dict | None, cfg: SafetyConfig, canonical_graduation: bool = False) -> SafetyReport:
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
            if rug.get("top10_pct") is not None and rug["top10_pct"] > cfg.max_top10_pct:
                hard.append(f"top 10 wallets own {rug['top10_pct']:.1f}%")
            if rug.get("creator_pct") is not None and rug["creator_pct"] > cfg.max_creator_pct:
                hard.append(f"creator still holds {rug['creator_pct']:.1f}%")
            if rug.get("insider_pct") is not None and rug["insider_pct"] > cfg.max_insider_pct:
                hard.append(f"insider networks hold {rug['insider_pct']:.1f}%")
            elif rug.get("insiders_detected", 0) >= 20:
                flags.append(f"{rug['insiders_detected']} insider wallets detected")
                score -= 10
            if rug.get("transfer_fee_pct", 0) > cfg.max_transfer_fee_pct:
                hard.append(f"transfer fee {rug['transfer_fee_pct']}%")
            holders = rug.get("total_holders")
            if holders is not None and holders < cfg.min_holders:
                hard.append(f"only {holders} holders")
            # Pump.fun / bonding-curve style tokens often have burned LP reported differently,
            # so a missing LP figure is a soft flag, a low figure is a hard fail.
            lp_low = rug["lp_locked_pct"] is not None and rug["lp_locked_pct"] < cfg.min_lp_locked_pct
            if rug["lp_locked_pct"] is None:
                flags.append("LP lock unknown")
                score -= 10
            elif lp_low and canonical_graduation:
                flags.append("LP burn not yet indexed (canonical pump.fun graduation)")
            elif lp_low:
                hard.append(f"only {rug['lp_locked_pct']:.0f}% LP locked")
            dangers = [r for r in rug["danger_risks"] if not (canonical_graduation and r in LP_LAG_RISKS)]
            if len(dangers) > cfg.max_rugcheck_danger_risks:
                hard.append("RugCheck danger: " + ", ".join(dangers))
            for w in rug["warn_risks"]:
                flags.append(f"RugCheck warn: {w}")
                score -= 5

    report.score = max(0.0, score)
    report.passed = not hard
    return report
