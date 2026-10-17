"""Valve KeyValues — text parse + binary shortcuts round-trip + shortcut appid."""

from collections import OrderedDict

from navig_games.engine.steam import vdf


def test_text_kv_nested():
    d = vdf.loads('"AppState"\n{\n "appid" "570"\n "name" "Dota 2"\n "u" { "a" "b" }\n}')
    assert d["AppState"]["name"] == "Dota 2"
    assert d["AppState"]["u"]["a"] == "b"


def test_text_kv_comments_and_bare_tokens():
    d = vdf.loads('// header comment\n"users" {\n  "76561" { "AccountName" "spacemonks" }\n}')
    assert d["users"]["76561"]["AccountName"] == "spacemonks"


def test_binary_roundtrip_preserves_fields():
    root = OrderedDict([("shortcuts", OrderedDict([
        ("0", OrderedDict([("appid", 12345), ("AppName", "Alpha"), ("Exe", '"C:/g/a.exe"'),
                           ("StartDir", '"C:/g"'), ("IsHidden", 0),
                           ("tags", OrderedDict([("0", "navig")]))])),
        ("1", OrderedDict([("appid", 67890), ("AppName", "Beta"), ("Exe", '"C:/g/b.exe"'),
                           ("tags", OrderedDict())])),
    ]))])
    back = vdf.read_binary(vdf.write_binary(root))
    assert back["shortcuts"]["0"]["AppName"] == "Alpha"
    assert back["shortcuts"]["0"]["Exe"] == '"C:/g/a.exe"'
    assert back["shortcuts"]["0"]["appid"] == 12345
    assert isinstance(back["shortcuts"]["0"]["IsHidden"], int)
    assert back["shortcuts"]["0"]["tags"]["0"] == "navig"
    assert back["shortcuts"]["1"]["AppName"] == "Beta"
    assert back["shortcuts"]["1"]["tags"] == {}  # empty tags map


def test_binary_magic_and_terminator():
    data = vdf.write_binary(OrderedDict([("shortcuts", OrderedDict())]))
    assert data.startswith(b"\x00shortcuts\x00")
    assert data.endswith(b"\x08\x08")  # end-of-shortcuts + end-of-root


def test_high_bit_int_roundtrips_stably_by_bytes():
    # A shortcut appid has the high bit set; bytes must survive a write→read→write.
    root = OrderedDict([("shortcuts", OrderedDict([
        ("0", OrderedDict([("appid", vdf.shortcut_appid('"x.exe"', "Game")), ("tags", OrderedDict())])),
    ]))])
    b1 = vdf.write_binary(root)
    b2 = vdf.write_binary(vdf.read_binary(b1))
    assert b1 == b2  # stable bytes despite signed/unsigned int interpretation


def test_shortcut_appid_deterministic_and_highbit():
    a = vdf.shortcut_appid('"x.exe"', "Game")
    assert a == vdf.shortcut_appid('"x.exe"', "Game")
    assert a & 0x80000000  # Steam sets the high bit
    assert vdf.shortcut_appid('"y.exe"', "Game") != a
