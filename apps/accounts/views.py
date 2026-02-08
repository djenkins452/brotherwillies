from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from datetime import timedelta
from .forms import RegisterForm, PersonalInfoForm, PreferencesForm, ModelConfigForm, PresetForm
from .models import UserProfile, UserModelConfig, ModelPreset, UserSubscription, user_has_feature
from .timezone_lookup import zip_to_timezone
from apps.analytics.models import UserGameInteraction, ModelResultSnapshot
from apps.cfb.models import Game


def register_view(request):
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, 'Account created successfully.')
            return redirect('value_board')
    else:
        form = RegisterForm()
    return render(request, 'accounts/register.html', {
        'form': form,
        'help_key': 'profile',
        'nav_active': 'profile',
    })


def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username', '')
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            next_url = request.GET.get('next', '/value/')
            return redirect(next_url)
        else:
            messages.error(request, 'Invalid username or password.')
    return render(request, 'accounts/login.html', {
        'help_key': 'profile',
        'nav_active': 'profile',
    })


def logout_view(request):
    logout(request)
    return redirect('/')


@login_required
def profile_view(request):
    profile = request.user.profile
    if request.method == 'POST':
        form = PersonalInfoForm(request.POST, request.FILES)
        if form.is_valid():
            request.user.first_name = form.cleaned_data['first_name']
            request.user.last_name = form.cleaned_data['last_name']
            request.user.email = form.cleaned_data['email']
            request.user.save()
            if form.cleaned_data.get('profile_picture'):
                profile.profile_picture = form.cleaned_data['profile_picture']
                profile.save()
            messages.success(request, 'Profile updated.')
            return redirect('profile')
    else:
        form = PersonalInfoForm(initial={
            'first_name': request.user.first_name,
            'last_name': request.user.last_name,
            'email': request.user.email,
        })

    return render(request, 'accounts/profile.html', {
        'form': form,
        'profile': profile,
        'help_key': 'profile',
        'nav_active': 'profile',
    })


@login_required
def preferences_view(request):
    profile = request.user.profile
    if request.method == 'POST':
        form = PreferencesForm(request.POST, instance=profile)
        if form.is_valid():
            pref = form.save(commit=False)
            zip_code = form.cleaned_data.get('zip_code', '')
            if zip_code:
                resolved_tz = zip_to_timezone(zip_code)
                if resolved_tz:
                    pref.timezone = resolved_tz
                else:
                    pref.timezone = ''
                    messages.warning(request, 'Could not determine timezone for that zip code.')
            else:
                pref.timezone = ''
            pref.save()
            messages.success(request, 'Preferences saved.')
            return redirect('preferences')
    else:
        form = PreferencesForm(instance=profile)

    return render(request, 'accounts/preferences.html', {
        'form': form,
        'profile': profile,
        'help_key': 'preferences',
        'nav_active': 'profile',
    })


@login_required
def my_model_view(request):
    config = UserModelConfig.get_or_create_for_user(request.user)
    if request.method == 'POST':
        if 'reset' in request.POST:
            config.rating_weight = 1.0
            config.hfa_weight = 1.0
            config.injury_weight = 1.0
            config.recent_form_weight = 1.0
            config.conference_weight = 1.0
            config.save()
            messages.success(request, 'Model reset to house defaults.')
            return redirect('my_model')
        form = ModelConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, 'Model weights saved.')
            return redirect('my_model')
    else:
        form = ModelConfigForm(instance=config)

    return render(request, 'accounts/my_model.html', {
        'form': form,
        'config': config,
        'help_key': 'my_model',
        'nav_active': 'profile',
    })


@login_required
def presets_view(request):
    presets = ModelPreset.objects.filter(user=request.user)
    can_create = user_has_feature(request.user, 'multiple_presets') or presets.count() < 1

    if request.method == 'POST' and can_create:
        form = PresetForm(request.POST)
        if form.is_valid():
            preset = form.save(commit=False)
            preset.user = request.user
            preset.save()
            messages.success(request, f'Preset "{preset.name}" saved.')
            return redirect('presets')
    else:
        # Pre-fill from current config
        config = UserModelConfig.get_or_create_for_user(request.user)
        form = PresetForm(initial={
            'rating_weight': config.rating_weight,
            'hfa_weight': config.hfa_weight,
            'injury_weight': config.injury_weight,
            'recent_form_weight': config.recent_form_weight,
            'conference_weight': config.conference_weight,
        })

    return render(request, 'accounts/presets.html', {
        'presets': presets,
        'form': form,
        'can_create': can_create,
        'help_key': 'my_model',
        'nav_active': 'profile',
    })


