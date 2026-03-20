#committee/views.py
from django.shortcuts import render,redirect, get_object_or_404
from .forms import CommitteeForm
from .models import Committee
from django.contrib.auth.decorators import login_required

def add_committee(request):
    if request.method == 'POST':
        form = CommitteeForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('committee_list')
    else:
        form = CommitteeForm()

    return render(request, 'committee/add_committee.html',{'form':form})
    
def committee_list(request):
    committees = Committee.objects.all().select_related('chairman__user', 'vice_chairman__user').prefetch_related('member__user')
    return render(request, 'committee/committee_list.html', {'committees': committees})


@login_required
def edit_committee(request, committee_id):
    if request.user.role not in ['SECRETARIAT', 'STAFF', 'ADMIN']:
        return redirect('committee_list')

    committee = get_object_or_404(Committee, id=committee_id)

    if request.method == 'POST':
        form = CommitteeForm(request.POST, instance=committee)
        if form.is_valid():
            form.save()
        return redirect('committee_list')
    else:
        form = CommitteeForm(instance=committee)

    return render(request, 'committee/committee_edit.html', {
        'form': form,
        'committee': committee
    })


