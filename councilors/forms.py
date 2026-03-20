from django import forms
from councilors.models import Councilor

class CouncilorForm(forms.ModelForm):
    class Meta:
        model = Councilor
        fields = ['name', 'email', 'district', 'profile_picture']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'w-full px-5 py-4 rounded-2xl border border-slate-200 focus:ring-4 focus:ring-indigo-50 focus:border-indigo-500 outline-none transition-all font-medium text-slate-700',
                'placeholder': 'e.g. Councilor Name'
            }),
            'email': forms.EmailInput(attrs={
                'class': 'w-full px-5 py-4 rounded-2xl border border-slate-200 focus:ring-4 focus:ring-indigo-50 focus:border-indigo-500 outline-none transition-all font-medium text-slate-700 bg-white appearance-none'
            }),
            'district': forms.RadioSelect(attrs={
                'class': 'flex gap-6'
            }),
            'profile_picture': forms.ClearableFileInput(attrs={
                'class': 'w-full text-sm text-slate-500 file:mr-4 file:py-2 file:px-4 file:rounded-full file:border-0 file:text-sm file:font-semibold file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100'
            })
        }
