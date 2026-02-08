from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from .models import GolfEvent, Golfer


def golf_hub(request):
    events = GolfEvent.objects.all()[:10]
    return render(request, 'golf/hub.html', {
        'events': events,
        'help_key': 'golf',
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
