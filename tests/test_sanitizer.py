"""
Unit Tests for Clutch Core Data Sanitization and Normalization Engine
Covers:
  - Bengali numerals (০-৯) to ASCII digits (0-9) conversion
  - Bangladeshi phone number extraction & normalization (013-019)
  - Dirty formatting (spaces, hyphens, prefixes: +880, 880, 01)
  - Multi-number conflict detection (single, duplicate, conflicting)
  - 3-tier delivery boundary detector with explicit fee mapping (Dhaka Core ৳70, Suburbs ৳100, Outside Dhaka ৳130)
  - Result object ergonomics (unpacking, equality, attributes)
"""

import pytest
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


# =========================================================================
# 1. Bengali Numeral Conversion Tests
# =========================================================================
class TestBengaliNumeralConversion:
    """Tests for Bengali numeral to ASCII digit conversion."""

    def test_all_bengali_digits_conversion(self):
        """Ensure all 10 Bengali digits (০-৯) map exactly to standard ASCII 0-9."""
        bengali_str = "০১২৩৪৫৬৭৮৯"
        expected = "0123456789"
        assert convert_bengali_digits(bengali_str) == expected

    def test_mixed_bengali_and_ascii_numerals(self):
        """Mixed Bengali and ASCII numerals should normalize cleanly."""
        mixed = "০1২3৪5৬7৮9"
        expected = "0123456789"
        assert convert_bengali_digits(mixed) == expected

    def test_bengali_numerals_in_conversational_text(self):
        """Bengali numerals inside Bengali/English sentences should convert without mutating text."""
        raw = "আমার মোবাইল নম্বর ০১৭১২৩৪৫৬৭৮ এবং বাসার নম্বর ১২/এ"
        expected = "আমার মোবাইল নম্বর 01712345678 এবং বাসার নম্বর 12/এ"
        assert convert_bengali_digits(raw) == expected

    def test_text_with_no_digits(self):
        """Strings without digits remain identical."""
        raw = "ধানমন্ডি ঢাকা বাংলাদেশ"
        assert convert_bengali_digits(raw) == raw

    def test_empty_and_none_input(self):
        """Empty or None input gracefully returns empty string."""
        assert convert_bengali_digits("") == ""
        assert convert_bengali_digits(None) == ""

    def test_reverse_conversion(self):
        """Verify ASCII to Bengali digit conversion."""
        ascii_str = "0123456789"
        expected = "০১২৩৪৫৬৭৮৯"
        assert convert_ascii_to_bengali_digits(ascii_str) == expected


