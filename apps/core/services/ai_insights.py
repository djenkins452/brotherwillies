"""
AI Insights service — generates factual game analysis using OpenAI.

Builds a structured prompt from game data, calls OpenAI Chat Completions,
and returns formatted analysis. The AI explains model-vs-market differences
using ONLY the facts provided. No speculation, no invented data.
"""
import hashlib
import json
import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)


# ── Persona system prompts ──────────────────────────────────────────────

PERSONA_PROMPTS = {
    'analyst': (
        "You are a professional sports data analyst. Your tone is neutral, factual, "
        "and precise. You analyze data like a quantitative researcher. No slang, no "
        "hype. Every statement is grounded in the numbers provided."
    ),
    'new_york_bookie': (
        "You are a sharp, street-smart New York bookie who has been in the game for "
        "30 years. You are blunt, confident, and informal. You call it like you see it. "
        "Profanity is allowed. You don't sugarcoat. You talk in short, punchy sentences."
    ),
    'southern_commentator': (
        "You are a calm, folksy Southern sports commentator — think SEC Network on a "
        "Saturday afternoon. You are confident but warm. You use occasional colloquial "
        "expressions. Mild profanity is rare but acceptable. You paint a picture with "
        "your words while staying grounded in the data."
    ),
    'ex_player': (
        "You are a former college player who went pro. You bring a locker-room perspective "
        "— direct, experiential, and confident. You reference what it feels like to play "
        "in certain situations. Profanity is allowed but controlled. You keep it real."
    ),
}


# ── Prompt construction ─────────────────────────────────────────────────

def _build_system_prompt(persona):
    """Build the system prompt with persona + strict rules."""
    persona_text = PERSONA_PROMPTS.get(persona, PERSONA_PROMPTS['analyst'])
    return f"""{persona_text}

CRITICAL RULES — you MUST follow these:
- ONLY use facts provided in the user message. Do NOT invent players, stats, or trends.
- If data is missing, say so explicitly.
- Do NOT speculate or guess.
- Do NOT give betting advice, picks, "best bets", or guarantees.
- Use language like "analyzed", "modeled", "suggests" — never "guaranteed" or "lock".
- If the house model and market are closely aligned, state that clearly.
- Keep the response concise (under 300 words).

RESPONSE STRUCTURE (follow this order):
1. One-line summary of the model-vs-market disagreement (or agreement)
2. MARKET VS HOUSE — state the numbers
3. KEY DRIVERS — bullet list of fact-based factors, ordered by impact
4. INJURY IMPACT — if injuries are provided, explain their effect; if none, skip
5. LINE MOVEMENT — if movement data is provided, explain; if none, skip
6. WHAT WOULD CHANGE THIS VIEW — 1-2 conditions that would shift the analysis
7. CONFIDENCE & LIMITATIONS — data quality and any missing data"""


def _build_user_prompt(context):
    """Build the user message from structured game data."""
    game = context['game_context']
    market = context['market_data']
    house = context['house_model']
    injuries = context.get('injuries', [])
    line_movement = context.get('line_movement')
    confidence = context.get('data_confidence', {})
    user_model = context.get('user_model')

    parts = []

    parts.append(f"GAME: {game['away_team']} @ {game['home_team']} ({game['sport'].upper()})")
    parts.append(f"Game time: {game['game_time']}")
    if game.get('neutral_site'):
        parts.append("Venue: Neutral site")
    if game.get('status'):
        parts.append(f"Status: {game['status']}")

    parts.append("")
    parts.append("MARKET DATA:")
    parts.append(f"  Market home win probability: {market['market_home_win_prob']:.1f}%")
    parts.append(f"  Market away win probability: {market['market_away_win_prob']:.1f}%")
    if market.get('spread') is not None:
        parts.append(f"  Spread: {market['spread']}")
    if market.get('total') is not None:
        parts.append(f"  Total: {market['total']}")
    if market.get('odds_age'):
        parts.append(f"  Odds age: {market['odds_age']}")

    parts.append("")
    parts.append("HOUSE MODEL OUTPUT:")
    parts.append(f"  House home win probability: {house['house_home_win_prob']:.1f}%")
    parts.append(f"  House away win probability: {house['house_away_win_prob']:.1f}%")
    parts.append(f"  House edge: {house['house_edge']:+.1f}%")
    parts.append(f"  Model version: {house['model_version']}")

    if user_model:
        parts.append("")
        parts.append("USER MODEL OUTPUT:")
        parts.append(f"  User home win probability: {user_model['user_home_win_prob']:.1f}%")
        parts.append(f"  User edge: {user_model['user_edge']:+.1f}%")

    parts.append("")
    parts.append("TEAM FACTORS:")
    parts.append(f"  {game['home_team']} rating: {game.get('home_rating', 'N/A')}")
    parts.append(f"  {game['away_team']} rating: {game.get('away_rating', 'N/A')}")
    parts.append(f"  {game['home_team']} conference: {game.get('home_conference', 'N/A')}")
    parts.append(f"  {game['away_team']} conference: {game.get('away_conference', 'N/A')}")

    if injuries:
        parts.append("")
        parts.append("INJURIES:")
        for inj in injuries:
            parts.append(f"  - {inj['team']}: {inj['impact_level'].upper()} impact")
            if inj.get('notes'):
                parts.append(f"    Notes: {inj['notes']}")
    else:
        parts.append("")
        parts.append("INJURIES: None reported")

    if line_movement:
        parts.append("")
        parts.append("LINE MOVEMENT:")
        parts.append(f"  Direction: {line_movement['direction']}")
        if line_movement.get('magnitude'):
            parts.append(f"  Magnitude: {line_movement['magnitude']:.1f}%")
    else:
        parts.append("")
        parts.append("LINE MOVEMENT: No significant movement detected")

    parts.append("")
    parts.append("DATA CONFIDENCE:")
    parts.append(f"  Level: {confidence.get('level', 'unknown').upper()}")
    missing = confidence.get('missing_data', [])
    if missing:
        parts.append(f"  Missing: {', '.join(missing)}")

    return "\n".join(parts)


