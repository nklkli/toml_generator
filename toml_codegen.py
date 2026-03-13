"""
toml_codegen.py
---------------
Reads a *.toml file and generates:
  1. Typed dataclasses (one per table / nested table / array-of-tables)
  2. A `parse_<stem>()` function that deserialises the TOML into those objects

Handles:
  - [single.table]
  - [[array.of.tables]]
  - inline dicts with uniform schema -> dict[str, ItemConfig]

Usage:
    python toml_codegen.py config.toml            # prints to stdout
    python toml_codegen.py config.toml -o out.py  # writes to file
"""


import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        sys.exit(
            "ERROR: Python < 3.11 detected and 'tomli' is not installed.\nRun: pip install tomli")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_class_name(dotted_key: str) -> str:
    segments = dotted_key.split(".")
    words = []
    for seg in segments:
        words.extend(part.capitalize() for part in seg.split("_"))
    return "".join(words) + "Config"


def _is_array_of_tables(value: Any) -> bool:
    """[[double-bracket]]: non-empty list whose items are all dicts."""
    return isinstance(value, list) and bool(value) and isinstance(value[0], dict)


def _dict_schema(d: dict[str, Any]) -> frozenset[tuple[str, str]]:
    """Return a frozenset of (key, type_name) pairs that describes a dict's shape."""
    return frozenset((k, type(v).__name__) for k, v in d.items())


def _is_homogeneous_map(value: Any) -> bool:
    """True when *value* is a dict whose every value is a dict with the same schema.

    Example:
        DE = {name="Deutschland", code="0088"}
        BE = {name="Belgium",     code="3434"}
    -> all values are dicts, all share the same keys+types.
    """
    if not isinstance(value, dict) or not value:
        return False
    inner_values = list(value.values())
    if not all(isinstance(v, dict) for v in inner_values):
        return False
    first_schema = _dict_schema(inner_values[0])
    return all(_dict_schema(v) == first_schema for v in inner_values[1:])


def _py_scalar_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        if not value:
            return "list[Any]"
        return f"list[{_py_scalar_type(value[0])}]"
    return "Any"


# ---------------------------------------------------------------------------
# Field kinds
# ---------------------------------------------------------------------------
SCALAR = "scalar"       # plain key=value
NESTED = "nested"       # [single.table]
AOT = "aot"          # [[array.of.tables]]
HMAP = "hmap"         # dict[str, ItemConfig]  (homogeneous inline-table map)


class ClassSpec:
    def __init__(self, class_name: str, dotted_path: str):
        self.class_name = class_name
        self.dotted_path = dotted_path
        # (field_name, type_str, kind)
        self.fields: list[tuple[str, str, str]] = []

    def add_scalar(self, key: str, value: Any) -> None:
        self.fields.append((key, _py_scalar_type(value), SCALAR))

    def add_nested(self, key: str, child_class_name: str) -> None:
        self.fields.append((key, child_class_name, NESTED))

    def add_aot(self, key: str, item_class_name: str) -> None:
        self.fields.append((key, f"list[{item_class_name}]", AOT))

    def add_hmap(self, key: str, item_class_name: str) -> None:
        self.fields.append((key, f"dict[str, {item_class_name}]", HMAP))


# ---------------------------------------------------------------------------
# Tree walk
# ---------------------------------------------------------------------------

def _collect_classes(data: dict[str, Any], path: str, specs: list[ClassSpec]) -> ClassSpec:
    class_name = _to_class_name(path) if path else "RootConfig"
    spec = ClassSpec(class_name, path)

    for key, value in data.items():
        child_path = f"{path}.{key}" if path else key

        if _is_array_of_tables(value):
            # [[array.of.tables]] — use first element as item schema
            item_spec = _collect_classes(value[0], child_path, specs)
            spec.add_aot(key, item_spec.class_name)

        elif _is_homogeneous_map(value):
            # DE = {name=..., code=...} / BE = {name=..., code=...}
            # Generate ONE shared item class named after the key (singular)
            first_item = next(iter(value.values()))
            # Use child_path so the item class name is e.g. MappingsEntryConfig
            item_path = child_path + ".Entry"
            item_spec = _collect_classes(first_item, item_path, specs)
            spec.add_hmap(key, item_spec.class_name)

        elif isinstance(value, dict):
            child_spec = _collect_classes(value, child_path, specs)
            spec.add_nested(key, child_spec.class_name)

        else:
            spec.add_scalar(key, value)

    specs.append(spec)
    return spec


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------