# =========================================================================
# 2. Bangladeshi Mobile Number Sanitizer Tests
# =========================================================================
class TestPhoneNumberSanitization:
    """Tests for extracting & normalizing 11-digit Bangladeshi mobile numbers."""

    @pytest.mark.parametrize(
        "operator_code, dirty_input, expected_phone",
        [
            ("013 (GP/Skitto)", "01301234567", "01301234567"),
            ("013 with +880", "+8801301234567", "01301234567"),
            ("013 with Bengali", "০১৩০১২৩৪৫৬৭", "01301234567"),
            ("014 (Banglalink)", "01401234567", "01401234567"),
            ("014 with hyphens", "014-0123-4567", "01401234567"),
            ("015 (Teletalk)", "01501234567", "01501234567"),
            ("015 with spaces", "015 0123 4567", "01501234567"),
            ("016 (Robi/Airtel)", "01601234567", "01601234567"),
            ("016 with parens", "(+880) 1601234567", "01601234567"),
            ("017 (GP)", "01712345678", "01712345678"),
            ("017 with +880 and spaces", "+880 1712 345678", "01712345678"),
            ("017 with +88- prefix", "+88-01712345678", "01712345678"),
            ("017 with Bengali + hyphens", "+৮৮০১৭১২-৩৪৫৬৭৮", "01712345678"),
            ("018 (Robi)", "01812345678", "01812345678"),
            ("018 with 880 prefix", "8801812345678", "01812345678"),
            ("019 (Banglalink)", "01912345678", "01912345678"),
            ("019 formatted", "01912-34-56-78", "01912345678"),
        ],
    )
    def test_valid_operators_and_formats(self, operator_code, dirty_input, expected_phone):
        """All 7 operators (013-019) across diverse dirty formatting must resolve to clean 11 digits."""
        result = normalize_bd_phone(dirty_input)
        assert result == [expected_phone], f"Failed for {operator_code} with input '{dirty_input}'"

    @pytest.mark.parametrize(
        "invalid_input",
        [
            "01012345678",     # Invalid operator 010
            "01112345678",     # Discontinued operator 011 (Citycell)
            "01212345678",     # Invalid operator 012
            "017123456",       # Too short (9 digits)
            "0171234567",      # Too short (10 digits)
            "abcdefghijk",     # Non-numeric
            "",                # Empty string
            None,              # None
            "Phone: N/A",      # Text without digits
        ],
    )
    def test_invalid_numbers_rejected(self, invalid_input):
        """Invalid operators, short numbers, and non-numeric inputs must not yield valid phones."""
        result = normalize_bd_phone(invalid_input)
        assert result == []

    def test_embedded_in_banglish_order_dm(self):
        """Extract phone number accurately from realistic Banglish order messages."""
        text = "bhai delivery ta urgent pathan. address mirpur 10 road 5. mobile: +880 1711-223344. cod te dibo"
        result = normalize_bd_phone(text)
        assert result == ["01711223344"]

    def test_embedded_in_pure_bengali_order(self):
        """Extract phone number accurately from pure Bengali script messages."""
        text = "ভাইয়া আমার অর্ডার কনফার্ম করুন। ফোনঃ ০১৮৯৯-৮৮৭৭৬৬। ধানমন্ডি ৩২।"
        result = normalize_bd_phone(text)
        assert result == ["01899887766"]


# =========================================================================
# 3. Multi-Number Conflict Detector Tests
# =========================================================================
class TestMultiNumberConflictDetection:
    """Tests for detecting single numbers, duplicates, and multi-number conflicts."""

    def test_single_number_no_conflict(self):
        """A single valid phone number has no conflict."""
        text = "Amar number 01712345678"
        assert detect_multi_number_conflict(text) is False
        assert has_multi_number_conflict(text) is False
        assert detect_phone_conflicts(text) is False

        phone, conflict = extract_clean_phone(text)
        assert phone == "01712345678"
        assert conflict is False

    def test_duplicate_same_number_no_conflict(self):
        """Customer repeating the exact same number (e.g. for confirmation) is NOT a conflict."""
        text = "Call 01712345678 or 01712-345678 (same number)"
        # Both normalize to 01712345678
        assert detect_multi_number_conflict(text) is False

        phone, conflict = extract_clean_phone(text)
        assert phone == "01712345678"
        assert conflict is False

    def test_two_distinct_numbers_triggers_conflict(self):
        """Two different phone numbers in a single text block must trigger conflict."""
        text = "Amar number 01711223344, delivery boy na pele 01899887766 e call diben"
        assert detect_multi_number_conflict(text) is True
        assert has_multi_number_conflict(text) is True

        phone, conflict = extract_clean_phone(text)
        assert phone is None  # Blocked from automated dispatch
        assert conflict is True

    def test_three_numbers_triggers_conflict(self):
        """Three distinct numbers must trigger conflict."""
        text = "01711111111 / 01822222222 / 01933333333"
        assert detect_multi_number_conflict(text) is True
        phone, conflict = extract_clean_phone(text)
        assert phone is None
        assert conflict is True

    def test_bengali_and_ascii_different_numbers_triggers_conflict(self):
        """One Bengali numeral phone and one ASCII phone representing different numbers."""
        text = "ফোন ০১৭১১২২৩৩৪৪ অথবা 01899887766"
        assert detect_multi_number_conflict(text) is True
        phone, conflict = extract_clean_phone(text)
        assert phone is None
        assert conflict is True

    def test_no_numbers_found(self):
        """Text without any valid phone numbers returns None and no conflict."""
        text = "Ami Dhanmondi thaki, payment done"
        assert detect_multi_number_conflict(text) is False
        phone, conflict = extract_clean_phone(text)
        assert phone is None
        assert conflict is False


