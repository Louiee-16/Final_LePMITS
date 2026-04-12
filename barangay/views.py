from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import Barangay, BarangayFiles
from accounts.models import User
from .forms import BarangayForm, MeasureUploadForm
from django.http import JsonResponse
from django.db.models import Q
from committees.models import Committee
from django.core.mail import send_mail
from django.conf import settings

@login_required
def barangay_dashboard(request):
    form = MeasureUploadForm()
    filed_measures = BarangayFiles.objects.all()
    context = {
        'form': form,
        'barangays': Barangay.objects.all(),
    }
    return render(request, 'dashboards/barangay.html', context)

@login_required
def upload_measure(request):
    if request.method == "POST":
        form = MeasureUploadForm(request.POST, request.FILES)
        if form.is_valid():

            barangay = form.save(commit=False)
            barangay.origin_barangay = request.user.barangay
            barangay.reference_no = request.POST.get('title')
            barangay.status = 'FILED'
            barangay.save()
            return redirect('barangay-dashboard')
    else:
        form = MeasureUploadForm() 

    return render(request, 'dashboards/barangay.html', {'form': form})

@login_required
def barangay_list(request):
    """
    Main Registry View: Lists all Barangays in the system.
    """
    barangays = Barangay.objects.all().order_by('barangay_name')
    return render(request, 'barangay/barangay_list.html', {'barangays': barangays})

@login_required
def add_barangay(request):
    if request.method == "POST":
        form = BarangayForm(request.POST)
        if form.is_valid():
            user = User.objects.create_user(
                role = 'BARANGAY',
                username = form.cleaned_data['email'],
                email = form.cleaned_data['email'],
                password = 'password123'

            )
            barangay = form.save(commit=False)
            barangay.user = user
            barangay.save()
            return redirect("barangay-list")
    else:
        form = BarangayForm()
    return render(request,'barangay/add_barangay.html', {'form':form})


def checker(request):
    barangay_name = request.GET.get('barangay_name', '').strip()
    email = request.GET.get('email', '').strip()

    errors = []

    if barangay_name and Barangay.objects.filter(barangay_name=barangay_name).exists():
        errors.append('barangay_name')

    if email and Barangay.objects.filter(user__email=email).exists():
        errors.append('email')

    return JsonResponse({'errors': errors})


@login_required
def barangay_to_referral(request, doc_id):
    if request.method == "POST" and request.user.role == 'SECRETARIAT' or request.user.role == 'STAFF':
        doc = get_object_or_404(BarangayFiles, id=doc_id, status='OTHER_MATTERS')
        committee_id = request.POST.get('referred_committee')

        if not committee_id:
            messages.error(request, "Please select a target committee.")
            return redirect('other-matters')

        committee = get_object_or_404(Committee, id=committee_id)
        
        doc.status = 'REFERRED'
        doc.referred_committee = committee
        doc.save() 
        recipients = []

        def add_email(councilor):
            if councilor and hasattr(councilor, 'user') and councilor.user.email:
                recipients.append(councilor.user.email)
            elif councilor and hasattr(councilor, 'email') and councilor.email:
                recipients.append(councilor.email)

        add_email(committee.chairman)
        recipients = list(set(recipients))
        if recipients:
            subject = f"Official Referral: {doc.title} - {doc.title}"
            message = (
                f"Honorable Members of the {committee.name},\\n\\n"
                f"The legislative measure entitled '{doc.title}' ({doc.title}) "
                f"has been officially referred to your committee.\\n\\n"
                f"Please log in to the system to review the content and prepare for the Committee Hearing.\\n\\n"
                f"Best regards,\\n"
                f"Office of the Secretariat"
            )
            
            try:
                send_mail(
                    subject,
                    message,
                    settings.DEFAULT_FROM_EMAIL,
                    recipients,
                    fail_silently=False,
                )
            except Exception as e:
                print(f"SMTP Error: {e}")

        messages.success(request, f"{doc.title} successfully referred to {committee.name}.")
        return redirect('other-matters')
        
    return redirect('other-matters')

    