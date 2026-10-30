"""任务 5 姓名规范化与兼容等级（spec AC02）。预期值手写，不由实现生成。"""

from __future__ import annotations

import pytest

from etl_task5 import names
from etl_task5.names import level, parse, split_list

BAD = "�"   # 替换字符 U+FFFD


@pytest.mark.parametrize("raw, display, strict, extra", [
    ("Bogart, Humphrey", "Humphrey Bogart", "humphrey bogart", {"reversed": True}),
    ("Robert Downey, Jr.", "Robert Downey Jr.", "robert downey jr", {"reversed": False, "suffix": "Jr."}),
    ("George 'Gabby' Hayes", "George Hayes", "george hayes", {"nickname": "Gabby"}),
    ("George “Gabby” Hayes", "George Hayes", "george hayes", {"nickname": "Gabby"}),
    ("Robert Wilson (III)", "Robert Wilson", "robert wilson", {"disambiguator": "III"}),
    ("Robert Wilson (Actor)", "Robert Wilson", "robert wilson", {"paren_text": "Actor", "disambiguator": None}),
    ("Dr. Theodore Seuss Jr.", "Theodore Seuss Jr.", "theodore seuss jr", {"honorific": "Dr.", "suffix": "Jr."}),
    ("Jose Ferrer", "Jose Ferrer", "jose ferrer", {}),
    ("José Ferrer", "José Ferrer", "jose ferrer", {}),
    (f"Fran{BAD}ois Girard", f"Fran{BAD}ois Girard", "fran ois girard", {"encoding_damaged": True}),
    ("Peter O'Toole", "Peter O'Toole", "peter otoole", {"nickname": None}),
])
def test_ac02_parse_parts_and_strict_key(raw, display, strict, extra):
    n = parse(raw)
    assert n.raw == raw                                        # 原始姓名不改
    assert (n.display, n.strict) == (display, strict)
    for attr, value in extra.items():
        assert getattr(n, attr) == value, attr


def test_ac02_loose_key_drops_middle_initial():
    assert parse("Alan J. Pakula").loose == parse("Alan Pakula").loose == "alan pakula"
    assert parse("Alan J. Pakula").strict != parse("Alan Pakula").strict


def test_ac02_tokenset_key_ignores_order_but_strict_key_does_not():
    a, b = parse("Kim Yu-mi"), parse("Yu-mi Kim")
    assert a.tokenset == b.tokenset and a.strict != b.strict


def test_ac02_family_and_initial_keys():
    n = parse("Robert Downey, Jr.")
    assert (n.family, n.initial) == ("downey", "r downey")
    assert parse("J. Smith").initial == "j smith" and parse("John Smith").initial == "j smith"


def test_ac02_suffix_stays_in_strict_key_so_lon_chaney_jr_differs():
    assert parse("Lon Chaney Jr.").strict != parse("Lon Chaney").strict


def test_ac02_placeholder_names():
    assert all(parse(v).placeholder for v in ("Various", "Various Artists", "N/A", "Unknown", ""))
    assert not parse("Various Smith").placeholder


def test_ac02_imdb_primary_name_goes_through_the_same_function():
    assert parse("Humphrey Bogart").strict == parse("Bogart, Humphrey").strict


@pytest.mark.parametrize("a, b, expected", [
    ("Bogart, Humphrey", "Humphrey Bogart", "L1"),
    ("José Ferrer", "Jose Ferrer", "L1"),
    ("George 'Gabby' Hayes", "George Hayes", "L1"),
    ("Kim Yu-mi", "Yu-mi Kim", "L2"),
    ("Alan Pakula", "Alan J. Pakula", "L3"),
    ("Tommy Jones", "Tommy Lee Jones", "L3"),
    ("George Hayes", "George Gabby Hayes", "L3"),
    ("Lon Chaney", "Lon Chaney Jr.", "L3"),
    ("J. Smith", "John Smith", "LI"),
    ("Billy Bob Thorton", "Billy Bob Thornton", "L4"),
    (f"Fran{BAD}ois Girard", "François Girard", "L4"),
    ("John A. Smith", "John B. Smith", None),
    ("John Smith", "Jane Smith", None),
    ("Andy Wachowski", "Lana Wachowski", None),
    ("Various", "Various", None),
])
def test_ac02_level(a, b, expected):
    assert level(parse(a), parse(b)) == expected
    assert level(parse(b), parse(a)) == expected                # 对称


def test_ac02_level_maps_to_x_and_n_levels():
    assert [names.X_LEVEL[k] for k in ("L1", "L2", "L3", "LI", "L4")] == ["X1", "X2", "X3", "X4", "X4"]
    assert [names.N_LEVEL[k] for k in ("L1", "L2", "L3", "LI", "L4")] == ["N1", "N2", "N2", "N3", "N4"]


@pytest.mark.parametrize("value, expected, ambiguous", [
    ("Humphrey Bogart, Ingrid Bergman", ["Humphrey Bogart", "Ingrid Bergman"], False),
    ("Robert Downey, Jr., Bonnie Hunt", ["Robert Downey, Jr.", "Bonnie Hunt"], False),
    ("Arlt, Lewis, Brown, Blair", ["Arlt", "Lewis", "Brown", "Blair"], True),
    ("Clyde Geronimi", ["Clyde Geronimi"], False),
    ("Arlt, Lewis", ["Arlt", "Lewis"], True),
    ("A B, C D, E", ["A B", "C D", "E"], False),
])
def test_ac02_split_list(value, expected, ambiguous):
    assert split_list(value) == (expected, ambiguous)