def _build_context_from_game(game, data, sport):
    """
    Build the structured context dict from a game object and its computed data.
    Works for both CFB and CBB games.
    """
    time_field = game.kickoff if sport == 'cfb' else game.tipoff

    game_context = {
        'sport': sport,
        'home_team': game.home_team.name,
        'away_team': game.away_team.name,
        'game_time': time_field.strftime('%A, %b %d %Y - %I:%M %p') if time_field else 'TBD',
        'neutral_site': game.neutral_site,
        'status': game.status,
        'home_rating': game.home_team.rating,
        'away_rating': game.away_team.rating,
        'home_conference': game.home_team.conference.name if game.home_team.conference else 'N/A',
        'away_conference': game.away_team.conference.name if game.away_team.conference else 'N/A',
    }

    market_data = {
        'market_home_win_prob': data['market_prob'],
        'market_away_win_prob': 100.0 - data['market_prob'],
    }
    if data.get('latest_odds'):
        odds = data['latest_odds']
        market_data['spread'] = odds.spread
        market_data['total'] = odds.total
        from django.utils import timezone as tz
        age_hours = (tz.now() - odds.captured_at).total_seconds() / 3600
        if age_hours < 1:
            market_data['odds_age'] = f"{int(age_hours * 60)} minutes"
        else:
            market_data['odds_age'] = f"{age_hours:.1f} hours"

    house_model = {
        'house_home_win_prob': data['house_prob'],
        'house_away_win_prob': 100.0 - data['house_prob'],
        'house_edge': data['house_edge'],
        'model_version': data['model_version'],
    }

    user_model = None
    if data.get('user_prob') is not None:
        user_model = {
            'user_home_win_prob': data['user_prob'],
            'user_edge': data['user_edge'],
        }

    injuries = []
    for inj in data.get('injuries', []):
        injuries.append({
            'team': inj.team.name,
            'impact_level': inj.impact_level,
            'notes': inj.notes,
        })

    line_movement = None
    if data.get('line_movement'):
        line_movement = {'direction': data['line_movement']}

    missing_data = []
    if not data.get('latest_odds'):
        missing_data.append('odds data')
    if not data.get('injuries'):
        missing_data.append('injury reports')

    data_confidence = {
        'level': data.get('confidence', 'low'),
        'missing_data': missing_data,
    }

    return {
        'game_context': game_context,
        'market_data': market_data,
        'house_model': house_model,
        'user_model': user_model,
        'injuries': injuries,
        'line_movement': line_movement,
        'data_confidence': data_confidence,
    }


# ── OpenAI call ──────────────────────────────────────────────────────────

def generate_insight(game, data, sport, persona='analyst'):
    """
    Generate an AI insight for a game.

    Args:
        game: CFB or CBB Game model instance
        data: dict from compute_game_data()
        sport: 'cfb' or 'cbb'
        persona: one of the AI_PERSONA_CHOICES keys

    Returns:
        dict with 'content' (str), 'error' (str|None), 'meta' (dict)
    """
    api_key = settings.OPENAI_API_KEY
    model = settings.OPENAI_MODEL

    if not api_key:
        return {
            'content': None,
            'error': 'AI Insights are not configured. The OPENAI_API_KEY environment variable is not set.',
            'meta': {},
        }

    context = _build_context_from_game(game, data, sport)
    system_prompt = _build_system_prompt(persona)
    user_prompt = _build_user_prompt(context)

    # Compute prompt hash for logging
    prompt_hash = hashlib.md5(
        (system_prompt + user_prompt).encode()
    ).hexdigest()[:12]

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
            temperature=0.4,
            max_tokens=600,
        )

        content = response.choices[0].message.content
        elapsed = time.time() - start_time

        logger.info(
            "AI insight generated | game=%s | sport=%s | persona=%s | model=%s | "
            "prompt_hash=%s | response_len=%d | elapsed=%.2fs",
            game.id, sport, persona, model, prompt_hash, len(content), elapsed
        )

        return {
            'content': content,
            'error': None,
            'meta': {
                'model': model,
                'prompt_hash': prompt_hash,
                'response_length': len(content),
                'elapsed_seconds': round(elapsed, 2),
            },
        }

    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(
            "AI insight failed | game=%s | sport=%s | error=%s | elapsed=%.2fs",
            game.id, sport, str(e), elapsed
        )
        return {
            'content': None,
            'error': f'Could not generate AI insight: {str(e)}',
            'meta': {
                'model': model,
                'prompt_hash': prompt_hash,
                'elapsed_seconds': round(elapsed, 2),
            },
        }
