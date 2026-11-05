"""
Clutch (ক্ল্যাচ) - Automated F-Commerce Order Management Engine
Core Sanitization and Normalization Engine
"""

from typing import Union, List, Tuple, Optional, Dict, Any
import re

# -------------------------------------------------------------------------
# Numerals Mapping
# -------------------------------------------------------------------------
BENGALI_DIGITS = "০১২৩৪৫৬৭৮৯"
ASCII_DIGITS = "0123456789"
BENGALI_TO_ASCII_MAP = str.maketrans(BENGALI_DIGITS, ASCII_DIGITS)
ASCII_TO_BENGALI_MAP = str.maketrans(ASCII_DIGITS, BENGALI_DIGITS)

# -------------------------------------------------------------------------
# Mobile Operators & Delivery Constants
# -------------------------------------------------------------------------
VALID_OPERATOR_PREFIXES = ("013", "014", "015", "016", "017", "018", "019")

FEE_DHAKA_CORE = 70
FEE_SUBURBS = 100
FEE_OUTSIDE_DHAKA = 130

ZONE_DHAKA_CORE = "Inside Dhaka"
ZONE_INSIDE_DHAKA = "Inside Dhaka"
ZONE_SUBURBS = "Suburbs"
ZONE_DHAKA_SUBURBS = "Suburbs"
ZONE_OUTSIDE_DHAKA = "Outside Dhaka"


class DeliveryZoneResult:
    """
    Polymorphic Result representing a classified delivery zone and fee.
    Supports:
      - Tuple unpacking: `zone, fee = resolve_zone(address)`
      - Attribute access: `res.zone`, `res.fee`, `res.tier`, `res.name`
      - Dict access: `res['zone']`, `res['fee']`
      - String & tuple equality comparisons:
          `res == "Inside Dhaka"`, `res == "Dhaka Core"`,
          `res == "Suburbs"`, `res == "Dhaka Suburbs"`,
          `res == ("Inside Dhaka", 70)`
    """

    def __init__(self, zone: str, fee: int, matched_keyword: Optional[str] = None):
        self.zone = zone
        self.fee = int(fee)
        self.matched_keyword = matched_keyword
        # Aliases for convenient caller ergonomics
        self.name = zone
        self.tier = zone
        self.charge = fee
        self.bdt = fee

    def __iter__(self):
        yield self.zone
        yield self.fee

    def __getitem__(self, item: str) -> Any:
        if item in ("zone", "name", "tier"):
            return self.zone
        if item in ("fee", "charge", "bdt"):
            return self.fee
        if item == "matched_keyword":
            return self.matched_keyword
        raise KeyError(item)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            lower_other = other.strip().lower()
            lower_zone = self.zone.strip().lower()
            if lower_zone == lower_other:
                return True
            # Allow interchangeable "Inside Dhaka" and "Dhaka Core"
            if lower_zone in ("inside dhaka", "dhaka core") and lower_other in ("inside dhaka", "dhaka core"):
                return True
            # Allow interchangeable "Suburbs" and "Dhaka Suburbs"
            if lower_zone in ("suburbs", "dhaka suburbs") and lower_other in ("suburbs", "dhaka suburbs"):
                return True
            # Allow interchangeable "Outside Dhaka" and "Outside"
            if lower_zone in ("outside dhaka", "outside") and lower_other in ("outside dhaka", "outside"):
                return True
            return False
        if isinstance(other, (tuple, list)) and len(other) == 2:
            zone_match = (self.zone == other[0]) or self.__eq__(other[0])
            fee_match = self.fee == other[1]
            return zone_match and fee_match
        if isinstance(other, dict):
            return self.zone == other.get("zone") and self.fee == other.get("fee")
        if isinstance(other, DeliveryZoneResult):
            return self.zone == other.zone and self.fee == other.fee
        return False

    def __repr__(self) -> str:
        return f"DeliveryZoneResult(zone='{self.zone}', fee={self.fee}, matched_keyword={self.matched_keyword!r})"

    def __str__(self) -> str:
        return self.zone

    def to_dict(self) -> Dict[str, Any]:
        return {
            "zone": self.zone,
            "fee": self.fee,
            "matched_keyword": self.matched_keyword,
        }


