from django.urls import path
from . import views

urlpatterns = [
    path('',views.barangay_dashboard, name='barangay-dashboard'),
    path('registry/', views.barangay_list, name='barangay-list'),
    path('registry/add/', views.add_barangay, name='add-barangay'),
    path('checker/',views.checker, name='checker'),
    path('upload_measure/',views.upload_measure, name='upload-measure'),
    path('barangay_to_referral/<int:doc_id>/',views.barangay_to_referral, name='barangay-to-referral')
]