# =========================================================================
# 4. 3-Tier Delivery Boundary Detector & Fee Mapping Tests
# =========================================================================
class TestDeliveryZoneDetector:
    """Tests for 3-tier delivery boundary classification with explicit fee mapping."""

    # ---------------------------------------------------------------------
    # Tier 1: Suburbs (Fee: 100 BDT)
    # ---------------------------------------------------------------------
    @pytest.mark.parametrize(
        "suburb_address, expected_keyword",
        [
            ("Savar bus stand, Savar", "savar"),
            ("সাভার বাজার রোড", "সাভার"),
            ("Gazipur Chowrasta, Gazipur", "gazipur"),
            ("গাজীপুর শিববাড়ি মোড়", "গাজীপুর"),
            ("Tongi College Gate, Tongi", "tongi"),
            ("টঙ্গী স্টেশন রোড", "টঙ্গী"),
            ("Keraniganj babubazar bridge", "keraniganj"),
            ("কেরানীগঞ্জ কদমতলী", "কেরানীগঞ্জ"),
            ("Narayanganj Chashara", "narayanganj"),
            ("নারায়ণগঞ্জ ২ নং রেলগেট", "নারায়ণগঞ্জ"),
            ("Ashulia DEPZ gate, Ashulia", "ashulia"),
            ("আশুলিয়া বাজার", "আশুলিয়া"),
            ("Baipail, Savar", "baipail"),
            ("Board Bazar, Gazipur", "board bazar"),
            ("Joydebpur Sadar", "joydebpur"),
            ("Siddhirganj, Narayanganj", "siddhirganj"),
            ("Fatullah, Narayanganj", "fatullah"),
            ("Sonargaon, Narayanganj", "sonargaon"),
        ],
    )
    def test_suburbs_zone_and_fee(self, suburb_address, expected_keyword):
        """All suburb boundary towns map to Suburbs with explicit Fee of 100 BDT."""
        res = resolve_zone(suburb_address)
        assert res.fee == FEE_SUBURBS
        assert res.fee == 100
        assert res == ZONE_SUBURBS
        assert res == "Dhaka Suburbs"
        assert res == ("Suburbs", 100)

    def test_suburb_precedence_over_dhaka_core(self):
        """
        Critical Logistics Rule:
        Addresses like 'Savar, Dhaka' or 'Ashulia, Dhaka' physically reside in suburbs.
        The suburb detector must take precedence over Dhaka Core keywords.
        """
        addr1 = "House 12, Savar, Dhaka"
        addr2 = "Ashulia, Dhaka"
        addr3 = "Gazipur, Dhaka"
        addr4 = "Keraniganj, Dhaka, Bangladesh"

        for addr in (addr1, addr2, addr3, addr4):
            res = resolve_zone(addr)
            assert res.fee == 100, f"Address '{addr}' should be charged 100 BDT suburb rate"
            assert res == "Suburbs"

    # ---------------------------------------------------------------------
    # Tier 2: Dhaka Core (Inside Dhaka) (Fee: 70 BDT)
    # ---------------------------------------------------------------------
    @pytest.mark.parametrize(
        "core_address, expected_area",
        [
            ("Mirpur-10, Road 5, House 12", "mirpur"),
            ("মিরপুর ২ নম্বর সেক্টর", "মিরপুর"),
            ("Kazipara metro station, Mirpur", "kazipara"),
            ("Uttara Sector 7, Road 3", "uttara"),
            ("উত্তরা ৪ নম্বর সেক্টর", "উত্তরা"),
            ("Azampur, Uttara", "azampur"),
            ("Dhanmondi 27, Flat 4B", "dhanmondi"),
            ("ধানমন্ডি ৩২ নম্বর", "ধানমন্ডি"),
            ("Kalabagan 1st lane", "kalabagan"),
            ("Gulshan-2, Circle 2", "gulshan"),
            ("গুলশান ১ নম্বর", "গুলশান"),
            ("Niketan Block E, Gulshan", "niketan"),
            ("Banani 11, Road 18", "banani"),
            ("বনানী সুপার মার্কেট", "বনানী"),
            ("Mohammadpur Town Hall, Tajmahal Road", "mohammadpur"),
            ("মোহাম্মদপুর জাপান গার্ডেন সিটি", "মোহাম্মদপুর"),
            ("Lalmatia Block C", "lalmatia"),
            ("Merul Badda, DIT Project", "badda"),
            ("মধ্য বাড্ডা বাজার", "বাড্ডা"),
            ("Aftabnagar Block B", "aftabnagar"),
            ("Banasree Block C", "banasree"),
            ("Motijheel C/A, Dilkusha", "motijheel"),
            ("মতিঝিল শাপলা চত্বর", "মতিঝিল"),
            ("Farmgate green road", "farmgate"),
            ("Tejgaon I/A", "tejgaon"),
            ("Mohakhali DOHS", "mohakhali"),
            ("Bashundhara R/A Block D", "bashundhara"),
            ("বসুন্ধরা আবাসিক এলাকা", "বসুন্ধরা"),
            ("Wari, Rankin Street", "wari"),
            ("Lalbagh Fort road", "lalbagh"),
            ("Khilgaon Taltola", "khilgaon"),
            ("Jatrabari, Dhaka", "jatrabari"),
            ("Shantinagar Mor", "shantinagar"),
            ("House 10, Road 4, Dhaka", "dhaka"),
            ("ঢাকা ক্যান্টনমেন্ট", "ক্যান্টনমেন্ট"),
        ],
    )
    def test_dhaka_core_zone_and_fee(self, core_address, expected_area):
        """All central Dhaka core neighborhoods map to Inside Dhaka with Fee of 70 BDT."""
        res = resolve_zone(core_address)
        assert res.fee == FEE_DHAKA_CORE
        assert res.fee == 70
        assert res == ZONE_DHAKA_CORE
        assert res == "Dhaka Core"
        assert res == "Inside Dhaka"
        assert res == ("Inside Dhaka", 70)

    # ---------------------------------------------------------------------
    # Tier 3: Outside Dhaka (Fee: 130 BDT)
    # ---------------------------------------------------------------------
    @pytest.mark.parametrize(
        "outside_address, expected_district",
        [
            ("Chittagong GEC Mor, Nasirabad", "chittagong"),
            ("চট্টগ্রাম আগ্রাবাদ বাণিজ্যিক এলাকা", "চট্টগ্রাম"),
            ("Sylhet Zindabazar, Sylhet Sadar", "sylhet"),
            ("সিলেট আম্বরখানা", "সিলেট"),
            ("Rajshahi University Campus", "rajshahi"),
            ("রাজশাহী সাহেব বাজার", "রাজশাহী"),
            ("Khulna Khalishpur, Khulna", "khulna"),
            ("খুলনা শিববাড়ি মোড়", "খুলনা"),
            ("Barishal Sadar, Nathullabad", "barishal"),
            ("বরিশাল চকবাজার", "বরিশাল"),
            ("Rangpur Medical Mor", "rangpur"),
            ("রংপুর জাহাজ কোম্পানি মোড়", "রংপুর"),
            ("Mymensingh Ganginarpar", "mymensingh"),
            ("ময়মনসিংহ নতুন বাজার", "ময়মনসিংহ"),
            ("Comilla Kandirpar", "comilla"),
            ("কুমিল্লা কান্দিরপাড়", "কুমিল্লা"),
            ("Cox's Bazar Kolatoli Beach", "coxs bazar"),
            ("কক্সবাজার কলাতলী", "কক্সবাজার"),
            ("Bogra Satmatha, Bogura Sadar", "bogura"),
            ("বগুড়া সাতমাথা", "বগুড়া"),
            ("Jashore Kotwali, Jessore", "jashore"),
            ("Feni Sadar, Trunk Road", "feni"),
            ("Tangail Sadar, Tangail", "tangail"),
            ("Kushtia Court Station", "kushtia"),
            ("Noakhali Maijdee Court", "noakhali"),
        ],
    )
    def test_outside_dhaka_districts(self, outside_address, expected_district):
        """All other 63 districts of Bangladesh map to Outside Dhaka with Fee of 130 BDT."""
        res = resolve_zone(outside_address)
        assert res.fee == FEE_OUTSIDE_DHAKA
        assert res.fee == 130
        assert res == ZONE_OUTSIDE_DHAKA
        assert res == "Outside Dhaka"
        assert res == ("Outside Dhaka", 130)

    def test_outside_dhaka_default_fallback(self):
        """Unknown, missing, or unrecognized addresses default safely to Outside Dhaka fallback (130 BDT)."""
        unknown_addresses = [
            "Boro Gram, Post Office Shampur, Union 4",
            "Random Village without district name",
            "House 44, Somewhere unknown",
            "",
            None,
            "   ",
        ]
        for addr in unknown_addresses:
            res = resolve_zone(addr)
            assert res.fee == 130
            assert res == ZONE_OUTSIDE_DHAKA
            assert res == "Outside Dhaka"


