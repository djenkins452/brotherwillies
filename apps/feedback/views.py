from django.shortcuts import render, get_object_or_404, redirect
from django.contrib import messages
from django.views.decorators.http import require_POST

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
    show_archived = request.GET.get('archived', '') == '1'
    qs = PartnerFeedback.objects.select_related('user', 'component').all()
    if not show_archived:
        qs = qs.filter(is_archived=False)

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
        'show_archived': show_archived,
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


@require_POST
@partner_required
def feedback_quick_status(request, pk):
    fb = get_object_or_404(PartnerFeedback, pk=pk)
    new_status = request.POST.get('status', '')
    valid_statuses = {c[0] for c in PartnerFeedback.Status.choices}

    if new_status not in valid_statuses:
        messages.error(request, 'Invalid status.')
        return redirect('feedback:console')

    # READY and DISMISSED require reviewer notes — redirect to edit page
    if new_status in (PartnerFeedback.Status.READY, PartnerFeedback.Status.DISMISSED):
        return redirect(f'/feedback/console/{fb.pk}/update/?status={new_status}')

    fb.status = new_status
    fb.save(update_fields=['status', 'updated_at'])
    messages.success(request, f'Status changed to {fb.get_status_display()}.')

    # Preserve current filters when redirecting back
    query = request.POST.get('return_query', '')
    url = '/feedback/console/'
    if query:
        url += f'?{query}'
    return redirect(url)


@require_POST
@partner_required
def feedback_archive(request, pk):
    fb = get_object_or_404(PartnerFeedback, pk=pk)
    fb.is_archived = True
    fb.save(update_fields=['is_archived', 'updated_at'])
    messages.success(request, f'"{fb.title}" archived.')

    query = request.POST.get('return_query', '')
    url = '/feedback/console/'
    if query:
        url += f'?{query}'
    return redirect(url)


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
        # Pre-select status if passed via query param (from quick-status redirect)
        preset_status = request.GET.get('status', '')
        if preset_status and preset_status in {c[0] for c in PartnerFeedback.Status.choices}:
            form.initial['status'] = preset_status
    return render(request, 'feedback/edit.html', {
        'form': form,
        'fb': fb,
        'help_key': 'feedback',
        'nav_active': 'profile',
    })