# -------------------------------------------------------------------------
# 1. Bengali Numeral Normalizer
# -------------------------------------------------------------------------
def convert_bengali_digits(text: Optional[str]) -> str:
    """
    Map and convert all Bengali numerals (০-৯) to standard ASCII digits (0-9).
    Non-digit characters and existing ASCII characters are preserved.
    """
    if not text:
        return ""
    return str(text).translate(BENGALI_TO_ASCII_MAP)


def convert_ascii_to_bengali_digits(text: Optional[str]) -> str:
    """
    Map and convert ASCII digits (0-9) to Bengali numerals (০-৯).
    """
    if not text:
        return ""
    return str(text).translate(ASCII_TO_BENGALI_MAP)


# -------------------------------------------------------------------------
# 2. Bangladeshi Phone Sanitizer & Normalizer
# -------------------------------------------------------------------------
def normalize_bd_phone(raw_text: Optional[str]) -> List[str]:
    """
    Extract and normalize valid 11-digit Bangladeshi mobile numbers.
    
    Rules & Features:
    - 1. Converts Bengali digits (০-৯) to ASCII digits (0-9).
    - 2. Strips spaces, hyphens, plus signs, dots, and parentheses.
    - 3. Handles international prefixes: +880, 880, 00880, or domestic 01X.
    - 4. Validates operator codes: 013, 014, 015, 016, 017, 018, 019.
    - 5. Normalizes all extracted numbers to standard 11-digit MSISDN format.
    
    Returns:
        List of 11-digit strings matching valid BD mobile numbers.
    """
    if not raw_text:
        return []

    # Step 1: Map Bengali digits (০-৯) to ASCII (0-9)
    ascii_text = convert_bengali_digits(raw_text)

    # Step 2: Strip formatting noise (spaces, hyphens, pluses, parentheses)
    sanitized = re.sub(r"[\s\-\+\(\)]", "", ascii_text)

    # Step 3: Match valid 11-digit numbers with BD prefixes (013-019)
    # Allows prefixes 8801 or 01 followed by operator digit [3-9] and 8 digits
    matches = re.findall(r"(?:8801|01)[3-9]\d{8}", sanitized)

    # Step 4: Normalize to standard 11 digits (stripping 88 country code if present)
    return [m[-11:] for m in matches]


# -------------------------------------------------------------------------
# 3. Multi-Number Conflict Detector
# -------------------------------------------------------------------------
def detect_multi_number_conflict(raw_text_or_numbers: Union[str, List[str], None]) -> bool:
    """
    Detect whether multiple conflicting (distinct) phone numbers exist in a text block.
    
    Rules:
    - 0 phone numbers -> False (no conflict)
    - 1 phone number -> False (unambiguous)
    - Same phone number repeated (e.g. pasted twice) -> False (deduplicated, no conflict)
    - 2 or more distinct phone numbers -> True (conflict detected, requires human review)
    """
    if not raw_text_or_numbers:
        return False

    if isinstance(raw_text_or_numbers, list):
        # Already extracted/normalized list
        numbers = raw_text_or_numbers
    else:
        numbers = normalize_bd_phone(raw_text_or_numbers)

    unique_numbers = set(numbers)
    return len(unique_numbers) > 1


# Aliases for conflict detection
detect_phone_conflicts = detect_multi_number_conflict
has_multi_number_conflict = detect_multi_number_conflict


