from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages

from .access import partner_required
from .models import PartnerFeedback, FeedbackComponent
from .forms import FeedbackSubmitForm, FeedbackEditForm


@partner_required
def feedback_new(request):
    if request.method == 'POST':
        form = FeedbackSubmitForm(request.POST)
        if form.is_valid():
            fb = form.save(commit=False)
            fb.user = request.user
            fb.save()
            messages.success(request, 'Feedback submitted.')
            return redirect('feedback:console')
    else:
        form = FeedbackSubmitForm()
    return render(request, 'feedback/new.html', {
        'form': form,
        'help_key': 'feedback',
        'nav_active': 'profile',
    })


@partner_required
def feedback_console(request):
    qs = PartnerFeedback.objects.select_related('user', 'component').all()

    # Filters
    status_filter = request.GET.get('status', '')
    component_filter = request.GET.get('component', '')
    user_filter = request.GET.get('user', '')

    if status_filter:
        qs = qs.filter(status=status_filter)
    if component_filter:
        qs = qs.filter(component_id=component_filter)
    if user_filter:
        qs = qs.filter(user__username=user_filter)

    # Counts
    all_feedback = PartnerFeedback.objects.all()
    status_counts = {
        'NEW': all_feedback.filter(status='NEW').count(),
        'ACCEPTED': all_feedback.filter(status='ACCEPTED').count(),
        'READY': all_feedback.filter(status='READY').count(),
        'DISMISSED': all_feedback.filter(status='DISMISSED').count(),
    }
    total_count = all_feedback.count()

    components = FeedbackComponent.objects.filter(is_active=True)
    partner_users = all_feedback.values_list('user__username', flat=True).distinct()

    return render(request, 'feedback/console.html', {
        'feedback_list': qs,
        'status_counts': status_counts,
        'total_count': total_count,
        'components': components,
        'partner_users': partner_users,
        'current_status': status_filter,
        'current_component': component_filter,
        'current_user': user_filter,
        'status_choices': PartnerFeedback.Status.choices,
        'help_key': 'feedback',
        'nav_active': 'profile',
    })


@partner_required
def feedback_detail(request, pk):
    fb = get_object_or_404(PartnerFeedback.objects.select_related('user', 'component'), pk=pk)
    return render(request, 'feedback/detail.html', {
        'fb': fb,
        'help_key': 'feedback',
        'nav_active': 'profile',
    })


@partner_required
def feedback_update(request, pk):
    fb = get_object_or_404(PartnerFeedback, pk=pk)
    if request.method == 'POST':
        form = FeedbackEditForm(request.POST, instance=fb)
        if form.is_valid():
            form.save()
            messages.success(request, 'Feedback updated.')
            return redirect('feedback:detail', pk=fb.pk)
    else:
        form = FeedbackEditForm(instance=fb)
    return render(request, 'feedback/edit.html', {
        'form': form,
        'fb': fb,
        'help_key': 'feedback',
        'nav_active': 'profile',
    })
