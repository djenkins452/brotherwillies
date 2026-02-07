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


class PersonalInfoForm(forms.Form):
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    zip_code = forms.CharField(max_length=5, required=False, help_text='US zip code (sets your timezone)')
    profile_picture = forms.ImageField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name != 'profile_picture':
                field.widget.attrs['class'] = 'form-control'
        self.fields['zip_code'].widget.attrs.update({
            'pattern': '[0-9]{5}',
            'inputmode': 'numeric',
            'maxlength': '5',
            'placeholder': 'e.g. 35487',
        })

    def clean_zip_code(self):
        val = self.cleaned_data.get('zip_code', '').strip()
        if val and (len(val) != 5 or not val.isdigit()):
            raise forms.ValidationError('Enter a valid 5-digit US zip code.')
        return val


class PreferencesForm(forms.ModelForm):
    class Meta:
        model = UserProfile
        fields = [
            'favorite_conference', 'favorite_team',
            'favorite_cbb_conference', 'favorite_cbb_team',
            'always_include_favorite_team',
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
