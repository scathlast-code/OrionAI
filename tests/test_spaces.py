import spaces


def test_spaces_flow(tmp_path):
    s = spaces.Spaces(tmp_path / "s.db")
    sid = s.create(1, "Редакція")
    assert s.is_member(sid, 1) and s.by_code(s.get(sid)["code"])["id"] == sid
    s.join(sid, 2)
    assert s.members(sid) == [1, 2] and [x["name"] for x in s.mine(2)] == ["Редакція"]
    t = s.add(sid, "task", "Зверстати", 2)
    s.toggle(t)
    assert s.items(sid, "task")[0]["done"] == 1
    old = s.get(sid)["code"]
    s.new_code(sid)
    assert s.by_code(old) is None
    s.leave(sid, 2)
    assert s.members(sid) == [1]
    s.delete(sid)
    assert s.get(sid) is None and not s.items(sid, "task")
