"""Literal search of a single bounded, read-only collection metadata page."""
import unicodedata

from mhphotos_lightroom_mcp.live_reads import validate_result

SEARCH_FIELDS = (
    'fileName', 'keywordTagsForExport', 'title', 'caption',
    'location', 'city', 'stateProvince', 'country',
)


def normalized_query(query):
    if (not isinstance(query, str) or not query.strip() or len(query) > 80
        or any(ord(c) < 32 or ord(c) == 127 for c in query)):
        raise ValueError('invalid_search_query')
    query.encode('utf-8')  # Reject invalid surrogate text before contacting Lightroom.
    return unicodedata.normalize('NFC', query.strip()).casefold()


def search_page(request, result, query):
    needle = normalized_query(query)
    result = validate_result(request, result)
    if result.get('error'):
        return {'photos': [], 'reason': result['error']}
    matches = []
    for index, photo in enumerate(result['photos'], request['offset']):
        fields = [field for field in SEARCH_FIELDS if field in photo
                  and needle in unicodedata.normalize('NFC', photo[field]).casefold()]
        if fields:
            matches.append({'collection_offset': index, 'matched_fields': fields,
                            'metadata': dict(photo)})
    end = request['offset'] + len(result['photos'])
    return {
        'photos': matches, 'match_count': len(matches),
        'scanned_count': len(result['photos']), 'offset': request['offset'],
        'next_offset': end if result['has_more'] else None,
        'total_count': result['total_count'], 'session_id': result['session_id'],
        'query': query.strip(), 'search_fields': list(SEARCH_FIELDS),
        'collection_path': list(request['path']),
        'source': 'live_lightroom_read', 'catalog_writes': False,
    }
