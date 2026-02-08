"""Analytics computations for mock bets.

Provides KPIs, chart data, comparison metrics, variance analysis,
confidence calibration, edge analysis, and flat-bet simulation.
"""

from collections import defaultdict
from decimal import Decimal
from statistics import stdev

from django.db.models import Q, Sum, Count, Avg, F


def compute_kpis(bets):
    """Compute global KPI metrics from a queryset/list of bets."""
    all_bets = list(bets)
    settled = [b for b in all_bets if b.result != 'pending']
    pending = [b for b in all_bets if b.result == 'pending']

    total_stake = sum(b.stake_amount for b in settled) if settled else Decimal('0')
    total_return = Decimal('0')
    for b in settled:
        if b.result == 'win':
            total_return += b.stake_amount + (b.simulated_payout or Decimal('0'))
        elif b.result == 'push':
            total_return += b.stake_amount
    net_pl = total_return - total_stake

    wins = sum(1 for b in settled if b.result == 'win')
    losses = sum(1 for b in settled if b.result == 'loss')
    pushes = sum(1 for b in settled if b.result == 'push')

    win_pct = (wins / len(settled) * 100) if settled else 0
    roi = (float(net_pl) / float(total_stake) * 100) if total_stake else 0

    avg_odds = sum(b.odds_american for b in settled) / len(settled) if settled else 0
    avg_implied = sum(float(b.implied_probability) for b in settled) / len(settled) if settled else 0

    return {
        'total_bets': len(all_bets),
        'settled_count': len(settled),
        'pending_count': len(pending),
        'wins': wins,
        'losses': losses,
        'pushes': pushes,
        'total_stake': total_stake,
        'total_return': total_return,
        'net_pl': net_pl,
        'win_pct': win_pct,
        'roi': roi,
        'avg_odds': round(avg_odds),
        'avg_implied': round(avg_implied * 100, 1),
    }


def compute_chart_data(bets):
    """Compute data for all Phase 2 charts."""
    settled = sorted(
        [b for b in bets if b.result != 'pending'],
        key=lambda b: b.settled_at or b.placed_at
    )

    return {
        'cumulative_pl': _cumulative_pl(settled),
        'rolling_win_pct': _rolling_win_pct(settled, window=10),
        'roi_by_sport': _roi_by_sport(settled),
        'performance_by_confidence': _performance_by_confidence(settled),
        'odds_distribution': _odds_distribution(settled),
    }


def _cumulative_pl(settled):
    """Time-series cumulative P/L."""
    running = Decimal('0')
    data = []
    for b in settled:
        if b.result == 'win':
            running += b.simulated_payout or Decimal('0')
        elif b.result == 'loss':
            running -= b.stake_amount
        # push: no change
        date_str = (b.settled_at or b.placed_at).strftime('%Y-%m-%d')
        data.append({'date': date_str, 'pl': float(running)})
    return data


def _rolling_win_pct(settled, window=10):
    """Rolling win % over last N bets."""
    data = []
    for i in range(len(settled)):
        start = max(0, i - window + 1)
        chunk = settled[start:i + 1]
        wins = sum(1 for b in chunk if b.result == 'win')
        pct = (wins / len(chunk)) * 100
        data.append({
            'bet_num': i + 1,
            'win_pct': round(pct, 1),
        })
    return data


def _roi_by_sport(settled):
    """ROI broken out by sport."""
    sports = defaultdict(lambda: {'stake': Decimal('0'), 'return': Decimal('0'), 'count': 0})
    for b in settled:
        sports[b.sport]['count'] += 1
        sports[b.sport]['stake'] += b.stake_amount
        if b.result == 'win':
            sports[b.sport]['return'] += b.stake_amount + (b.simulated_payout or Decimal('0'))
        elif b.result == 'push':
            sports[b.sport]['return'] += b.stake_amount

    result = {}
    for sport, d in sports.items():
        net = d['return'] - d['stake']
        roi = (float(net) / float(d['stake']) * 100) if d['stake'] else 0
        result[sport] = {'roi': round(roi, 1), 'count': d['count'], 'net': float(net)}
    return result