@login_required
def load_preset(request, preset_id):
    preset = get_object_or_404(ModelPreset, id=preset_id, user=request.user)
    config = UserModelConfig.get_or_create_for_user(request.user)
    config.rating_weight = preset.rating_weight
    config.hfa_weight = preset.hfa_weight
    config.injury_weight = preset.injury_weight
    config.recent_form_weight = preset.recent_form_weight
    config.conference_weight = preset.conference_weight
    config.save()
    messages.success(request, f'Loaded preset "{preset.name}".')
    return redirect('my_model')


@login_required
def delete_preset(request, preset_id):
    preset = get_object_or_404(ModelPreset, id=preset_id, user=request.user)
    preset.delete()
    messages.success(request, 'Preset deleted.')
    return redirect('presets')


@login_required
def my_stats_view(request):
    period = request.GET.get('period', '30')
    now = timezone.now()
    if period == '7':
        since = now - timedelta(days=7)
    elif period == 'season':
        since = now - timedelta(days=180)
    else:
        since = now - timedelta(days=30)
        period = '30'

    interactions = UserGameInteraction.objects.filter(
        user=request.user, created_at__gte=since
    )
    total_analyzed = interactions.values('game').distinct().count()

    # Favorite team games
    fav_team_id = None
    try:
        fav_team_id = request.user.profile.favorite_team_id
    except Exception:
        pass

    fav_analyzed = 0
    if fav_team_id:
        fav_analyzed = interactions.filter(
            models.Q(game__home_team_id=fav_team_id) | models.Q(game__away_team_id=fav_team_id)
        ).values('game').distinct().count()

    # Get snapshots for stats
    snapshots = ModelResultSnapshot.objects.filter(
        captured_at__gte=since,
        game__in=interactions.values('game')
    )

    avg_house_edge = 0
    avg_user_edge = 0
    agreement_count = 0
    total_snaps = 0
    confidence_dist = {'low': 0, 'med': 0, 'high': 0}

    for snap in snapshots:
        h_edge = snap.house_prob - snap.market_prob
        avg_house_edge += h_edge
        if snap.user_prob is not None:
            u_edge = snap.user_prob - snap.market_prob
            avg_user_edge += u_edge
            # Agreement: both positive or both negative edge
            if (h_edge > 0) == (u_edge > 0):
                agreement_count += 1
        confidence_dist[snap.data_confidence] = confidence_dist.get(snap.data_confidence, 0) + 1
        total_snaps += 1

    if total_snaps > 0:
        avg_house_edge = round((avg_house_edge / total_snaps) * 100, 1)
        avg_user_edge = round((avg_user_edge / total_snaps) * 100, 1)
        agreement_rate = round((agreement_count / total_snaps) * 100, 0)
    else:
        avg_house_edge = 0
        avg_user_edge = 0
        agreement_rate = 0

    # Recent history
    recent_interactions = interactions.select_related('game', 'game__home_team', 'game__away_team').order_by('-created_at')[:20]

    return render(request, 'accounts/my_stats.html', {
        'period': period,
        'total_analyzed': total_analyzed,
        'fav_analyzed': fav_analyzed,
        'avg_house_edge': avg_house_edge,
        'avg_user_edge': avg_user_edge,
        'agreement_rate': agreement_rate,
        'confidence_dist': confidence_dist,
        'recent_interactions': recent_interactions,
        'help_key': 'my_stats',
        'nav_active': 'profile',
    })


@login_required
def performance_view(request):
    snapshots = ModelResultSnapshot.objects.filter(
        final_outcome__isnull=False
    ).order_by('-captured_at')[:50]

    correct = 0
    total = 0
    brier_sum = 0
    for snap in snapshots:
        total += 1
        predicted_home = snap.house_prob > 0.5
        actual_home = snap.final_outcome
        if predicted_home == actual_home:
            correct += 1
        brier_sum += (snap.house_prob - (1.0 if actual_home else 0.0)) ** 2

    accuracy = round((correct / total) * 100, 1) if total > 0 else 0
    brier = round(brier_sum / total, 3) if total > 0 else 0

    return render(request, 'accounts/performance.html', {
        'snapshots': snapshots,
        'accuracy': accuracy,
        'brier_score': brier,
        'total_games': total,
        'help_key': 'performance',
        'nav_active': 'profile',
    })


from django.db import models
