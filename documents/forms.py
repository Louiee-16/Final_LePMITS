from django import forms
from .models import Document
from committees.models import Committee # Assuming your committee model is here

class DraftForm(forms.ModelForm):
    class Meta:
        model = Document
        # These are the fields we want to show in the form
        fields = ['title', 'doc_type', 'content']
        
        # This part adds the Tailwind CSS classes to the HTML elements
        widgets = {
            'title': forms.TextInput(attrs={
                'class': 'w-full px-4 py-3 border border-gray-300 rounded focus:ring-2 focus:ring-blue-500 outline-none',
                'placeholder': 'Ex: An Ordinance Regulating...'
            }),
            'doc_type': forms.Select(attrs={
                'class': 'w-full px-4 py-3 border border-gray-300 rounded'
            }),
            'content': forms.Textarea(attrs={
                'class': 'w-full h-64 px-4 py-3 border border-gray-300 rounded font-mono text-sm',
                'placeholder': 'WHEREAS...'
            }),
        }

    # Custom field for Committee because it might live in another app
    target_committee = forms.ModelChoiceField(
        queryset=Committee.objects.all(),
        empty_label="-- Select a Committee --",
        widget=forms.Select(attrs={'class': 'w-full px-4 py-3 border border-slate-300 rounded bg-white'})
    )