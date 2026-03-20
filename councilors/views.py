from .forms import CouncilorForm
from django.shortcuts import render,redirect, get_object_or_404
from .models import Councilor
from accounts.models import User


def add_councilor(request):
    if request.method == "POST":
        form = CouncilorForm(request.POST, request.FILES)
        if form.is_valid():
            user= User.objects.create_user(
                role = 'COUNCILOR',
                username=form.cleaned_data['email'],
                email=form.cleaned_data['email'],
                password='password123'
            )

            councilor = form.save(commit=False)
            councilor.user = user
            councilor.save()

            return redirect("councilors_list")
    else:
        form = CouncilorForm()
    return render(request, 'councilors/add_councilor.html',{'form':form})

def councilors(request):
    councilor = Councilor.objects.filter(is_active=True)
    return render(request,'councilors/councilor_list.html', {'councilor':councilor})

def editCouncilor(request, councilor_id):
    councilor = get_object_or_404(Councilor,id=councilor_id)

    if request.method == "POST":
        form = CouncilorForm(request.POST, request.FILES, instance=councilor)
        if form.is_valid():
            form.save()
            return redirect('councilors_list')
    else:
        form = CouncilorForm(instance=councilor)

    return render(request, 'councilors/edit_councilor.html',{'form':form, 'councilor':councilor})

def end_term(request, councilor_id):
    councilor = get_object_or_404(Councilor, id=councilor_id)
    councilor.is_active = False
    councilor.save()
    return redirect('councilors_list')