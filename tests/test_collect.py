import collect


def test_collect_roundtrip(tmp_path):
    c = collect.Collect(tmp_path / "c.db")
    assert c.add(1, "перший") == 1 and c.add(1, "другий", True) == 2 and c.add(2, "чужий") == 1
    assert c.joined(1).startswith("перший") and "чужий" not in c.joined(1)
    assert c.docx(1)[:2] == b"PK" and b"\xd0" in c.txt(1)
    c.pop_last(1)
    assert c.count(1) == 1
    c.clear(1)
    assert c.count(1) == 0
    assert collect.chunks("а" * 9000, 3900) and all(len(x) <= 3900 for x in collect.chunks("а\n" * 5000))
