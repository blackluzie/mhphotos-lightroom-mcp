"""Summarize bounded submitted metadata without retaining or changing photos."""
from collections import Counter

from mhphotos_lightroom_mcp.selection_schema import FIELDS, validate_selection

GROUP_FIELDS = ('cameraMake', 'cameraModel', 'lens', 'fileType', 'rating', 'pickStatus')


def summarize_selection(photos: list) -> dict:
    validate_selection({'photos': photos})
    groups = {}
    for field in GROUP_FIELDS:
        counts = Counter(str(photo[field]) for photo in photos if field in photo and str(photo[field]).strip())
        groups[field] = {
            'values': [{'value': value, 'count': count} for value, count in sorted(counts.items())],
            'missing_count': len(photos) - sum(counts.values()),
        }
    return {
        'photo_count': len(photos),
        'groups': groups,
        'available_metadata_counts': {
            field: sum(field in photo and bool(str(photo[field]).strip()) for photo in photos)
            for field in sorted(FIELDS)
        },
        'source': 'local_bridge_snapshot',
        'catalog_writes': False,
    }
