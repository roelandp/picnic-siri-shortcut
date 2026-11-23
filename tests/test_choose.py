from datetime import datetime, timedelta, timezone

from helpers import make_delivery

from picnic_service.choose import decide, name_matches, normalize, spoken_options, term_variants
from picnic_service.store import Store

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)
VOLLE = ("s1", "Picnic bio volle melk", "1 liter", 1, 149)
TONYS = ("s2", "Tony's Chocolonely melk", "180 gram", 1, 459)
OATLY = ("s3", "Oatly haverdrink barista", "1 liter", 2, 578)
EIEREN = ("s4", "Picnic scharreleieren", "10 stuks M", 1, 313)
BIO_EIEREN = ("s5", "Picnic bio eieren", "10 stuks", 1, 439)


def ago(days):
    return (NOW - timedelta(days=days)).isoformat()


def build(tmp_path, deliveries):
    store = Store(tmp_path / "picnic.db")
    for d in deliveries:
        store.upsert_delivery(d)
    store.recompute_derived()
    return store


def test_normalize_and_variants():
    assert normalize("Wat melk, graag!") == "melk"
    assert normalize("some oat milk") == "oat milk"
    assert ["haverdrink"] in term_variants("oat milk", "en")
    assert ["haverdrink"] in term_variants("havermelk", "nl")


def test_word_matching_respects_word_boundaries():
    assert name_matches("Coca-Cola zero", [["cola"]])
    assert not name_matches("Alpro soja dessert chocolade", [["cola"]])
    assert name_matches("Picnic snackworteltjes", [["wortels"]])
    assert name_matches("Karnemelk", [["melk"]])


def test_ambiguous_term_asks(tmp_path):
    store = build(tmp_path, [make_delivery(f"d{i}", ago(10 * i + 1), [VOLLE, TONYS]) for i in range(5)])
    decision = decide("melk", "nl", store, NOW)
    assert decision.kind == "choose"
    assert {o.article_id for o in decision.options} == {"s1", "s2"}
    assert all(o.label for o in decision.options)


def test_clear_winner_is_added(tmp_path):
    deliveries = [make_delivery(f"d{i}", ago(10 * i + 1), [EIEREN]) for i in range(9)]
    deliveries.append(make_delivery("dx", ago(200), [BIO_EIEREN]))
    store = build(tmp_path, deliveries)
    decision = decide("eieren", "nl", store, NOW)
    assert decision.kind == "winner"
    assert decision.article.article_id == "s4"


def test_english_term_uses_translation(tmp_path):
    deliveries = [make_delivery(f"d{i}", ago(10 * i + 1), [EIEREN]) for i in range(3)]
    store = build(tmp_path, deliveries)
    assert decide("eggs", "en", store, NOW).article.article_id == "s4"
    oat = build(tmp_path / "b", [make_delivery("o1", ago(3), [OATLY])])
    assert decide("oat milk", "en", oat, NOW).article.article_id == "s3"


def test_alias_beats_history(tmp_path):
    store = build(tmp_path, [make_delivery(f"d{i}", ago(10 * i + 1), [VOLLE, TONYS]) for i in range(5)])
    store.set_alias("milk", "s1", "Picnic bio volle melk", "1 liter")
    decision = decide("Milk", "en", store, NOW)
    assert decision.kind == "alias"
    assert decision.article.article_id == "s1"


def test_old_purchases_are_ignored(tmp_path):
    store = build(tmp_path, [make_delivery("old", ago(900), [VOLLE])])
    assert decide("melk", "nl", store, NOW).kind == "none"


def test_favorites_add_candidates(tmp_path):
    store = build(tmp_path, [make_delivery("d1", ago(5), [VOLLE])])
    store.replace_favorites([{"article_id": "s9", "name": "Picnic halfvolle melk", "unit_quantity": "1 liter", "price_cents": 119}])
    decision = decide("melk", "nl", store, NOW)
    # s1 heeft alle recente aankopen, dus winnaar; de favoriet staat wel in de kandidaten
    assert decision.kind == "winner"
    assert decision.article.article_id == "s1"
    only_fav = decide("halfvolle melk", "nl", store, NOW)
    assert only_fav.kind == "winner" and only_fav.article.favorite


OATLY_VOL = ("s6", "Oatly haverdrink vol", "1 liter", 1, 269)


def test_partial_translation_distinguishes_barista_and_regular(tmp_path):
    deliveries = [make_delivery(f"o{i}", ago(10 * i + 1), [OATLY, OATLY_VOL]) for i in range(4)]
    store = build(tmp_path, deliveries)
    assert ["haverdrink", "barista"] in term_variants("havermelk barista", "nl")
    assert ["barista", "haverdrink"] in term_variants("barista oat milk", "en")
    assert decide("havermelk", "nl", store, NOW).kind == "choose"
    assert decide("havermelk barista", "nl", store, NOW).article.article_id == "s3"
    assert decide("barista oat milk", "en", store, NOW).article.article_id == "s3"
    assert decide("gewone havermelk", "nl", store, NOW).article.article_id == "s6"
    assert decide("regular oat milk", "en", store, NOW).article.article_id == "s6"


def test_short_labels_say_only_the_difference(tmp_path):
    oatly_small = ("s8", "Oatly haverdrink barista", "500 ml", 1, 199)
    deliveries = [make_delivery(f"o{i}", ago(10 * i + 1), [OATLY, OATLY_VOL]) for i in range(4)]
    deliveries.append(make_delivery("small", ago(50), [oatly_small]))
    store = build(tmp_path, deliveries)
    decision = decide("havermelk", "nl", store, NOW)
    shorts = {o.article_id: o.short_label for o in decision.options}
    assert shorts == {"s6": "vol", "s3": "barista, 1 liter", "s8": "barista, 500 ml"}
    spoken = spoken_options(decision.options, "nl")
    assert spoken.count(" of ") == 1 and "vol" in spoken
    assert " or " in spoken_options(decision.options, "en")
