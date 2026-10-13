"""Literal read-only filters for bounded, explicitly submitted photo metadata."""
from mhphotos_lightroom_mcp.selection_schema import MAX_VALUE_LENGTH, validate_selection

FILTER_FIELDS = {
    'file_name': 'fileName', 'camera_model': 'cameraModel', 'lens': 'lens',
    'file_type': 'fileType', 'rating': 'rating', 'pick_status': 'pickStatus',
}
SUBSTRING_FIELDS = frozenset({'file_name', 'camera_model', 'lens'})


def validate_filters(filters: dict) -> None:
    if not filters or not set(filters) <= set(FILTER_FIELDS):
        raise ValueError('invalid_filter_request')
    for key, value in filters.items():
        if (
            not isinstance(value, str) or len(value) > MAX_VALUE_LENGTH
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or (key not in {'rating', 'pick_status'} and not value.strip())
        ):
            raise ValueError('invalid_filter_request')


def filter_selection(photos: list, filters: dict) -> dict:
    validate_filters(filters)
    validate_selection({'photos': photos})
    def matches(photo):
        for key, wanted in filters.items():
            field = FILTER_FIELDS[key]
            if field not in photo:
                return False
            actual = str(photo[field])
            if key in SUBSTRING_FIELDS:
                if wanted.strip().casefold() not in actual.casefold():
                    return False
            elif key == 'file_type':
                if wanted.strip().casefold() != actual.casefold():
                    return False
            elif wanted != actual:
                return False
        return True
    selected = [photo.copy() for photo in photos if matches(photo)]
    return {
        'photos': selected, 'photo_count': len(selected), 'snapshot_count': len(photos),
        'applied_filters': filters.copy(), 'source': 'local_bridge_snapshot',
        'catalog_writes': False,
    }
