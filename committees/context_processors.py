def committees_processor(request):
    if not request.user.is_authenticated:
        return {}

    committees = []

    if request.user.role == 'COUNCILOR':
        councilor = getattr(request.user, 'councilor_profile', None)

        if councilor:
            committees = councilor.chaired_committees.all()

    return {
        'committees': committees
    }