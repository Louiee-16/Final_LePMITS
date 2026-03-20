from django import forms
from .models import Committee
from councilors.models import Councilor

class CommitteeForm(forms.ModelForm):
    class Meta:
        model = Committee
        fields = ['name', 'chairman', 'vice_chairman', 'member']
        
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'w-full px-5 py-4 rounded-2xl border border-slate-200 focus:ring-4 focus:ring-indigo-50 focus:border-indigo-500 outline-none transition-all font-medium text-slate-700',
                'placeholder': 'e.g. Committee on Health and Sanitation'
            }),
            'chairman': forms.Select(attrs={
                'class': 'w-full px-5 py-4 rounded-2xl border border-slate-200 focus:ring-4 focus:ring-indigo-50 focus:border-indigo-500 outline-none transition-all font-medium text-slate-700 bg-white appearance-none'
            }),
            'vice_chairman': forms.Select(attrs={
                'class': 'w-full px-5 py-4 rounded-2xl border border-slate-200 focus:ring-4 focus:ring-indigo-50 focus:border-indigo-500 outline-none transition-all font-medium text-slate-700 bg-white appearance-none'
            }),
            'member': forms.RadioSelect(attrs={
                'class': 'rounded text-indigo-600 focus:ring-indigo-500'
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set the dropdown options to all councilors
        self.fields['chairman'].queryset = Councilor.objects.all()
        self.fields['vice_chairman'].queryset = Councilor.objects.all()