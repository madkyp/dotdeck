"""Escritor TOML mínimo para las definiciones de dots (tomllib solo lee).

Soporta lo que usa el formato: str, bool, int, float, listas de escalares,
tablas anidadas y arrays de tablas.
"""
from __future__ import annotations


def _str(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{out}"'


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _str(v)
    if isinstance(v, list):
        if not v:
            return "[]"
        items = [_scalar(x) for x in v]
        one = "[" + ", ".join(items) + "]"
        if len(one) <= 88:
            return one
        return "[\n" + "".join(f"  {i},\n" for i in items) + "]"
    raise TypeError(f"tipo no soportado en TOML: {type(v).__name__}")


def _is_table(v) -> bool:
    return isinstance(v, dict)


def _is_table_array(v) -> bool:
    return isinstance(v, list) and v and all(isinstance(x, dict) for x in v)


def dumps(data: dict, comments: dict[str, str] | None = None) -> str:
    """Serializa ``data``. ``comments`` mapea clave de primer nivel → comentario."""
    comments = comments or {}
    lines: list[str] = []
    _emit(data, [], lines, comments)
    return "\n".join(lines).strip() + "\n"


def _emit(d: dict, prefix: list[str], lines: list[str], comments: dict) -> None:
    for k, v in d.items():
        if v is None or _is_table(v) or _is_table_array(v):
            continue
        if not prefix and k in comments:
            lines.append(f"# {comments[k]}")
        lines.append(f"{_key(k)} = {_scalar(v)}")
    for k, v in d.items():
        path = prefix + [k]
        if _is_table(v):
            lines.append("")
            if not prefix and k in comments:
                lines.append(f"# {comments[k]}")
            lines.append(f"[{'.'.join(_key(p) for p in path)}]")
            _emit(v, path, lines, comments)
        elif _is_table_array(v):
            for item in v:
                lines.append("")
                lines.append(f"[[{'.'.join(_key(p) for p in path)}]]")
                _emit(item, path, lines, comments)


def _key(k: str) -> str:
    if k and all(c.isalnum() or c in "-_" for c in k):
        return k
    return _str(k)
