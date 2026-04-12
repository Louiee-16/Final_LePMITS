from django.urls import path, include
from .views import *
from . import views
urlpatterns = [
    path('report_workbench/<int:draft_id>/', views.report_workbench, name = "report_workbench"),
    path('set_hearing_date/',views.set_hearing_date, name='set_hearing_date'),
    path('report_workbench/<int:draft_id>/pdf/', generate_committee_pdf, name='generate_pdf'),
    path('committee-level/<int:doc_id>/amendments/',views.committee_amendments, name='committee-amendments'),
    path('save_committee_amendments/<int:doc_id>/',views.save_committee_amendments, name='save-committee-amendments'),
    path('move_to_second_reading/<int:doc_id>/',views.move_to_second_reading, name='move-to-second-reading'),
    path('view/committee_level/',views.view_committee, name='view-committee'),
    path('move/<int:doc_id>/unfinished_business', views.move_to_unfinished, name="move-to-unfinished")
]