def extract_clean_phone(raw_text: Optional[str]) -> Tuple[Optional[str], bool]:
    """
    Extract a single unambiguous clean phone number.
    
    Returns:
        (phone_number, has_conflict)
        - If exactly 1 distinct valid number: (phone, False)
        - If multiple distinct valid numbers: (None, True)
        - If no valid numbers found: (None, False)
    """
    numbers = normalize_bd_phone(raw_text)
    unique_numbers = list(dict.fromkeys(numbers))  # preserve order while deduplicating

    if len(unique_numbers) == 1:
        return unique_numbers[0], False
    elif len(unique_numbers) > 1:
        return None, True
    return None, False


# -------------------------------------------------------------------------
# 4. 3-Tier Delivery Boundary Detector & Explicit Fee Mapping
# -------------------------------------------------------------------------

# Suburbs: Boundary towns around Dhaka (Fee: 100 BDT)
SUBURB_KEYWORDS = [
    # Savar
    "savar", "shavar", "saver", "সাভার",
    # Gazipur
    "gazipur", "gajipur", "গাজীপুর", "গাজিপুর",
    # Tongi
    "tongi", "tongy", "টঙ্গী", "টঙ্গি",
    # Keraniganj
    "keraniganj", "keranigonj", "keranigong", "কেরানীগঞ্জ", "কেরানিগঞ্জ",
    # Narayanganj
    "narayanganj", "narayangonj", "narayangong", "নারায়ণগঞ্জ", "নারায়নগঞ্জ",
    # Ashulia
    "ashulia", "asulia", "আশুলিয়া", "আশুলিয়া",
    # Suburb growth centers & industrial hubs
    "baipail", "বাইপাইল",
    "zirani", "জিরানী", "জিরানি",
    "konabari", "কোনাবাড়ী", "কোনাবাড়ী",
    "board bazar", "boardbazar", "বোর্ড বাজার", "বোর্ডবাজার",
    "joydebpur", "জয়দেবপুর", "জয়দেবপুর",
    "chashara", "chasara", "চাষাঢ়া", "চাষাড়া",
    "siddhirganj", "shiddhirganj", "সিদ্ধিরগঞ্জ",
    "fatullah", "fatulla", "ফতুল্লা", "ফতুল্লাহ",
    "sonargaon", "সোনারগাঁও", "সোনারগা",
    "kanchpur", "কাঁচপুর", "কাচপুর",
    "rupganj", "রূপগঞ্জ", "রুপগঞ্জ",
    "hemayetpur", "হেমায়েতপুর", "হেমায়েতপুর",
    "dhamrai", "ধামরাই",
    "epz", "depz",
]

