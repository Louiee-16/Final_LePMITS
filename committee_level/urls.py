from django.urls import path, include
from .views import *
from . import views
urlpatterns = [
    path('report_workbench/<int:draft_id>/', views.report_workbench, name = "report_workbench"),
    path('set_hearing_date/',views.set_hearing_date, name='set_hearing_date'),
    path('report_workbench/<int:draft_id>/pdf/', generate_committee_pdf, name='generate_pdf'),
    path('committee_report/<int:report_id>/file/', views.committee_report_document_bytes, name='committee-report-file'),
    path('committee_report/<int:report_id>/save/', views.committee_report_save, name='committee-report-save'),
    path('hearing_log/<int:hearing_id>/delete/', views.delete_hearing_log, name='delete-hearing-log'),
    path('hearing_log/<int:hearing_id>/restore/', views.restore_hearing_log, name='restore-hearing-log'),
    path('committee_report/<int:report_id>/has_remarks/', views.committee_report_has_remarks, name='committee-report-has-remarks'),
    path('committee-level/<int:doc_id>/amendments/',views.committee_amendments, name='committee-amendments'),
    path('save_committee_amendments/<int:doc_id>/',views.save_committee_amendments, name='save-committee-amendments'),
    path('move_to_second_reading/<int:doc_id>/',views.move_to_second_reading, name='move-to-second-reading'),
    path('view/committee_level/',views.view_committee, name='view-committee'),
    path('move/<int:doc_id>/unfinished_business', views.move_to_unfinished, name="move-to-unfinished")
]