_HEADER = '''\
"""
Auto-generated by toml_codegen.py
Source: {source}
Do not edit manually - re-run the generator to refresh.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]  pip install tomli

'''


def _emit_dataclass(spec: ClassSpec) -> str:
    lines = ["@dataclass", f"class {spec.class_name}:"]
    if not spec.fields:
        lines.append("    pass")
    else:
        for fname, ftype, _ in spec.fields:
            lines.append(f"    {fname}: {ftype}")
    return "\n".join(lines)


def _emit_parser(root_spec: ClassSpec, all_specs: list[ClassSpec], stem: str) -> str:

    def _find_spec(class_name: str) -> ClassSpec | None:
        return next((s for s in all_specs if s.class_name == class_name), None)

    def _build_expr(data_expr: str, spec: ClassSpec, indent: int) -> list[str]:
        pad = "    " * indent
        lines = [f"{pad}{spec.class_name}("]

        for fname, ftype, kind in spec.fields:

            if kind == AOT:
                inner_cls = ftype[len("list["):-1]
                lines.append(
                    f"{pad}    {fname}=[\n"
                    f"{pad}        {inner_cls}(**_item)\n"
                    f"{pad}        for _item in {data_expr}[{fname!r}]\n"
                    f"{pad}    ],"
                )

            elif kind == HMAP:
                inner_cls = ftype[len("dict[str, "):-1]
                lines.append(
                    f"{pad}    {fname}={{\n"
                    f"{pad}        _k: {inner_cls}(**_v)\n"
                    f"{pad}        for _k, _v in {data_expr}[{fname!r}].items()\n"
                    f"{pad}    }},"
                )

            elif kind == NESTED:
                child_spec = _find_spec(ftype)
                if child_spec:
                    sub = "\n".join(_build_expr(
                        f"{data_expr}[{fname!r}]", child_spec, indent + 2))
                    lines.append(f"{pad}    {fname}=(\n{sub}\n{pad}    ),")
                else:
                    lines.append(f"{pad}    {fname}={data_expr}[{fname!r}],")

            else:  # SCALAR
                lines.append(f"{pad}    {fname}={data_expr}[{fname!r}],")

        lines.append(f"{pad})")
        return lines

    inner = "\n".join(_build_expr("data", root_spec, 2))
    fn_name = f"parse_{stem.replace('-', '_')}"

    return (
        f'def {fn_name}(path: str | Path = "{stem}.toml") -> {root_spec.class_name}:\n'
        f'    """Load *path* and return a fully-typed {root_spec.class_name}."""\n'
        f'    with open(path, "rb") as fh:\n'
        f'        data = tomllib.load(fh)\n'
        f'    return (\n'
        f'{inner}\n'
        f'    )\n'
    )


def generate(toml_path: Path) -> str:
    with open(toml_path, "rb") as fh:
        data = tomllib.load(fh)

    specs: list[ClassSpec] = []
    root_spec = _collect_classes(data, "", specs)

    parts = [_HEADER.format(source=toml_path.name)]
    for spec in specs:
        parts.append(_emit_dataclass(spec))
        parts.append("")

    parts.append("")
    parts.append(_emit_parser(root_spec, specs, toml_path.stem))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate dataclasses from a TOML file.")
    parser.add_argument("toml", help="Path to the .toml file")
    parser.add_argument(
        "-o", "--output", help="Write output to this .py file (default: stdout)")
    args = parser.parse_args()

    toml_path = Path(args.toml)
    if not toml_path.exists():
        sys.exit(f"ERROR: file not found: {toml_path}")

    code = generate(toml_path)

    if args.output:
        Path(args.output).write_text(code, encoding="utf-8")
        print(f"Written to {args.output}")
    else:
        print(code)


if __name__ == "__main__":
    main()