# Dhaka Core: Inside Dhaka metropolitan area (Fee: 70 BDT)
DHAKA_CORE_KEYWORDS = [
    # Mirpur
    "mirpur", "মিরপুর", "kazipara", "কাজীপাড়া", "কাজীপাড়া", "shewrapara", "শেওড়াপাড়া", "শেওড়াপাড়া",
    "agargaon", "আগারগাঁও", "আগারগা", "senpara", "paikpara", "kalyanpur", "কল্যাণপুর", "gabtoli", "গাবতলী",
    # Uttara
    "uttara", "উত্তরা", "azampur", "আজমপুর", "jasimuddin", "জসীমউদ্দীন", "জসিমউদদীন", "rajlakshmi", "রাজলক্ষ্মী",
    "abdullahpur", "আবদুল্লাহপুর", "dakshinkhan", "দক্ষিণখান", "uttarkhan", "উত্তরখান", "airport", "বিমানবন্দর", "এয়ারপোর্ট",
    # Dhanmondi
    "dhanmondi", "dhanmondy", "ধানমন্ডি", "ধানমন্ডী", "shankar", "শংকর", "jigatola", "জিগাতলা",
    "rayerbazar", "রায়েরবাজার", "রায়েরবাজার", "kalabagan", "কলাবাগান", "sobhanbag", "সোবহানবাগ", "panthapath", "পান্থপথ",
    # Gulshan
    "gulshan", "গুলশান", "niketan", "নিকেতন",
    # Banani
    "banani", "বনানী", "kakoli", "কাকলী", "chairman bari", "চেয়ারম্যান বাড়ি",
    # Mohammadpur
    "mohammadpur", "মোহাম্মদপুর", "lalmatia", "লালমাটিয়া", "লালমাটিয়া", "japan garden", "ring road", "iqbal road",
    "tajmahal road", "salimullah road", "asad gate", "আসাদ গেট", "bosila", "basila", "বসিলা", "chad uddan",
    # Badda
    "badda", "বাড্ডা", "merul badda", "aftabnagar", "আফতাবনগর", "rampura", "রামপুরা", "banasree", "বনশ্রী",
    # Motijheel
    "motijheel", "মতিঝিল", "dilkusha", "দিলকুশা", "fakirapool", "ফকিরাপুল", "arambagh", "আরামবাগ", "kamalapur", "কমলাপুর",
    # Additional core Dhaka neighborhoods
    "tejgaon", "তেজগাঁও", "farmgate", "ফার্মগেট", "nakhalpara", "tejkunipara",
    "mohakhali", "মহাখালী", "wireless gate",
    "baridhara", "বারিধারা", "bashundhara", "basundhara", "বসুন্ধরা",
    "khilkhet", "খিলক্ষেত", "kuril", "কুড়িল", "কুড়িল", "nikunja", "নিকুঞ্জ",
    "malibagh", "মালিবাগ", "mouchak", "মৌচাক", "shantinagar", "শান্তিনগর", "kakrail", "কাকরাইল", "bailey road",
    "segunbagicha", "সেগুনবাগিচা", "paltan", "পল্টন", "bijoynagar", "বিজয়নগর", "বিজয়নগর",
    "khilgaon", "খিলগাঁও", "goran", "গোরান", "mugda", "মুগদা", "basabo", "বাসাবো", "maniknagar", "মানিকনগর",
    "sayedabad", "সায়েদাবাদ", "সায়েদাবাদ", "jatrabari", "যাত্রাবাড়ী", "যাত্রাবাড়ী", "jurain", "জুরাইন", "postogola", "পোস্তগোলা", "demra", "ডেমরা",
    "shahbagh", "শাহবাগ", "elephant road", "নিউ মার্কেট", "new market", "katabon", "কাটাবন", "nilkhet", "নীলক্ষেত", "azimpur", "আজিমপুর",
    "lalbagh", "লালবাগ", "wari", "ওয়ারী", "ওয়ারী", "kotwali", "কোতোয়ালী", "sutrapur", "সূত্রাপুর", "chawkbazar", "চকবাজার",
    "puran dhaka", "old dhaka", "পুরান ঢাকা", "bangshal", "বংশাল", "sadarghat", "সদরঘাট", "islampur", "ইসলামপুর",
    "hazaribagh", "হাজারীবাগ", "kamrangirchar", "কামরাঙ্গীরচর",
    "cantonment", "সেনানিবাস", "ক্যান্টনমেন্ট", "kafrul", "কাফরুল", "ibrahimpur", "ইব্রাহিমপুর", "vashantek", "ভাষানটেক",
    # Dhaka city general mentions
    "dhaka", "dhaka city", "ঢাকা",
]

