from django import forms
from .models import Barangay, BarangayFiles
from documents.models import Document
import re
class BarangayForm(forms.ModelForm):

    barangay_name = forms.CharField(max_length=100, required=True)
    captain = forms.CharField(max_length=100, required=True)
    email = forms.EmailField(required=True)

    class Meta:
        model = Barangay
        fields = ['barangay_name','captain','email'] 

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['barangay_name'].widget.attrs.update({
            'class': 'w-full bg-slate-50 border-none rounded-2xl px-6 py-4 text-sm font-bold text-slate-700 focus:ring-2 focus:ring-indigo-500 shadow-inner'
        })




class MeasureUploadForm(forms.ModelForm):
    title = forms.CharField(
        max_length=200,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'w-full rounded-xl border border-slate-300 px-5 py-3 text-sm text-slate-700 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500',
            'placeholder': 'Barangay Ordinance No. _, Series of ____'
        })
    )
    subject = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'w-full rounded-xl border border-slate-300 px-5 py-3 text-sm text-slate-700 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500',
            'placeholder': 'Enter subject'
        })
    )
    remarks = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'w-full rounded-xl border border-slate-300 px-5 py-3 text-sm text-slate-700 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 resize-none',
            'rows': 3,
            'placeholder': 'Optional remarks'
        })
    )
    scanned_pdf = forms.FileField(
        required=True,
        widget=forms.ClearableFileInput(attrs={
            'class': 'w-full text-sm text-slate-600 file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-sm file:font-semibold file:bg-indigo-50 file:text-indigo-600 hover:file:bg-indigo-100 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500',
        })
    )

    class Meta:
        model = BarangayFiles
        fields = ['title', 'scanned_pdf', 'subject', 'remarks']

    def clean_title(self):
        title = self.cleaned_data['title']
        
        pattern = r'^Barangay Ordinance No\. \d+, Series of \d{4}$'
        if not re.match(pattern, title):
            raise forms.ValidationError(
                'Use format: Barangay Ordinance No. <number>, Series of <year>'
            )
        
        return title