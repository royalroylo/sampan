import uuid

PREFIX_MIN = 8  # shortest id prefix that names a session


def new_id():
    return str(uuid.uuid4())


def short(id):
    return id[:PREFIX_MIN]


def resolve(rows, ref):
    """The row that ref names: by name, else by full id, else by a unique id prefix of PREFIX_MIN+ characters.
    Returns the row, or an error string (zh-TW). A row without an id never matches by id."""
    for r in rows:
        if r.tmux == ref:
            return r
    with_id = [r for r in rows if r.id]
    for r in with_id:
        if r.id == ref:
            return r
    hits = [r for r in with_id if len(ref) >= PREFIX_MIN and r.id.startswith(ref)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return f"「{ref}」對到多個 session：" + "、".join(f"{r.tmux}（{short(r.id)}）" for r in hits)
    return f"找不到 session「{ref}」"
