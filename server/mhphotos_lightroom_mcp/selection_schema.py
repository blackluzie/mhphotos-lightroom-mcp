"""Bounded read-only metadata protocol shared by sender and local bridge."""

MAX_BODY = 65536
MAX_PHOTOS = 50
MAX_VALUE_LENGTH = 80
BASE_FIELDS = frozenset({"fileName", "rating", "pickStatus"})
EXTENDED_FIELDS = frozenset(
    {
        "dateTimeOriginalISO8601",
        "cameraMake",
        "cameraModel",
        "lens",
        "shutterSpeed",
        "aperture",
        "isoSpeedRating",
        "focalLength",
        "dimensions",
        "croppedDimensions",
        "fileType",
    }
)
FIELDS = BASE_FIELDS | EXTENDED_FIELDS
PROTOCOL_VERSION = 2


def validate_selection(payload: object, *, require_base: bool = False) -> list:
    if not isinstance(payload, dict) or set(payload) != {"photos"}:
        raise ValueError("invalid_payload")
    photos = payload["photos"]
    if not isinstance(photos, list) or not 1 <= len(photos) <= MAX_PHOTOS:
        raise ValueError("invalid_photos")
    for photo in photos:
        if (
            not isinstance(photo, dict)
            or not photo
            or not set(photo) <= FIELDS
            or (require_base and not BASE_FIELDS <= set(photo))
        ):
            raise ValueError("invalid_fields")
        for key, value in photo.items():
            if (
                not isinstance(value, (str, int))
                or isinstance(value, bool)
                or len(str(value)) > MAX_VALUE_LENGTH
                or (key in EXTENDED_FIELDS and not isinstance(value, str))
            ):
                raise ValueError("invalid_values")
    return photos
