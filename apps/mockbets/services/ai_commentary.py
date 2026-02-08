"""
AI Performance Commentary for mock bet analytics.

Generates a structured performance analysis from aggregated mock bet data
using OpenAI Chat Completions. Respects the user's AI persona preference.
"""
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)


# ── Persona system prompts (mirrored from core.services.ai_insights) ───

PERSONA_PROMPTS = {
    'analyst': (
        "You are a professional sports analytics performance reviewer. Your tone is "
        "neutral, factual, and precise. You review simulated betting performance like "
        "a quantitative portfolio analyst. No slang, no hype."
    ),
    'new_york_bookie': (
        "You are a sharp, street-smart New York bookie reviewing someone's simulated "
        "betting track record. You are blunt, confident, and informal. You call it "
        "like you see it in short, punchy sentences."
    ),
    'southern_commentator': (
        "You are a folksy Southern sports commentator reviewing someone's simulated "
        "betting decisions. Warm but honest. Occasional colloquial expressions. "
        "You paint a picture with your words while staying grounded in the data."
    ),
    'ex_player': (
        "You are a former college player reviewing someone's simulated betting "
        "performance. Direct, experiential, confident. You keep it real and "
        "reference the feeling of being in pressure situations."
    ),
}


def _build_system_prompt(persona):
    """Build system prompt with persona + strict rules."""
    persona_text = PERSONA_PROMPTS.get(persona, PERSONA_PROMPTS['analyst'])
    return f"""{persona_text}

CRITICAL RULES — you MUST follow these:

1. You are reviewing SIMULATED betting performance data. This is NOT real money.
   Always frame your analysis as reviewing a simulation/analytics exercise.

2. Use ONLY the data provided in the user message. Do NOT invent stats.

3. Do NOT give betting advice, picks, or recommendations.

4. Use language like "the simulation shows", "this pattern suggests", "your analysis
   indicates" — never "you should bet", "guaranteed", or "lock".

5. Be honest about both strengths and weaknesses in the data.

6. Keep the response concise (under 300 words).

RESPONSE STRUCTURE:
1. One-line overall assessment of simulated performance
2. STRENGTHS — 2-3 bullet points on what's working well
3. AREAS TO WATCH — 2-3 bullet points on patterns that need attention
4. CALIBRATION CHECK — brief note on confidence calibration (if data provided)
5. BOTTOM LINE — one sentence summary

End with: "This analysis is based on simulated data only. Past simulated performance
does not predict future outcomes."
"""


def _build_user_prompt(kpis, comparison, calibration, edge, variance):
    """Build structured data prompt from analytics results."""
    parts = []

    parts.append("MOCK BET SIMULATION PERFORMANCE DATA")
    parts.append("=" * 40)

    parts.append("")
    parts.append("OVERALL KPIs:")
    parts.append(f"  Total bets: {kpis['total_bets']}")
    parts.append(f"  Settled: {kpis['settled_count']}")
    parts.append(f"  Pending: {kpis['pending_count']}")
    parts.append(f"  Record: {kpis['wins']}W - {kpis['losses']}L - {kpis['pushes']}P")
    parts.append(f"  Win rate: {kpis['win_pct']:.1f}%")
    parts.append(f"  Simulated ROI: {kpis['roi']:.1f}%")
    parts.append(f"  Simulated net P/L: ${float(kpis['net_pl']):.2f}")
    parts.append(f"  Average odds: {kpis['avg_odds']:+d}")
    parts.append(f"  Average implied probability: {kpis['avg_implied']}%")

    if comparison and comparison.get('house') and comparison.get('user'):
        parts.append("")
        parts.append("HOUSE VS USER MODEL COMPARISON:")
        h, u = comparison['house'], comparison['user']
        parts.append(f"  House model: {h['count']} bets, {h['win_pct']}% win rate, {h['roi']}% ROI")
        parts.append(f"  User model:  {u['count']} bets, {u['win_pct']}% win rate, {u['roi']}% ROI")

    if calibration:
        parts.append("")
        parts.append("CONFIDENCE CALIBRATION:")
        for level in ('low', 'medium', 'high'):
            if level in calibration:
                d = calibration[level]
                parts.append(
                    f"  {level.capitalize()}: {d['count']} bets, "
                    f"expected {d['expected_win_pct']}%, actual {d['actual_win_pct']}%, "
                    f"diff {d['diff']:+.1f}%"
                )

    if edge:
        parts.append("")
        parts.append("EDGE ANALYSIS:")
        for key, d in edge.items():
            parts.append(f"  {d['range']}: {d['count']} bets, {d['win_pct']}% win rate, {d['roi']}% ROI")

    if variance:
        parts.append("")
        parts.append("VARIANCE METRICS:")
        parts.append(f"  Longest losing streak: {variance['longest_losing_streak']}")
        parts.append(f"  Longest winning streak: {variance['longest_winning_streak']}")
        parts.append(f"  Max drawdown: ${variance['max_drawdown']}")
        parts.append(f"  Volatility (std dev): {variance['volatility']}")
        parts.append(f"  Best {variance['best_stretch']['window']}-bet stretch: ${variance['best_stretch']['value']}")
        parts.append(f"  Worst {variance['worst_stretch']['window']}-bet stretch: ${variance['worst_stretch']['value']}")

    return "\n".join(parts)


def generate_commentary(kpis, comparison, calibration, edge, variance, persona='analyst'):
    """
    Generate AI performance commentary from analytics data.

    Returns:
        dict with 'content' (str), 'error' (str|None), 'meta' (dict)
    """
    if kpis['settled_count'] < 5:
        return {
            'content': None,
            'error': 'Need at least 5 settled bets for meaningful AI commentary.',
            'meta': {},
        }

    api_key = settings.OPENAI_API_KEY
    model = settings.OPENAI_MODEL

    if not api_key:
        return {
            'content': None,
            'error': 'AI Commentary is not configured. The OPENAI_API_KEY environment variable is not set.',
            'meta': {},
        }

    try:
        from apps.core.models import SiteConfig
        config = SiteConfig.get()
        temperature = config.ai_temperature
        max_tokens = config.ai_max_tokens
    except Exception:
        temperature = 0.0
        max_tokens = 800

    system_prompt = _build_system_prompt(persona)
    user_prompt = _build_user_prompt(kpis, comparison, calibration, edge, variance)

    start_time = time.time()

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )

        content = response.choices[0].message.content
        elapsed = time.time() - start_time

        logger.info(
            "AI commentary generated | persona=%s | model=%s | "
            "response_len=%d | elapsed=%.2fs",
            persona, model, len(content), elapsed
        )

        return {
            'content': content,
            'error': None,
            'meta': {
                'model': model,
                'persona': persona,
                'response_length': len(content),
                'elapsed_seconds': round(elapsed, 2),
            },
        }

    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(
            "AI commentary failed | persona=%s | error=%s | elapsed=%.2fs",
            persona, str(e), elapsed
        )
        return {
            'content': None,
            'error': f'Could not generate AI commentary: {str(e)}',
            'meta': {
                'model': model,
                'elapsed_seconds': round(elapsed, 2),
            },
        }
