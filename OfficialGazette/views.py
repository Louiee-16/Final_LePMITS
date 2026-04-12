from django.shortcuts import render, get_object_or_404
from documents.models import Document
from django.db.models import Q

def gazette_list(request):
    """Publicly accessible list of approved laws."""
    queryset = Document.objects.filter(status='APPROVED').order_by('-updated_at')
    
    query = request.GET.get('q')
    year = request.GET.get('year')
    
    if query:
        queryset = queryset.filter(Q(title__icontains=query) | Q(reference_no__icontains=query))
    if year:
        queryset = queryset.filter(updated_at__year=year)
        
    return render(request, 'gazette/public_list.html', {'approved_docs': queryset})

def gazette_detail(request, pk):
    """View one specific law as a citizen."""
    doc = get_object_or_404(Document, pk=pk, status='APPROVED')
    return render(request, 'gazette/public_detail.html', {'doc': doc})