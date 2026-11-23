from pathlib import Path

from picnic_service.schema import MASK, build_schema, mask_example, personal_values

RAW = Path(__file__).parent / "fixtures" / "raw"


def test_personal_values_from_user_dump():
    values = personal_values(RAW)
    assert {"Testvoornaam", "Voorbeeldstad", "test@example.org"} <= values


def test_mask_by_key_pattern_and_value():
    personal = {"Testvoornaam"}
    assert mask_example("street", "Wat dan ook", personal) == MASK
    assert mask_example("note", "mail naar iemand@example.org", personal) == MASK
    assert mask_example("note", "bel 06 12345678", personal) == MASK
    assert mask_example("note", "Hoi Testvoornaam", personal) == MASK
    assert mask_example("name", "Halfvolle melk", personal) == "Halfvolle melk"
    assert mask_example("window_start", "2026-07-15T14:15:00.000+02:00", personal) == "2026-07-15T14:15:00.000+02:00"
    assert mask_example("mts", 1726491234000, personal) == "1726491234000"
    assert mask_example("account", "•••• 1234", personal) == MASK
    assert mask_example("redacted_phone_number", "+31 6*****123", personal) == MASK
    assert mask_example("note", "rekening •••• 1234", personal) == MASK
    assert mask_example("name", "Iemand", personal, "user.name") == MASK
    assert mask_example("id", "abc", personal, "address.id") == MASK
    assert mask_example("name", "Halfvolle melk", personal, "items[].name") == "Halfvolle melk"


def test_schema_contains_paths_and_no_personal_data():
    text = build_schema(RAW)
    assert "`address.city`" in text
    assert "`body.child.children[].sellingUnit.display_price`" in text
    assert "SELLING_UNIT_TILE" in text
    assert "search-results-block-<id>" in text
    for secret in ("Testvoornaam", "Testachternaam", "Voorbeeldstraat", "1234 AB", "test@example.org", "+31600000000"):
        assert secret not in text
