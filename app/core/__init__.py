"""
Core engines for Clutch: Sanitization, Security, and Normalization
"""

from app.core.sanitizer import (
    convert_bengali_digits,
    convert_ascii_to_bengali_digits,
    normalize_bd_phone,
    detect_multi_number_conflict,
    detect_phone_conflicts,
    has_multi_number_conflict,
    extract_clean_phone,
    resolve_zone,
    get_delivery_fee,
    sanitize_order_payload,
    DeliveryZoneResult,
    FEE_DHAKA_CORE,
    FEE_SUBURBS,
    FEE_OUTSIDE_DHAKA,
    ZONE_DHAKA_CORE,
    ZONE_INSIDE_DHAKA,
    ZONE_SUBURBS,
    ZONE_OUTSIDE_DHAKA,
)
from app.core.security import verify_meta_signature
from app.core.config import settings, Settings

__all__ = [
    "convert_bengali_digits",
    "convert_ascii_to_bengali_digits",
    "normalize_bd_phone",
    "detect_multi_number_conflict",
    "detect_phone_conflicts",
    "has_multi_number_conflict",
    "extract_clean_phone",
    "resolve_zone",
    "get_delivery_fee",
    "sanitize_order_payload",
    "DeliveryZoneResult",
    "FEE_DHAKA_CORE",
    "FEE_SUBURBS",
    "FEE_OUTSIDE_DHAKA",
    "ZONE_DHAKA_CORE",
    "ZONE_INSIDE_DHAKA",
    "ZONE_SUBURBS",
    "ZONE_OUTSIDE_DHAKA",
    "verify_meta_signature",
    "settings",
    "Settings",
]
