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
            'class': 'w-full rounded-xl border border-slate-200 px-5 py-3 text-sm text-slate-700 placeholder-slate-400 outline-none focus:border-slate-300 focus:ring-2 focus:ring-slate-100 transition-all',
            'placeholder': 'Barangay Ordinance No. _, Series of ____'
        })
    )
    subject = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'w-full rounded-xl border border-slate-200 px-5 py-3 text-sm text-slate-700 placeholder-slate-400 outline-none focus:border-slate-300 focus:ring-2 focus:ring-slate-100 transition-all',
            'placeholder': 'Enter subject'
        })
    )
    remarks = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={
            'class': 'w-full rounded-xl border border-slate-200 px-5 py-3 text-sm text-slate-700 placeholder-slate-400 outline-none focus:border-slate-300 focus:ring-2 focus:ring-slate-100 transition-all resize-none',
            'rows': 3,
            'placeholder': 'Optional remarks'
        })
    )
    uploaded_docx = forms.FileField(
        required=True,
        widget=forms.ClearableFileInput(attrs={
            'class': 'w-full text-sm text-slate-600 file:mr-4 file:py-2 file:px-4 file:rounded-xl file:border-0 file:text-sm file:font-semibold file:bg-slate-100 file:text-slate-700 hover:file:bg-slate-200 file:transition-all outline-none',
            'accept': '.docx',
        })
    )

    class Meta:
        model = BarangayFiles
        fields = ['title', 'uploaded_docx', 'subject', 'remarks']

    def clean_title(self):
        title = self.cleaned_data['title']

        pattern = r'^Barangay Ordinance No\. \d+, Series of \d{4}$'
        if not re.match(pattern, title):
            raise forms.ValidationError(
                'Use format: Barangay Ordinance No. <number>, Series of <year>'
            )

        return title

    def clean_uploaded_docx(self):
        # A .docx is really a zip archive — "PK\x03\x04" magic bytes catch
        # a mislabeled/non-docx file before it becomes a committee's
        # working document. Extension check first since it's cheap and
        # gives a clearer error for the common "picked the wrong file"
        # case.
        f = self.cleaned_data['uploaded_docx']
        if not f.name.lower().endswith('.docx'):
            raise forms.ValidationError('Upload a .docx file.')
        header = f.read(4)
        f.seek(0)
        if header != b'PK\x03\x04':
            raise forms.ValidationError('That file doesn\'t look like a valid .docx file.')
        return f