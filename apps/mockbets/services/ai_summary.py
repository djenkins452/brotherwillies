"""AI Postgame Analyst Summary — fact-driven plain-English recap.

This is a sibling of ai_commentary.py but feeds OFF the command_center
analytics object so the AI summary and the deterministic dashboard are
guaranteed to be reading from the same set of facts.

Design rules (enforced via system prompt + structural input):
  - Facts in the user prompt come from build_command_center() ONLY.
    Nothing the AI says should reference data that isn't in the prompt.
  - System prompt repeatedly forbids invented stats, real-money advice,
    and over-confident long-term-edge claims on small samples.
  - When the API key is missing or the call fails, we fall back to a
    deterministic narrative built from the same analytics object —
    operators are never left with a blank panel.
  - The view labels the result as an "AI Summary" so users know it's a
    machine narrative, not a primary signal.
"""
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)


def _format_money(value):
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return '$0.00'


def _format_bet_one_liner(bet) -> str:
    """Compact identifier for an individual bet referenced in the prompt."""
    if bet is None:
        return ''
    odds = bet.odds_american
    odds_str = f"+{odds}" if odds and odds > 0 else f"{odds}"
    selection = (bet.selection or '').strip() or '?'
    sport = (bet.sport or '?').upper()
    return f"{sport} {selection} ({odds_str})"


