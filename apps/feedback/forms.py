from django import forms
from .models import PartnerFeedback, FeedbackComponent


class FeedbackSubmitForm(forms.ModelForm):
    class Meta:
        model = PartnerFeedback
        fields = ['component', 'title', 'description']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 6, 'placeholder': 'Brain dump your thoughts here...'}),
            'title': forms.TextInput(attrs={'placeholder': 'Short summary of your feedback'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['component'].queryset = FeedbackComponent.objects.filter(is_active=True)
        for name, field in self.fields.items():
            field.widget.attrs['class'] = 'form-control'


class FeedbackEditForm(forms.ModelForm):
    class Meta:
        model = PartnerFeedback
        fields = ['title', 'description', 'status', 'reviewer_notes']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 6}),
            'reviewer_notes': forms.Textarea(attrs={'rows': 4, 'placeholder': 'Required for READY or DISMISSED status'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs['class'] = 'form-control'

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get('status')
        notes = cleaned.get('reviewer_notes', '').strip()
        if status in (PartnerFeedback.Status.READY, PartnerFeedback.Status.DISMISSED) and not notes:
            self.add_error('reviewer_notes', 'Reviewer notes are required when setting status to Ready or Dismissed.')
        return cleaned