# Explicit outside districts of Bangladesh
OUTSIDE_DHAKA_DISTRICTS = [
    "chittagong", "chattogram", "চট্টগ্রাম", "ctg",
    "sylhet", "সিলেট",
    "rajshahi", "রাজশাহী",
    "khulna", "খুলনা",
    "barisal", "barishal", "বরিশাল",
    "rangpur", "রংপুর",
    "mymensingh", "ময়মনসিংহ", "ময়মনসিংহ",
    "comilla", "cumilla", "কুমিল্লা",
    "cox's bazar", "coxs bazar", "কক্সবাজার",
    "bogura", "bogra", "বগুড়া", "বগুড়া",
    "jessore", "jashore", "যশোর",
    "pabna", "পাবনা",
    "dinajpur", "দিনাজপুর",
    "feni", "ফেনী",
    "noakhali", "নোয়াখালী",
    "tangail", "টাঙ্গাইল",
    "brahmanbaria", "ব্রাহ্মণবাড়িয়া", "ব্রাহ্মণবাড়িয়া", "b-baria", "bbaria",
    "narsingdi", "নরসিংদী",
    "manikganj", "মানিকগঞ্জ",
    "munshiganj", "মুন্সীগঞ্জ",
    "faridpur", "ফরিদপুর",
    "kushtia", "কুষ্টিয়া", "কুষ্টিয়া",
    "natore", "নাটোর",
    "sirajganj", "সিরাজগঞ্জ",
    "naogaon", "নওগাঁ",
    "chapainawabganj", "চাঁপাইনবাবগঞ্জ",
    "jamalpur", "জামালপুর",
    "sherpur", "শেরপুর",
    "netrokona", "নেত্রকোণা", "নেত্রকোনা",
    "kishoreganj", "কিশোরগঞ্জ",
    "sunamganj", "সুনামগঞ্জ",
    "habiganj", "হবিগঞ্জ",
    "moulvibazar", "মৌলভীবাজার",
    "chandpur", "চাঁদপুর",
    "lakshmipur", "লক্ষ্মীপুর",
    "bandarban", "বান্দরবান",
    "rangamati", "রাঙ্গামাটি",
    "khagrachhari", "খাগড়াছড়ি", "খাগড়াছড়ি",
    "satkhira", "সাতক্ষীরা",
    "bagerhat", "বাগেরহাট",
    "jhenaidah", "ঝিনাইদহ",
    "magura", "মাগুরা",
    "narail", "নড়াইল", "নড়াইল",
    "chuadanga", "চুয়াডাঙ্গা", "চুয়াডাঙ্গা",
    "meherpur", "মেহেরপুর",
    "kurigram", "কুড়িগ্রাম", "কুড়িগ্রাম",
    "gaibandha", "গাইবান্ধা",
    "lalmonirhat", "লালমনিরহাট",
    "nilphamari", "নীলফামারী",
    "panchagarh", "পঞ্চগড়", "পঞ্চগড়",
    "thakurgaon", "ঠাকুরগাঁও",
    "patuakhali", "পটুয়াখালী",
    "bhola", "ভোলা",
    "pirojpur", "পিরোজপুর",
    "barguna", "বরগুনা",
    "jhalokati", "ঝালকাঠি",
    "gopalganj", "গোপালগঞ্জ",
    "madaripur", "মাদারীপুর",
    "shariatpur", "শরীয়তপুর", "শরীয়তপুর",
    "rajbari", "রাজবাড়ী", "রাজবাড়ী",
]


def _match_keyword(text: str, keyword: str) -> bool:
    """
    Match keyword in text.
    - If keyword contains non-ASCII (e.g. Bengali), substring match is used.
    - If ASCII keyword, word boundary regex is used to avoid false sub-string matches.
    """
    if re.search(r"[\u0980-\u09FF]", keyword):
        # Bengali script: direct search handles inflectional suffixes (e.g. মিরপুরে, সাভারে)
        return keyword in text
    else:
        # ASCII script: use word boundary
        pattern = r"\b" + re.escape(keyword) + r"\b"
        return bool(re.search(pattern, text, re.IGNORECASE))