def _build_user_prompt(cc: dict) -> str:
    """Render the deterministic facts as a single text block for the AI.

    Only includes a section when its underlying capability flag is True —
    avoids feeding the AI all-zero rows that would make it confabulate.
    """
    caps = cc['capabilities']
    parts = ['MOCK BET ANALYTICS — FACTS', '=' * 40, '']

    kpis = cc['kpis']
    parts.append('SUMMARY')
    parts.append(f"  Total bets: {kpis['total_bets']}")
    parts.append(f"  Settled: {kpis['settled_count']}, Pending: {kpis['pending_count']}")
    parts.append(f"  Record (W-L-P): {kpis['wins']}-{kpis['losses']}-{kpis['pushes']}")
    parts.append(f"  Win rate: {kpis['win_pct']:.1f}% (excludes pushes)")
    parts.append(f"  Net P/L: {_format_money(kpis['net_pl'])}")
    parts.append(f"  ROI: {kpis['roi']:.1f}%")
    parts.append(f"  Avg odds: {kpis['avg_odds']:+d}")

    clv = cc['clv']
    if clv['sample_size']:
        parts.append('')
        parts.append('CLOSING LINE VALUE (CLV)')
        parts.append(f"  Bets with CLV captured: {clv['sample_size']}")
        parts.append(f"  Positive-CLV rate: {clv['positive_rate']:.1f}%")
        parts.append(f"  Average CLV: {clv['avg_clv']:+.4f} (decimal-odds delta)")
        if clv['best']:
            parts.append(
                f"  Best CLV bet: {_format_bet_one_liner(clv['best'])} "
                f"(CLV {clv['best'].clv_cents:+.4f})"
            )
        if clv['worst']:
            parts.append(
                f"  Worst CLV bet: {_format_bet_one_liner(clv['worst'])} "
                f"(CLV {clv['worst'].clv_cents:+.4f})"
            )
    else:
        parts.append('')
        parts.append('CLV: no closing-odds data captured for any settled bet yet.')

    if caps['has_recommendation_data']:
        parts.append('')
        parts.append('RECOMMENDATION ENGINE PERFORMANCE')
        for key in ('recommended', 'not_recommended'):
            row = cc['by_status'].get(key, {})
            if not row.get('total_bets'):
                continue
            parts.append(
                f"  {key.replace('_', ' ').title()}: {row['total_bets']} bets, "
                f"{row['win_rate']:.1f}% win, {row['roi']:.1f}% ROI, "
                f"net {_format_money(row['net_pl'])}"
            )
        for key in ('elite', 'strong', 'standard'):
            row = cc['by_tier'].get(key, {})
            if not row.get('total_bets'):
                continue
            parts.append(
                f"  Tier {key.title()}: {row['total_bets']} bets, "
                f"{row['win_rate']:.1f}% win, {row['roi']:.1f}% ROI, "
                f"net {_format_money(row['net_pl'])}"
            )
    else:
        parts.append('')
        parts.append('RECOMMENDATION DATA: not available for these bets.')

    drivers = cc['drivers']
    if drivers['best_wins']:
        parts.append('')
        parts.append('TOP RESULTS DRIVERS')
        for b in drivers['best_wins'][:3]:
            parts.append(
                f"  Best win: {_format_bet_one_liner(b)} "
                f"(+{_format_money(b.simulated_payout)})"
            )
        for b in drivers['worst_losses'][:3]:
            parts.append(
                f"  Worst loss: {_format_bet_one_liner(b)} "
                f"(-{_format_money(b.stake_amount)})"
            )
        if drivers['best_validations']:
            parts.append('')
            parts.append('VALIDATIONS (recommended/elite + +CLV win):')
            for b in drivers['best_validations'][:3]:
                parts.append(f"  {_format_bet_one_liner(b)}")
        if drivers['biggest_misses']:
            parts.append('')
            parts.append('BIGGEST MISSES (recommended/elite losses):')
            for b in drivers['biggest_misses'][:3]:
                parts.append(f"  {_format_bet_one_liner(b)}")

    sys_conf = cc['system_confidence']
    parts.append('')
    parts.append(
        f"SYSTEM CONFIDENCE SCORE: {sys_conf['score']:.1f}/100 "
        f"(based on {sys_conf['components']['total_bets']} settled bets)"
    )

    # System verdict — the deterministic "is the system working?" answer
    # the AI should be able to validate, not contradict.
    verdict = cc.get('system_verdict')
    if verdict:
        parts.append('')
        parts.append('SYSTEM VERDICT (deterministic):')
        parts.append(
            f"  Verdict: {verdict['verdict']}, "
            f"Confidence: {verdict['confidence_level']}"
        )
        if verdict['reasons']:
            parts.append('  Reasons:')
            for r in verdict['reasons']:
                parts.append(f"    - {r}")
        if verdict['warnings']:
            parts.append('  Warnings:')
            for w in verdict['warnings']:
                parts.append(f"    - {w}")

    # Edge bucket performance — answers "where is my real edge?"
    edge_buckets = cc.get('edge_buckets') or []
    populated_buckets = [b for b in edge_buckets if b['count'] > 0]
    if populated_buckets:
        parts.append('')
        parts.append('EDGE BUCKET PERFORMANCE:')
        for b in populated_buckets:
            parts.append(
                f"  {b['range']}: {b['count']} bets, "
                f"{b['win_rate']:.1f}% win, {b['roi']:+.1f}% ROI"
                + (f", CLV %+ {b['clv_positive_rate']:.0f}% (n={b['clv_sample']})"
                   if b['clv_sample'] else '')
            )

    # Decision quality breakdown — answers "did results match decisions?"
    dq = cc.get('decision_quality')
    if dq and dq['classified']:
        parts.append('')
        parts.append(f"DECISION QUALITY BREAKDOWN ({dq['classified']} classified):")
        for key, label in (
            ('perfect', 'Perfect (win + +CLV)'),
            ('lucky', 'Got Lucky (win + -CLV)'),
            ('unlucky', 'Unlucky (loss + +CLV)'),
            ('bad', 'Bad Bet (loss + -CLV)'),
            ('neutral', 'Neutral (push)'),
        ):
            count = dq['counts'].get(key, 0)
            if count:
                parts.append(f"  {label}: {count} ({dq['pcts'].get(key, 0):.0f}%)")

    return '\n'.join(parts)


