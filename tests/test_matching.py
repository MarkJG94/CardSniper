import pytest

from cardsniper.matching import detect_finish, exclusion_reason, norm


def test_norm():
    assert norm("Urza's Saga") == "urzas saga"
    assert norm("Æther Vial") == "aether vial"
    assert norm("Fire // Ice") == "fire ice"
    assert norm("Lim-Dûl's Vault") == "lim duls vault"


@pytest.mark.parametrize("title,reason", [
    ("Ragavan Nimble Pilferer MH2 PSA 10", "graded card"),
    ("Ragavan proxy card", "not a genuine single"),
    ("Opt x4 playset", "multiple cards"),
    ("Force of Will Japanese EMA", "not English"),
    ("ラガバン", "not English"),
    ("Ragavan, Nimble Pilferer MH2 NM", None),
])
def test_exclusions(title, reason):
    assert exclusion_reason(title) == reason


def test_detect_finish():
    assert detect_finish("Sheoldred Foil NM") == "foil"
    assert detect_finish("Sheoldred Non-Foil NM") == "nonfoil"
    assert detect_finish("Counterspell foil etched") == "etched"
    assert detect_finish("Sheoldred NM") == "nonfoil"


def test_structured_shop_title(index):
    m, why = index.match("Sheoldred, the Apocalypse [Dominaria United]")
    assert why is None
    assert m.display_name == "Sheoldred, the Apocalypse"
    assert m.set_matched
    assert all(c.set_code == "dmu" for c in m.candidates)


def test_treatment_narrows_printing(index):
    m, _ = index.match("Sheoldred, the Apocalypse (Borderless) [Dominaria United]")
    assert m.unique is not None
    assert "borderless" in m.unique.treatments


def test_set_code_in_ebay_title(index):
    m, _ = index.match("MTG Ragavan, Nimble Pilferer MH2 Near Mint")
    assert m.set_matched
    assert {c.set_code for c in m.candidates} == {"mh2"}


def test_ambiguous_uses_cheapest_printing(index):
    m, _ = index.match("Ragavan Nimble Pilferer mtg card NM")
    assert not m.set_matched
    ref, card = m.reference_eur()
    priced = [c.eur for c in m.candidates if c.eur]
    assert ref == min(priced)


def test_front_face_name_matches_adventure(index):
    m, _ = index.match("Bonecrusher Giant ELD NM")
    assert m.display_name == "Bonecrusher Giant // Stomp"
    assert m.set_matched


def test_split_card(index):
    m, _ = index.match("Fire // Ice [Modern Horizons 2]")
    assert m.display_name == "Fire // Ice"
    assert m.set_matched


def test_foil_filter(index):
    m, _ = index.match("The One Ring foil [The Lord of the Rings: Tales of Middle-earth]")
    assert m.finish == "foil"
    assert all("foil" in c.finishes for c in m.candidates)


def test_collector_number_with_leading_zeros(index):
    m, _ = index.match("The One Ring LTR 0451 borderless #0451")
    assert m.unique is not None and m.unique.collector_number == "451"


def test_name_hint_preferred(index):
    m, _ = index.match("Opt - Near Mint", name_hint="Opt")
    assert m.display_name == "Opt"


def test_unknown_card(index):
    m, why = index.match("Totally Not A Card")
    assert m is None and why == "no card name recognised"


def _ref(id, name, set_name, set_code, number="1", finishes=("nonfoil", "foil"), eur=1.0):
    from cardsniper.matching import CardRef
    return CardRef(id=id, name=name, name_norm=norm(name), set_code=set_code, set_name=set_name,
                   set_norm=norm(set_name), collector_number=number, finishes=finishes,
                   treatments=frozenset(), eur=eur, eur_foil=eur)


def test_words_in_card_or_set_names_are_not_junk():
    from cardsniper.matching import CardIndex
    idx = CardIndex([_ref("a", "Order of Midnight // Alter Fate", "Throne of Eldraine", "eld"),
                     _ref("b", "Dead Ringers", "Mystery Booster 2", "mb2"),
                     _ref("c", "Booster Tutor", "Unstable", "ust")])
    assert idx.match("Order of Midnight // Alter Fate [Throne of Eldraine]")[0].unique.id == "a"
    assert idx.match("Dead Ringers [Mystery Booster 2]")[0].unique.id == "b"
    assert idx.match("Booster Tutor UST NM")[0].unique.id == "c"
    assert idx.match("Dead Ringers x4 playset")[1] == "multiple cards"
    assert idx.match("Booster Tutor altered art")[1] == "not a genuine single"


def test_exact_hint_beats_longer_name():
    from cardsniper.matching import CardIndex
    idx = CardIndex([_ref("s", "Sleep", "Magic 2010", "m10"), _ref("m", "Sleep Magic", "Final Fantasy", "fin")])
    assert idx.match("Sleep [Magic 2010]", name_hint="Sleep", hint_exact=True)[0].unique.id == "s"
    # an eBay search hint is not authoritative: "Sleep Magic" is the more specific name
    assert idx.match("Sleep Magic FIN NM", name_hint="Sleep")[0].unique.id == "m"


def test_foil_only_printing_without_foil_in_title(index):
    m, _ = index.match("Ugin, Eye of the Storms [Tarkir: Dragonstorm] #409")
    assert m.unique is not None and m.unique.collector_number == "409"
    assert m.finish == "foil"


def test_explicit_non_foil_is_respected(index):
    # #409 only exists in foil, so a listing that insists it's non-foil can't be identified
    m, why = index.match("Ugin, Eye of the Storms [Tarkir: Dragonstorm] #409 non-foil")
    assert m is None and why.startswith("no nonfoil printing")
