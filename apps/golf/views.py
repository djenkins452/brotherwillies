from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Subquery, OuterRef
from django.utils import timezone
from .models import GolfEvent, Golfer, GolfOddsSnapshot


def golf_hub(request):
    events = GolfEvent.objects.all()[:10]
    return render(request, 'golf/hub.html', {
        'events': events,
        'help_key': 'golf',
        'nav_active': 'golf',
    })


def event_detail(request, slug):
    event = get_object_or_404(GolfEvent, slug=slug)

    # Get the latest odds snapshot per golfer for this event
    latest_snap_ids = (
        GolfOddsSnapshot.objects.filter(event=event, golfer=OuterRef('golfer'))
        .order_by('-captured_at')
        .values('id')[:1]
    )
    latest_odds = (
        GolfOddsSnapshot.objects.filter(event=event, id__in=Subquery(latest_snap_ids))
        .select_related('golfer')
        .order_by('outright_odds')  # favorites first (lower/more negative = bigger favorite)
    )

    # Build golfer odds list
    golfer_odds = []
    golfers_with_odds = set()
    for snap in latest_odds:
        golfer_odds.append({
            'golfer': snap.golfer,
            'outright_odds': snap.outright_odds,
            'implied_prob': snap.implied_prob,
        })
        golfers_with_odds.add(snap.golfer_id)

    # Also include golfers in the field (from GolfRound) who don't have odds
    field_golfer_ids = (
        event.rounds.values_list('golfer_id', flat=True).distinct()
    )
    field_only = (
        Golfer.objects.filter(id__in=field_golfer_ids)
        .exclude(id__in=golfers_with_odds)
        .order_by('last_name', 'first_name')
    )
    for golfer in field_only:
        golfer_odds.append({
            'golfer': golfer,
            'outright_odds': None,
            'implied_prob': None,
        })

    # Check if event is still open for betting (end_date hasn't passed)
    event_open = event.end_date >= timezone.now().date()

    return render(request, 'golf/event_detail.html', {
        'event': event,
        'golfer_odds': golfer_odds,
        'event_open': event_open,
        'help_key': 'golf_event',
        'nav_active': 'golf',
    })


@login_required
def golfer_search(request):
    """AJAX endpoint: search golfers by name fragment. Returns JSON list."""
    q = request.GET.get('q', '').strip()
    if len(q) < 2:
        return JsonResponse([], safe=False)

    results = Golfer.objects.filter(
        Q(name__icontains=q) |
        Q(first_name__icontains=q) |
        Q(last_name__icontains=q)
    ).order_by('last_name', 'first_name')[:15]

    data = [{'id': g.id, 'name': g.name} for g in results]
    return JsonResponse(data, safe=False)