# =========================================================================
# 5. Ergonomics & Result Object Tests
# =========================================================================
class TestDeliveryZoneResultErgonomics:
    """Verify polymorphic behaviors of DeliveryZoneResult."""

    def test_tuple_unpacking(self):
        """Supports `zone, fee = resolve_zone(address)`."""
        zone, fee = resolve_zone("Mirpur 10")
        assert zone == "Inside Dhaka"
        assert fee == 70

        zone, fee = resolve_zone("Savar")
        assert zone == "Suburbs"
        assert fee == 100

        zone, fee = resolve_zone("Sylhet")
        assert zone == "Outside Dhaka"
        assert fee == 130

    def test_attribute_access(self):
        """Supports `.zone`, `.fee`, `.tier`, `.name`, `.charge`."""
        res = resolve_zone("Uttara")
        assert res.zone == "Inside Dhaka"
        assert res.fee == 70
        assert res.name == "Inside Dhaka"
        assert res.tier == "Inside Dhaka"
        assert res.charge == 70

    def test_dict_access_and_serialization(self):
        """Supports `res['zone']`, `res['fee']`, and `.to_dict()`."""
        res = resolve_zone("Gazipur")
        assert res["zone"] == "Suburbs"
        assert res["fee"] == 100

        d = res.to_dict()
        assert d == {"zone": "Suburbs", "fee": 100, "matched_keyword": "gazipur"}

    def test_helper_get_delivery_fee(self):
        """Verify `get_delivery_fee(address)` returns integer fee directly."""
        assert get_delivery_fee("Gulshan 1") == 70
        assert get_delivery_fee("Tongi") == 100
        assert get_delivery_fee("Chittagong") == 130
        assert get_delivery_fee("Unknown Village") == 130


