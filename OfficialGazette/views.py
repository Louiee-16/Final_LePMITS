from django.shortcuts import render, get_object_or_404
from documents.models import Document, NationalLaw
from django.db.models import Q

def gazette_list(request):
    """Publicly accessible list of approved local ordinances/resolutions,
    plus national-law search results when a search term is given."""
    query = (request.GET.get('q') or '').strip()
    year = request.GET.get('year')

    queryset = Document.objects.filter(status='APPROVED').order_by('-updated_at')
    if query:
        queryset = queryset.filter(Q(title__icontains=query) | Q(reference_no__icontains=query))
    if year:
        queryset = queryset.filter(updated_at__year=year)

    # National laws only appear as search results, not on the default
    # browse view — the corpus has ~11,866 rows, and this page's primary
    # purpose is the city's own local ordinance/resolution registry.
    national_laws = NationalLaw.objects.none()
    if query:
        national_laws = NationalLaw.objects.filter(
            Q(title__icontains=query) | Q(law_number__icontains=query) | Q(description__icontains=query)
        )
        if year:
            national_laws = national_laws.filter(year=year)
        national_laws = national_laws.order_by('law_number')[:30]

    return render(request, 'gazette/public_list.html', {
        'approved_docs': queryset,
        'national_laws': national_laws,
        'query': query,
        'year': year or '',
    })

def gazette_detail(request, pk):
    """View one specific law as a citizen."""
    doc = get_object_or_404(Document, pk=pk, status='APPROVED')
    return render(request, 'gazette/public_detail.html', {'doc': doc})

def national_law_detail(request, pk):
    """View one specific national law (Republic Act) as a citizen."""
    law = get_object_or_404(NationalLaw, pk=pk)
    return render(request, 'gazette/national_law_detail.html', {'law': law})