_SYSTEM_PROMPT = """You are a professional sports analytics performance reviewer.
You review SIMULATED mock-bet performance — never real money.

CRITICAL RULES — you MUST follow these:

1. Use ONLY the data provided in the user message. Do NOT invent stats or
   reference outcomes/teams not in the prompt.
2. Frame everything as simulated analytics. Never give real-money betting
   advice, picks, or recommendations.
3. Sample size matters. If settled-bet count is below 30, explicitly note
   that conclusions are tentative. If below 10, lead with that caveat.
4. Use neutral, factual language: "the simulation shows", "this pattern
   suggests", "the data indicates" — never "you should bet", "guaranteed",
   or "lock".
5. Be honest about both strengths AND weaknesses.

RESPONSE STRUCTURE (under 400 words total):
1. Headline (one sentence — overall simulated result)
2. WHAT WENT WELL — 2-3 bullets
3. WHAT WENT POORLY — 2-3 bullets
4. DID THE SYSTEM MAKE GOOD DECISIONS? — one paragraph explicitly
   referencing the SYSTEM VERDICT block (STRONG/NEUTRAL/WEAK +
   confidence) AND the DECISION QUALITY BREAKDOWN. Did results match
   decision quality (lots of "Perfect" and "Unlucky" = good process,
   lots of "Got Lucky" and "Bad Bet" = bad process)?
5. WHERE IS THE STRONGEST EDGE? — one paragraph referencing the EDGE
   BUCKET PERFORMANCE block. If one bucket is clearly outperforming
   others (e.g. 6pp+ at 65% win vs 0-2pp at 45%), call it out. If no
   bucket has enough data, say so.
6. DID THE SYSTEM BEAT THE MARKET? — short paragraph on CLV %+. If no
   CLV data, say so.
7. WHAT TO WATCH NEXT — 2-3 bullets

The deterministic SYSTEM VERDICT is the source of truth on whether the
system is working. Your job is to summarize and contextualize it, NOT
to override it. If the verdict says WEAK, do not write "the system is
performing well." If the verdict says STRONG with LOW confidence, lead
with the sample-size caveat.

End with: "This summary is based on simulated data only. Past simulated
performance does not predict future outcomes."
"""


def _deterministic_fallback(cc: dict) -> str:
    """Plain-English narrative built from the analytics object only.

    Used when OpenAI is unavailable (no key, API error, low sample) so the
    panel always shows something honest. Mirrors the AI structure but with
    a strict template so the output is fully predictable.
    """
    kpis = cc['kpis']
    clv = cc['clv']
    sys_conf = cc['system_confidence']
    settled = kpis['settled_count']

    if settled == 0:
        return (
            'No settled bets to summarize yet. Place a few mock bets and '
            'check back after they resolve.'
        )

    parts = []

    sample_caveat = ''
    if settled < 10:
        sample_caveat = (
            f' Sample size is small ({settled} settled bets); these numbers '
            'are heavily affected by variance.'
        )
    elif settled < 30:
        sample_caveat = (
            f' Sample size is modest ({settled} settled bets); treat patterns '
            'as tentative.'
        )

    headline = (
        f"Across {settled} settled bets, the simulation shows a "
        f"{kpis['wins']}-{kpis['losses']}-{kpis['pushes']} record, "
        f"{kpis['roi']:.1f}% ROI, and {_format_money(kpis['net_pl'])} net P/L."
        + sample_caveat
    )
    parts.append(headline)

    parts.append('')
    parts.append('What went well:')
    if kpis['net_pl'] > 0:
        parts.append(f"- Positive net result: {_format_money(kpis['net_pl'])}")
    if kpis['win_pct'] >= 53:
        parts.append(f"- Win rate above breakeven at {kpis['win_pct']:.1f}%")
    if clv['sample_size'] and clv['positive_rate'] >= 50:
        parts.append(
            f"- Beat the closing line on {clv['positive_rate']:.0f}% of "
            f"bets with CLV captured ({clv['sample_size']} samples)"
        )
    rec_row = cc['by_status'].get('recommended', {})
    if rec_row.get('total_bets') and rec_row.get('roi', 0) > 0:
        parts.append(
            f"- Recommended bets returned {rec_row['roi']:.1f}% ROI "
            f"({rec_row['total_bets']} bets)"
        )

    parts.append('')
    parts.append('What went poorly:')
    if kpis['net_pl'] < 0:
        parts.append(f"- Negative net result: {_format_money(kpis['net_pl'])}")
    if clv['sample_size'] and clv['positive_rate'] < 50:
        parts.append(
            f"- Lost the closing line on {100 - clv['positive_rate']:.0f}% of "
            f"bets with CLV captured — the market disagreed with our pricing"
        )
    not_rec = cc['by_status'].get('not_recommended', {})
    if not_rec.get('total_bets') and not_rec.get('roi', 0) > 0:
        parts.append(
            f"- Not-Recommended bets outperformed Recommended ones — the "
            f"decision rules may need tuning"
        )

    parts.append('')
    if clv['sample_size']:
        parts.append(
            f"Did the system beat the market? CLV %+ = "
            f"{clv['positive_rate']:.0f}% across {clv['sample_size']} bets."
        )
    else:
        parts.append(
            "Did the system beat the market? Cannot tell — no closing-line "
            "data has been captured for any settled bets yet."
        )

    # Edge bucket signal — point at the strongest one if any has signal.
    edge_buckets = cc.get('edge_buckets') or []
    settled_buckets = [b for b in edge_buckets if b['count'] >= 5]
    if settled_buckets:
        best = max(settled_buckets, key=lambda b: b['roi'])
        if best['roi'] > 5:
            parts.append('')
            parts.append(
                f"Where is the edge? The {best['range']} edge bucket "
                f"({best['count']} bets) returned {best['roi']:+.1f}% ROI — "
                "the strongest signal in the data."
            )

    verdict = cc.get('system_verdict')
    if verdict:
        parts.append('')
        parts.append(
            f"System Verdict: {verdict['verdict']} "
            f"(confidence: {verdict['confidence_level']})"
        )

    parts.append('')
    parts.append(
        f"System Confidence Score: {sys_conf['score']:.1f}/100. "
        "Past simulated performance does not predict future outcomes."
    )

    return '\n'.join(parts)


