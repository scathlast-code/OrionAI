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


def test_batches_boundaries(tmp_path):
    c = collect.Collect(tmp_path / "b.db")
    b = collect.Batches(c)
    for i in range(5):
        c.add(1, "x" * 6000, True)
    assert b.pending_chars(1) == 30000
    items = b.take(1)
    assert len(items) == 3  # до 20000 знаків від першого непідсумованого
    b.mark(1, items, "авто", True)
    c.add(1, "нове під час написання", True)  # прийшло, поки писалось зведення — не губиться
    assert len(b.pending(1)) == 3
    assert len(b.autos_since_manual(1)) == 1
    b.mark(1, b.take(1), "ручне", False)
    assert not b.autos_since_manual(1) and not b.pending(1)
    assert collect.CMD_RE.match("готуй зведення") and collect.CMD_RE.match("Напиши новину") and not collect.CMD_RE.match("як готувати борщ")
