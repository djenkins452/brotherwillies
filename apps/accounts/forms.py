from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm
from .models import UserProfile, UserModelConfig, ModelPreset


class RegisterForm(UserCreationForm):
    email = forms.EmailField(required=False)

    class Meta:
        model = User
        fields = ['username', 'email', 'password1', 'password2']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs['class'] = 'form-control'


class ProfileForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = [
            'favorite_conference', 'favorite_team', 'always_include_favorite_team',
            'preference_spread_min', 'preference_spread_max',
            'preference_odds_min', 'preference_odds_max', 'preference_min_edge',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                continue
            field.widget.attrs['class'] = 'form-control'


class ModelConfigForm(forms.ModelForm):
    class Meta:
        model = UserModelConfig
        fields = ['rating_weight', 'hfa_weight', 'injury_weight', 'recent_form_weight', 'conference_weight']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget = forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.05',
                'min': '0',
                'max': '3.0',
            })


class PresetForm(forms.ModelForm):
    class Meta:
        model = ModelPreset
        fields = ['name', 'rating_weight', 'hfa_weight', 'injury_weight', 'recent_form_weight', 'conference_weight']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].widget.attrs['class'] = 'form-control'
        for name in ['rating_weight', 'hfa_weight', 'injury_weight', 'recent_form_weight', 'conference_weight']:
            self.fields[name].widget = forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.05',
                'min': '0',
                'max': '3.0',
            })
