import zipcodes


def zip_to_timezone(zip_code):
    """Look up a US timezone from a 5-digit zip code.
    Returns the IANA timezone string, or empty string if not found.
    """
    if not zip_code or len(zip_code) < 5:
        return ''
    results = zipcodes.matching(zip_code)
    if results:
        return results[0].get('timezone', '')
    return ''