def generate_mockbet_analytics_summary(cc: dict) -> dict:
    """AI postgame summary from the command-center analytics object.

    Returns:
        {'content': str, 'source': 'openai'|'deterministic', 'meta': {...}, 'error': str|None}

    Falls back to a deterministic narrative when:
      - No settled bets at all
      - OPENAI_API_KEY missing
      - OpenAI call raises
    """
    settled = cc['kpis']['settled_count']
    if settled == 0:
        return {
            'content': _deterministic_fallback(cc),
            'source': 'deterministic',
            'meta': {'reason': 'no_settled_bets'},
            'error': None,
        }

    api_key = settings.OPENAI_API_KEY
    if not api_key:
        return {
            'content': _deterministic_fallback(cc),
            'source': 'deterministic',
            'meta': {'reason': 'no_api_key'},
            'error': None,
        }

    try:
        from apps.core.models import SiteConfig
        config = SiteConfig.get()
        temperature = config.ai_temperature
        max_tokens = config.ai_max_tokens
    except Exception:
        temperature = 0.0
        max_tokens = 800

    user_prompt = _build_user_prompt(cc)
    model = settings.OPENAI_MODEL
    start = time.time()

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': _SYSTEM_PROMPT},
                {'role': 'user', 'content': user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = response.choices[0].message.content
        elapsed = time.time() - start
        logger.info(
            'ai_summary generated model=%s settled=%d response_len=%d elapsed=%.2fs',
            model, settled, len(content), elapsed,
        )
        return {
            'content': content,
            'source': 'openai',
            'meta': {
                'model': model,
                'response_length': len(content),
                'elapsed_seconds': round(elapsed, 2),
            },
            'error': None,
        }
    except Exception as e:
        elapsed = time.time() - start
        logger.error('ai_summary failed err=%s elapsed=%.2fs', e, elapsed)
        return {
            'content': _deterministic_fallback(cc),
            'source': 'deterministic',
            'meta': {'reason': 'openai_error', 'elapsed_seconds': round(elapsed, 2)},
            'error': str(e),
        }