def _performance_by_confidence(settled):
    """Win rate and ROI by confidence level."""
    levels = defaultdict(lambda: {'wins': 0, 'total': 0, 'stake': Decimal('0'), 'return': Decimal('0')})
    for b in settled:
        levels[b.confidence_level]['total'] += 1
        levels[b.confidence_level]['stake'] += b.stake_amount
        if b.result == 'win':
            levels[b.confidence_level]['wins'] += 1
            levels[b.confidence_level]['return'] += b.stake_amount + (b.simulated_payout or Decimal('0'))
        elif b.result == 'push':
            levels[b.confidence_level]['return'] += b.stake_amount

    result = {}
    for level in ('low', 'medium', 'high'):
        d = levels[level]
        if d['total'] > 0:
            net = d['return'] - d['stake']
            result[level] = {
                'count': d['total'],
                'win_pct': round(d['wins'] / d['total'] * 100, 1),
                'roi': round(float(net) / float(d['stake']) * 100, 1) if d['stake'] else 0,
            }
    return result


def _odds_distribution(settled):
    """Distribution of odds vs outcomes for chart."""
    data = []
    for b in settled:
        data.append({
            'odds': b.odds_american,
            'result': b.result,
            'implied_prob': float(b.implied_probability) * 100,
        })
    return data


def compute_comparison(bets):
    """House vs User head-to-head comparison."""
    house_bets = [b for b in bets if b.model_source == 'house' and b.result != 'pending']
    user_bets = [b for b in bets if b.model_source == 'user' and b.result != 'pending']

    def _stats(settled):
        if not settled:
            return None
        stake = sum(b.stake_amount for b in settled)
        ret = Decimal('0')
        for b in settled:
            if b.result == 'win':
                ret += b.stake_amount + (b.simulated_payout or Decimal('0'))
            elif b.result == 'push':
                ret += b.stake_amount
        net = ret - stake
        wins = sum(1 for b in settled if b.result == 'win')
        avg_odds = sum(b.odds_american for b in settled) / len(settled)
        avg_implied = sum(float(b.implied_probability) for b in settled) / len(settled)

        returns = []
        for b in settled:
            if b.result == 'win':
                returns.append(float(b.simulated_payout or 0))
            elif b.result == 'loss':
                returns.append(-float(b.stake_amount))
            else:
                returns.append(0.0)

        volatility = stdev(returns) if len(returns) > 1 else 0

        return {
            'count': len(settled),
            'wins': wins,
            'win_pct': round(wins / len(settled) * 100, 1),
            'roi': round(float(net) / float(stake) * 100, 1) if stake else 0,
            'avg_odds': round(avg_odds),
            'avg_implied': round(avg_implied * 100, 1),
            'volatility': round(volatility, 2),
            'net_pl': float(net),
        }

    return {
        'house': _stats(house_bets),
        'user': _stats(user_bets),
    }


def compute_confidence_calibration(bets):
    """Analyze win % by confidence level — expected vs actual."""
    settled = [b for b in bets if b.result != 'pending']
    levels = defaultdict(lambda: {'total': 0, 'wins': 0, 'avg_implied': []})

    for b in settled:
        levels[b.confidence_level]['total'] += 1
        levels[b.confidence_level]['avg_implied'].append(float(b.implied_probability))
        if b.result == 'win':
            levels[b.confidence_level]['wins'] += 1

    result = {}
    for level in ('low', 'medium', 'high'):
        d = levels[level]
        if d['total'] > 0:
            expected = sum(d['avg_implied']) / len(d['avg_implied']) * 100
            actual = d['wins'] / d['total'] * 100
            result[level] = {
                'count': d['total'],
                'expected_win_pct': round(expected, 1),
                'actual_win_pct': round(actual, 1),
                'diff': round(actual - expected, 1),
            }
    return result