# =========================================================================
# 6. Composite Order Sanitizer Pipeline Tests
# =========================================================================
class TestCompositeOrderSanitizer:
    """End-to-end integration tests using `sanitize_order_payload`."""

    def test_clean_order_inside_dhaka(self):
        raw = "Bhai amar order ta confirm koren. Phone: 01712-345678, Address: Dhanmondi 27, Dhaka."
        payload = sanitize_order_payload(raw)
        assert payload["clean_phone"] == "01712345678"
        assert payload["has_phone_conflict"] is False
        assert payload["zone"] == "Inside Dhaka"
        assert payload["fee"] == 70
        assert payload["requires_human_review"] is False

    def test_suburb_order_with_bengali_numerals(self):
        raw = "সাভার বাস স্ট্যান্ড এর কাছে ডেলিভারি দিবেন। ফোনঃ ০১৮১২-৩৪৫৬৭৮"
        payload = sanitize_order_payload(raw)
        assert payload["clean_phone"] == "01812345678"
        assert payload["has_phone_conflict"] is False
        assert payload["zone"] == "Suburbs"
        assert payload["fee"] == 100
        assert payload["requires_human_review"] is False

    def test_multi_phone_conflict_flags_review(self):
        raw = "Phone 01711223344 or 01899887766. Mirpur 12."
        payload = sanitize_order_payload(raw)
        assert payload["clean_phone"] is None
        assert payload["has_phone_conflict"] is True
        assert payload["zone"] == "Inside Dhaka"
        assert payload["fee"] == 70
        assert payload["requires_human_review"] is True  # Flags needs_review in DB

