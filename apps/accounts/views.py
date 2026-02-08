import base64
from io import BytesIO
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from datetime import timedelta
from PIL import Image
from .forms import RegisterForm, PersonalInfoForm, PreferencesForm, ModelConfigForm, PresetForm
from .models import UserProfile, UserModelConfig, ModelPreset, UserSubscription, user_has_feature
from .timezone_lookup import zip_to_timezone
from apps.analytics.models import UserGameInteraction, ModelResultSnapshot
from apps.cfb.models import Game


MAX_AVATAR_SIZE = 200  # px
MAX_AVATAR_BYTES = 150_000  # ~150 KB base64 limit


def _process_profile_picture(uploaded_file):
    """Resize uploaded image to 200x200 and return a base64 data URI."""
    img = Image.open(uploaded_file)
    img = img.convert('RGB')

    # Crop to square (center crop)
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))

    # Resize to max avatar size
    img = img.resize((MAX_AVATAR_SIZE, MAX_AVATAR_SIZE), Image.LANCZOS)

    # Encode as JPEG
    buf = BytesIO()
    img.save(buf, format='JPEG', quality=75, optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return f'data:image/jpeg;base64,{b64}'


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
            next_url = request.GET.get('next', '/')
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
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    if request.method == 'POST':
        form = PersonalInfoForm(request.POST, request.FILES)
        if form.is_valid():
            request.user.first_name = form.cleaned_data['first_name']
            request.user.last_name = form.cleaned_data['last_name']
            request.user.email = form.cleaned_data['email']
            request.user.save()
            if form.cleaned_data.get('profile_picture'):
                try:
                    data_uri = _process_profile_picture(form.cleaned_data['profile_picture'])
                    profile.profile_picture_data = data_uri
                    profile.save()
                except Exception:
                    messages.warning(request, 'Could not process image. Profile info was saved.')
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
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
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
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        fav_team_id = profile.favorite_team_id
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
    all_snaps = list(
        ModelResultSnapshot.objects.filter(
            final_outcome__isnull=False
        ).select_related('game', 'cbb_game').order_by('-captured_at')
    )

    cfb_snaps = [s for s in all_snaps if s.game_id is not None]
    cbb_snaps = [s for s in all_snaps if s.cbb_game_id is not None]

    now = timezone.now()
    snaps_7d = [s for s in all_snaps if s.captured_at >= now - timedelta(days=7)]
    snaps_30d = [s for s in all_snaps if s.captured_at >= now - timedelta(days=30)]

    return render(request, 'accounts/performance.html', {
        'overall': _compute_metrics(all_snaps),
        'cfb': _compute_metrics(cfb_snaps),
        'cbb': _compute_metrics(cbb_snaps),
        'last_7d': _compute_metrics(snaps_7d),
        'last_30d': _compute_metrics(snaps_30d),
        'calibration': _compute_calibration(all_snaps),
        'clv': _compute_clv(all_snaps),
        'recent_snapshots': all_snaps[:50],
        'help_key': 'performance',
        'nav_active': 'profile',
    })


def _compute_metrics(snapshots):
    correct = 0
    total = 0
    brier_sum = 0.0
    for s in snapshots:
        total += 1
        predicted_home = s.house_prob > 0.5
        if predicted_home == s.final_outcome:
            correct += 1
        brier_sum += (s.house_prob - (1.0 if s.final_outcome else 0.0)) ** 2
    return {
        'count': total,
        'accuracy': round((correct / total) * 100, 1) if total else 0,
        'brier': round(brier_sum / total, 3) if total else 0,
    }


def _compute_calibration(snapshots):
    ranges = [
        ('50-60%', 0.50, 0.60),
        ('60-70%', 0.60, 0.70),
        ('70-80%', 0.70, 0.80),
        ('80-90%', 0.80, 0.90),
        ('90-100%', 0.90, 1.01),
    ]
    buckets = {label: {'preds': [], 'actuals': []} for label, _, _ in ranges}
    for s in snapshots:
        # Use the favored-side probability for calibration
        prob = s.house_prob if s.house_prob >= 0.5 else 1.0 - s.house_prob
        actual = s.final_outcome if s.house_prob >= 0.5 else not s.final_outcome
        for label, lo, hi in ranges:
            if lo <= prob < hi:
                buckets[label]['preds'].append(prob)
                buckets[label]['actuals'].append(1 if actual else 0)
                break
    result = []
    for label, _, _ in ranges:
        b = buckets[label]
        if b['actuals']:
            avg_pred = sum(b['preds']) / len(b['preds']) * 100
            actual_rate = sum(b['actuals']) / len(b['actuals']) * 100
            result.append({
                'bucket': label,
                'count': len(b['actuals']),
                'avg_prediction': round(avg_pred, 1),
                'actual_rate': round(actual_rate, 1),
                'diff': round(actual_rate - avg_pred, 1),
            })
    return result


def _compute_clv(snapshots):
    with_closing = [s for s in snapshots if s.closing_market_prob is not None]
    if not with_closing:
        return {'count': 0, 'avg_clv': 0, 'positive_clv_pct': 0}

    clv_sum = 0.0
    positive = 0
    for s in with_closing:
        # CLV = did the market move toward the model's prediction?
        initial_edge = abs(s.house_prob - s.market_prob)
        closing_edge = abs(s.house_prob - s.closing_market_prob)
        clv = initial_edge - closing_edge  # positive = market moved toward model
        clv_sum += clv
        if clv > 0:
            positive += 1

    count = len(with_closing)
    return {
        'count': count,
        'avg_clv': round((clv_sum / count) * 100, 2),
        'positive_clv_pct': round((positive / count) * 100, 1),
    }


@login_required
def user_guide_view(request):
    return render(request, 'accounts/user_guide.html', {
        'help_key': 'user_guide',
        'nav_active': 'profile',
    })


def whats_new_view(request):
    return render(request, 'accounts/whats_new.html', {
        'help_key': 'whats_new',
        'nav_active': 'profile',
    })


from django.db import models