def compute_edge_analysis(bets):
    """Analyze expected edge vs actual outcomes."""
    settled = [b for b in bets if b.result != 'pending' and b.expected_edge is not None]
    if not settled:
        return None

    # Bucket by edge ranges
    buckets = {
        'negative': {'range': '< 0%', 'bets': []},
        'small': {'range': '0-3%', 'bets': []},
        'medium': {'range': '3-7%', 'bets': []},
        'large': {'range': '7%+', 'bets': []},
    }

    for b in settled:
        edge = float(b.expected_edge)
        if edge < 0:
            buckets['negative']['bets'].append(b)
        elif edge < 3:
            buckets['small']['bets'].append(b)
        elif edge < 7:
            buckets['medium']['bets'].append(b)
        else:
            buckets['large']['bets'].append(b)

    result = {}
    for key, bucket in buckets.items():
        blist = bucket['bets']
        if blist:
            wins = sum(1 for b in blist if b.result == 'win')
            stake = sum(b.stake_amount for b in blist)
            ret = Decimal('0')
            for b in blist:
                if b.result == 'win':
                    ret += b.stake_amount + (b.simulated_payout or Decimal('0'))
                elif b.result == 'push':
                    ret += b.stake_amount
            net = ret - stake
            result[key] = {
                'range': bucket['range'],
                'count': len(blist),
                'win_pct': round(wins / len(blist) * 100, 1),
                'roi': round(float(net) / float(stake) * 100, 1) if stake else 0,
            }
    return result


def compute_flat_bet_simulation(bets, flat_stake):
    """Recalculate P/L and ROI using a hypothetical flat stake amount."""
    settled = sorted(
        [b for b in bets if b.result != 'pending'],
        key=lambda b: b.settled_at or b.placed_at
    )
    if not settled:
        return None

    flat_stake = Decimal(str(flat_stake))
    total_stake = flat_stake * len(settled)
    total_return = Decimal('0')
    cumulative = []
    running = Decimal('0')
    max_drawdown = Decimal('0')
    peak = Decimal('0')

    for b in settled:
        if b.result == 'win':
            if b.odds_american > 0:
                payout = flat_stake * (Decimal(b.odds_american) / Decimal('100'))
            else:
                payout = flat_stake * (Decimal('100') / Decimal(abs(b.odds_american)))
            total_return += flat_stake + payout
            running += payout
        elif b.result == 'push':
            total_return += flat_stake
        else:
            running -= flat_stake

        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_drawdown:
            max_drawdown = dd

        cumulative.append({
            'date': (b.settled_at or b.placed_at).strftime('%Y-%m-%d'),
            'pl': float(running),
        })

    net = total_return - total_stake
    roi = (float(net) / float(total_stake) * 100) if total_stake else 0

    return {
        'flat_stake': float(flat_stake),
        'total_bets': len(settled),
        'total_stake': float(total_stake),
        'total_return': float(total_return),
        'net_pl': float(net),
        'roi': round(roi, 1),
        'max_drawdown': float(max_drawdown),
        'cumulative_pl': cumulative,
    }


def compute_variance_stats(bets):
    """Compute variance and stress testing metrics."""
    settled = sorted(
        [b for b in bets if b.result != 'pending'],
        key=lambda b: b.settled_at or b.placed_at
    )
    if len(settled) < 2:
        return None

    # Build result sequence
    results = []
    for b in settled:
        if b.result == 'win':
            results.append(float(b.simulated_payout or 0))
        elif b.result == 'loss':
            results.append(-float(b.stake_amount))
        else:
            results.append(0.0)

    # Longest losing streak
    max_losing = 0
    current_losing = 0
    max_winning = 0
    current_winning = 0
    for b in settled:
        if b.result == 'loss':
            current_losing += 1
            max_losing = max(max_losing, current_losing)
            current_winning = 0
        elif b.result == 'win':
            current_winning += 1
            max_winning = max(max_winning, current_winning)
            current_losing = 0
        else:
            current_losing = 0
            current_winning = 0

    # Maximum drawdown
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for r in results:
        running += r
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_drawdown:
            max_drawdown = dd

    # Best and worst N-bet stretches
    n = min(10, len(results))
    best_stretch = float('-inf')
    worst_stretch = float('inf')
    best_start = worst_start = 0
    for i in range(len(results) - n + 1):
        chunk_sum = sum(results[i:i + n])
        if chunk_sum > best_stretch:
            best_stretch = chunk_sum
            best_start = i
        if chunk_sum < worst_stretch:
            worst_stretch = chunk_sum
            worst_start = i

    return {
        'longest_losing_streak': max_losing,
        'longest_winning_streak': max_winning,
        'max_drawdown': round(max_drawdown, 2),
        'worst_stretch': {
            'value': round(worst_stretch, 2),
            'window': n,
            'start': worst_start + 1,
        },
        'best_stretch': {
            'value': round(best_stretch, 2),
            'window': n,
            'start': best_start + 1,
        },
        'volatility': round(stdev(results), 2) if len(results) > 1 else 0,
    }
