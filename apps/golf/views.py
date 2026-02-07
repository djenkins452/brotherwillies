from django.shortcuts import render
from .models import GolfEvent


def golf_hub(request):
    events = GolfEvent.objects.all()[:10]
    return render(request, 'golf/hub.html', {
        'events': events,
        'help_key': 'golf',
        'nav_active': 'golf',
    })