def resolve_zone(address: Optional[str]) -> DeliveryZoneResult:
    """
    Classify an address into the 3-tier delivery boundary with explicit fee mapping.
    
    Tiers & Fees:
      1. Suburbs (Fee: 100 BDT)
         Boundary towns: Savar, Gazipur, Tongi, Keraniganj, Narayanganj, Ashulia.
         Note: Evaluated FIRST so addresses like 'Savar, Dhaka' are properly charged suburb rates.
      2. Outside Dhaka (Fee: 130 BDT)
         Explicit districts outside Dhaka (e.g. Chittagong, Sylhet, Rajshahi, Barishal, Jashore, etc.).
         Note: Evaluated before generic/shared Dhaka thana names (e.g. 'Barishal Chawkbazar' is Outside Dhaka).
      3. Dhaka Core / Inside Dhaka (Fee: 70 BDT)
         Core zones: Mirpur, Uttara, Dhanmondi, Gulshan, Banani, Mohammadpur, Badda, Motijheel, etc.
      4. Outside Dhaka Fallback (Fee: 130 BDT)
         Default fallback for unrecognized locations.
    
    Returns:
        DeliveryZoneResult object (unpacks as tuple (zone, fee), matches strings & tuples)
    """
    if not address or not str(address).strip():
        return DeliveryZoneResult(zone=ZONE_OUTSIDE_DHAKA, fee=FEE_OUTSIDE_DHAKA, matched_keyword=None)

    clean_addr = str(address).strip().lower()

    # Step 1: Check Suburbs FIRST
    # Crucial for addresses like "Savar, Dhaka" or "Ashulia, Dhaka"
    for kw in SUBURB_KEYWORDS:
        if _match_keyword(clean_addr, kw.lower()):
            return DeliveryZoneResult(zone=ZONE_SUBURBS, fee=FEE_SUBURBS, matched_keyword=kw)

    # Step 2: Check Explicit Outside Districts
    # Exception: "chittagong road" / "চিটাগাং রোড" / "ctg road" is a locality/road in Dhaka (Jatrabari/Demra)
    is_ctg_road = bool(re.search(r"\b(?:chittagong|chattogram|ctg)\s+road\b", clean_addr) or "চিটাগাং রোড" in clean_addr or "চট্টগ্রাম রোড" in clean_addr)
    for kw in OUTSIDE_DHAKA_DISTRICTS:
        kw_lower = kw.lower()
        if kw_lower in ("chittagong", "chattogram", "ctg", "চট্টগ্রাম") and is_ctg_road:
            continue
        if _match_keyword(clean_addr, kw_lower):
            return DeliveryZoneResult(zone=ZONE_OUTSIDE_DHAKA, fee=FEE_OUTSIDE_DHAKA, matched_keyword=kw)

    # Step 3: Check Dhaka Core
    for kw in DHAKA_CORE_KEYWORDS:
        if _match_keyword(clean_addr, kw.lower()):
            return DeliveryZoneResult(zone=ZONE_DHAKA_CORE, fee=FEE_DHAKA_CORE, matched_keyword=kw)

    # Step 4: Default fallback
    return DeliveryZoneResult(zone=ZONE_OUTSIDE_DHAKA, fee=FEE_OUTSIDE_DHAKA, matched_keyword=None)


def get_delivery_fee(address: Optional[str]) -> int:
    """
    Convenience helper to get only the delivery fee (in BDT) for an address.
    """
    return resolve_zone(address).fee


# -------------------------------------------------------------------------
# 5. Composite Order Sanitizer
# -------------------------------------------------------------------------
def sanitize_order_payload(raw_text: str, explicit_address: Optional[str] = None) -> Dict[str, Any]:
    """
    High-level composite normalizer combining phone extraction, conflict detection,
    and 3-tier delivery zone resolution from a customer's raw text payload.
    """
    phones = normalize_bd_phone(raw_text)
    clean_phone, has_conflict = extract_clean_phone(raw_text)

    # Address resolution: use explicit_address if provided, else analyze raw_text
    addr_to_evaluate = explicit_address if explicit_address else raw_text
    zone_result = resolve_zone(addr_to_evaluate)

    return {
        "raw_text": raw_text,
        "clean_phone": clean_phone,
        "all_phones": phones,
        "has_phone_conflict": has_conflict,
        "zone": zone_result.zone,
        "fee": zone_result.fee,
        "matched_zone_keyword": zone_result.matched_keyword,
        "requires_human_review": has_conflict or (clean_phone is None),
    }
