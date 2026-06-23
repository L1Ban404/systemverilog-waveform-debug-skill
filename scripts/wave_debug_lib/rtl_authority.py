from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from . import AUTHORITY_SCHEMA_VERSION


IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_$]*"
AUTHORITY_BACKEND = "internal-static-parser"
AUTHORITY_MATCH_STATUS = "static-source-match"
AUTHORITY_LIMITATIONS = (
    "conditional compilation and include expansion are not elaborated",
    "generate conditions and instance arrays are represented only when textually discoverable",
    "interfaces, packages, typedefs, and user-defined type widths may remain unresolved",
)


@dataclass(frozen=True)
class SignalDecl:
    name: str
    kind: str
    direction: str | None
    width: int | None
    range_text: str | None


@dataclass(frozen=True)
class InstanceDecl:
    module_type: str
    name: str
    named_overrides: dict[str, str]
    positional_overrides: tuple[str, ...]


@dataclass
class ModuleDecl:
    name: str
    source_file: str
    parameters: dict[str, int]
    parameter_order: tuple[str, ...]
    parameter_expressions: dict[str, str]
    signals: list[SignalDecl]
    instances: list[InstanceDecl]


def _without_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", text)


def _split_top_level(text: str, delimiter: str = ",") -> list[str]:
    rows: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character == '"':
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth = max(0, depth - 1)
        elif character == delimiter and depth == 0:
            rows.append(text[start:index].strip())
            start = index + 1
    rows.append(text[start:].strip())
    return [row for row in rows if row]


def _balanced(text: str, start: int) -> tuple[str, int]:
    if start >= len(text) or text[start] != "(":
        raise ValueError("expected '('")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1:index], index + 1
    raise ValueError("unterminated parenthesized declaration")


def _safe_int(expression: str, parameters: dict[str, int]) -> int | None:
    expression = expression.strip()
    expression = re.sub(
        r"(?i)(?:\d+)?'([s]?[bodh])([0-9a-f_xz?]+)",
        lambda match: str(int(match.group(2).replace("_", ""), {"b": 2, "o": 8, "d": 10, "h": 16}[match.group(1)[-1].lower()]))
        if not re.search(r"[xz?]", match.group(2), re.I) else "0",
        expression,
    )
    expression = re.sub(r"\$clog2\s*\(([^()]*)\)", lambda match: str(max(0, (value - 1).bit_length())) if (value := _safe_int(match.group(1), parameters)) is not None and value > 0 else "0", expression)
    try:
        node = ast.parse(expression, mode="eval")
    except SyntaxError:
        return None

    binary = {
        ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
        ast.Mult: lambda a, b: a * b, ast.FloorDiv: lambda a, b: a // b,
        ast.Div: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
        ast.LShift: lambda a, b: a << b, ast.RShift: lambda a, b: a >> b,
        ast.BitOr: lambda a, b: a | b, ast.BitAnd: lambda a, b: a & b,
        ast.BitXor: lambda a, b: a ^ b,
    }

    def evaluate(item: ast.AST) -> int:
        if isinstance(item, ast.Expression):
            return evaluate(item.body)
        if isinstance(item, ast.Constant) and isinstance(item.value, int):
            return item.value
        if isinstance(item, ast.Name) and item.id in parameters:
            return parameters[item.id]
        if isinstance(item, ast.BinOp) and type(item.op) in binary:
            return binary[type(item.op)](evaluate(item.left), evaluate(item.right))
        if isinstance(item, ast.UnaryOp) and isinstance(item.op, (ast.UAdd, ast.USub, ast.Invert)):
            value = evaluate(item.operand)
            return value if isinstance(item.op, ast.UAdd) else -value if isinstance(item.op, ast.USub) else ~value
        raise ValueError

    try:
        return int(evaluate(node))
    except (ArithmeticError, ValueError):
        return None


def _width(range_text: str | None, parameters: dict[str, int]) -> int | None:
    if not range_text:
        return 1
    dimensions = re.findall(r"\[\s*([^:\]]+)\s*:\s*([^\]]+)\]", range_text)
    if not dimensions:
        return 1
    result = 1
    for left_text, right_text in dimensions:
        left = _safe_int(left_text, parameters)
        right = _safe_int(right_text, parameters)
        if left is None or right is None:
            return None
        result *= abs(left - right) + 1
    return result


def _parameter_definitions(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in _split_top_level(text):
        match = re.search(rf"\b(?:parameter|localparam)\b.*?\b({IDENTIFIER})\s*=\s*(.+)$", part, re.DOTALL)
        if not match:
            match = re.search(rf"^\s*({IDENTIFIER})\s*=\s*(.+)$", part, re.DOTALL)
        if match:
            result[match.group(1)] = match.group(2).strip()
    return result


def _evaluate_parameter_definitions(definitions: dict[str, str]) -> tuple[dict[str, int], bool]:
    result: dict[str, int] = {}
    resolved = True
    for name, expression in definitions.items():
        value = _safe_int(expression, result)
        if value is None:
            resolved = False
        else:
            result[name] = value
    return result, resolved


def _declarations(text: str, parameters: dict[str, int], ansi: bool = False) -> list[SignalDecl]:
    if not ansi:
        result: list[SignalDecl] = []
        for statement in (row.strip() for row in text.split(";") if row.strip()):
            result.extend(_declarations(statement, parameters, ansi=True))
        return result
    parts = _split_top_level(text)
    result: list[SignalDecl] = []
    direction: str | None = None
    kind = "logic"
    range_text: str | None = None
    active = False
    for part in parts:
        declaration = re.search(r"\b(input|output|inout)\b", part)
        type_match = re.search(r"\b(wire|wand|wor|tri|tri0|tri1|uwire|reg|logic|bit|byte|shortint|int|longint|integer|time)\b", part)
        range_match = re.search(r"(?:\[[^\]]+\]\s*)+", part)
        if declaration:
            direction = declaration.group(1)
        if type_match:
            kind = type_match.group(1)
        elif declaration and direction == "input":
            kind = "wire"
        if range_match:
            range_text = range_match.group(0)
        elif declaration:
            range_text = None
        if declaration or type_match:
            active = True
        if not active:
            continue
        cleaned = re.sub(r"\b(?:input|output|inout|wire|wand|wor|tri|tri0|tri1|uwire|reg|logic|bit|signed|unsigned|var|const|byte|shortint|int|longint|integer|time)\b", " ", part)
        cleaned = re.sub(r"(?:\[[^\]]+\]\s*)+", " ", cleaned)
        cleaned = cleaned.split("=", 1)[0]
        names = re.findall(rf"\b({IDENTIFIER})\b", cleaned)
        if names:
            result.append(SignalDecl(names[-1], kind, direction, _width(range_text, parameters), range_text))
    return result


def _module_blocks(text: str) -> list[tuple[str, str, str, str]]:
    blocks: list[tuple[str, str, str, str]] = []
    pattern = re.compile(rf"\bmodule\s+(?:automatic\s+)?({IDENTIFIER})\b")
    for match in pattern.finditer(text):
        end_match = re.search(r"\bendmodule\b", text[match.end():])
        if not end_match:
            continue
        block_end = match.end() + end_match.start()
        cursor = match.end()
        while cursor < block_end and text[cursor].isspace():
            cursor += 1
        parameter_text = ""
        if cursor < block_end and text[cursor] == "#":
            cursor += 1
            while cursor < block_end and text[cursor].isspace():
                cursor += 1
            parameter_text, cursor = _balanced(text, cursor)
        while cursor < block_end and text[cursor].isspace():
            cursor += 1
        port_text = ""
        if cursor < block_end and text[cursor] == "(":
            port_text, cursor = _balanced(text, cursor)
        semicolon = text.find(";", cursor, block_end)
        if semicolon < 0:
            continue
        blocks.append((match.group(1), parameter_text, port_text, text[semicolon + 1:block_end]))
    return blocks


def parse_modules(files: list[Path]) -> dict[str, ModuleDecl]:
    modules: dict[str, ModuleDecl] = {}
    raw: list[tuple[ModuleDecl, str]] = []
    for path in files:
        text = _without_comments(path.read_text(encoding="utf-8", errors="ignore"))
        for name, parameter_text, port_text, body in _module_blocks(text):
            header_definitions = _parameter_definitions(parameter_text)
            body_definitions = _parameter_definitions(
                ",".join(re.findall(r"\b(?:localparam|parameter)\b[^;]*", body))
            )
            parameter_expressions = {**header_definitions, **body_definitions}
            parameters, _ = _evaluate_parameter_definitions(parameter_expressions)
            signals = _declarations(port_text, parameters, ansi=True)
            signals.extend(_declarations(body, parameters))
            unique: dict[str, SignalDecl] = {}
            for signal in signals:
                previous = unique.get(signal.name)
                unique[signal.name] = SignalDecl(
                    signal.name,
                    signal.kind,
                    signal.direction or (previous.direction if previous else None),
                    signal.width if signal.width is not None else (previous.width if previous else None),
                    signal.range_text or (previous.range_text if previous else None),
                )
            module = ModuleDecl(
                name,
                str(path.resolve()),
                parameters,
                tuple(header_definitions),
                parameter_expressions,
                list(unique.values()),
                [],
            )
            modules[name] = module
            raw.append((module, body))
    module_names = set(modules)
    for module, body in raw:
        module.instances = _parse_instances(body, module_names)
    return modules


def _parse_instances(body: str, module_names: set[str]) -> list[InstanceDecl]:
    instances: list[InstanceDecl] = []
    module_pattern = "|".join(re.escape(name) for name in sorted(module_names, key=len, reverse=True))
    if not module_pattern:
        return instances
    for match in re.finditer(rf"(?m)^\s*({module_pattern})\b", body):
        module_type = match.group(1)
        cursor = match.end()
        while cursor < len(body) and body[cursor].isspace():
            cursor += 1
        override_text = ""
        if cursor < len(body) and body[cursor] == "#":
            cursor += 1
            while cursor < len(body) and body[cursor].isspace():
                cursor += 1
            if cursor >= len(body) or body[cursor] != "(":
                continue
            try:
                override_text, cursor = _balanced(body, cursor)
            except ValueError:
                continue
        while cursor < len(body) and body[cursor].isspace():
            cursor += 1
        instance_match = re.match(IDENTIFIER, body[cursor:])
        if not instance_match:
            continue
        name = instance_match.group(0)
        cursor += instance_match.end()
        while cursor < len(body) and body[cursor].isspace():
            cursor += 1
        if cursor < len(body) and body[cursor] == "[":
            # An instance array needs elaboration to produce waveform paths such as u[0].
            # Omitting it is safer than publishing a plausible but incorrect scalar path.
            continue
        if cursor >= len(body) or body[cursor] != "(":
            continue
        named: dict[str, str] = {}
        positional: list[str] = []
        for part in _split_top_level(override_text):
            named_match = re.fullmatch(rf"\.({IDENTIFIER})\s*\((.*)\)", part, re.DOTALL)
            if named_match:
                named[named_match.group(1)] = named_match.group(2).strip()
            elif part:
                positional.append(part)
        instances.append(InstanceDecl(module_type, name, named, tuple(positional)))
    return instances


def _authority_rows(modules: dict[str, ModuleDecl], top: str) -> list[dict[str, object]]:
    if top not in modules:
        raise ValueError(f"top module is not declared in the selected RTL sources: {top}")
    rows: list[dict[str, object]] = []

    def resolve_parameters(module: ModuleDecl, overrides: dict[str, int]) -> tuple[dict[str, int], bool]:
        values: dict[str, int] = {}
        resolved = True
        for name, expression in module.parameter_expressions.items():
            if name in overrides:
                values[name] = overrides[name]
                continue
            value = _safe_int(expression, values)
            if value is None:
                resolved = False
                if name in module.parameters:
                    values[name] = module.parameters[name]
            else:
                values[name] = value
        return values, resolved

    def visit(
        module_name: str,
        instance_path: str,
        ancestry: tuple[str, ...],
        effective_parameters: dict[str, int] | None = None,
        parameters_resolved: bool = True,
    ) -> None:
        if module_name in ancestry:
            return
        module = modules[module_name]
        parameters, defaults_resolved = resolve_parameters(module, effective_parameters or {})
        parameters_resolved = parameters_resolved and defaults_resolved
        for signal in module.signals:
            width = _width(signal.range_text, parameters)
            rows.append({
                "full_signal_name": f"{instance_path}.{signal.name}",
                "module_type": module.name,
                "instance_path": instance_path,
                "local_signal_name": signal.name,
                "signal_kind": signal.kind,
                "direction": signal.direction,
                "decl_width_bits": width if width is not None else signal.width,
                "source_file": module.source_file,
                "provenance": AUTHORITY_BACKEND,
                "match_status": AUTHORITY_MATCH_STATUS,
                "confidence": "medium" if parameters_resolved and width is not None else "low",
            })
        for instance in module.instances:
            child = modules[instance.module_type]
            child_overrides: dict[str, int] = {}
            child_resolved = parameters_resolved
            for index, expression in enumerate(instance.positional_overrides):
                if index >= len(child.parameter_order):
                    child_resolved = False
                    continue
                value = _safe_int(expression, parameters)
                if value is None:
                    child_resolved = False
                else:
                    child_overrides[child.parameter_order[index]] = value
            for name, expression in instance.named_overrides.items():
                if name not in child.parameter_order:
                    child_resolved = False
                    continue
                value = _safe_int(expression, parameters)
                if value is None:
                    child_resolved = False
                else:
                    child_overrides[name] = value
            visit(
                instance.module_type,
                f"{instance_path}.{instance.name}",
                ancestry + (module_name,),
                child_overrides,
                child_resolved,
            )

    visit(top, top, ())
    return rows


def _identity(files: list[Path], top: str) -> dict[str, object]:
    sources = []
    for path in files:
        sources.append({"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"engine": f"{AUTHORITY_BACKEND}-v2", "top": top, "sources": sources}


def build_rtl_authority(files: list[Path], top: str, output: Path, force: bool = False) -> None:
    identity = _identity(files, top)
    metadata = output / "cache_meta.json"
    required = [output / name for name in ("rtl_authority.sqlite3", "rtl_authority_table.json", "rtl_authority_index.json")]
    if not force and metadata.is_file() and all(path.is_file() for path in required):
        try:
            if json.loads(metadata.read_text(encoding="utf-8")) == identity:
                return
        except json.JSONDecodeError:
            pass
    modules = parse_modules(files)
    rows = _authority_rows(modules, top)
    output.mkdir(parents=True, exist_ok=True)
    authority_info = {
        "backend": AUTHORITY_BACKEND,
        "match_status": AUTHORITY_MATCH_STATUS,
        "limitations": list(AUTHORITY_LIMITATIONS),
    }
    table = {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "top": top,
        "authority": authority_info,
        "signals": rows,
    }
    index = {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "top": top,
        "authority": authority_info,
        "signals": {str(row["full_signal_name"]): row for row in rows},
    }
    (output / "rtl_authority_table.json").write_text(json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "rtl_authority_index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    database = output / "rtl_authority.sqlite3"
    temporary = database.with_suffix(".sqlite3.tmp")
    temporary.unlink(missing_ok=True)
    with sqlite3.connect(temporary) as connection:
        connection.execute(
            "create table authority_lookup (full_signal_name text primary key, module_type text not null, "
            "instance_path text not null, local_signal_name text not null, signal_kind text, direction text, "
            "decl_width_bits integer, source_file text, provenance text not null, "
            "match_status text not null, confidence text not null)"
        )
        connection.executemany(
            "insert into authority_lookup values (:full_signal_name, :module_type, :instance_path, "
            ":local_signal_name, :signal_kind, :direction, :decl_width_bits, :source_file, :provenance, "
            ":match_status, :confidence)", rows,
        )
        connection.execute("create index authority_instance_path on authority_lookup(instance_path)")
        connection.execute("create table authority_metadata (key text primary key, value text not null)")
        connection.executemany(
            "insert into authority_metadata values (?, ?)",
            (
                ("schema_version", AUTHORITY_SCHEMA_VERSION),
                ("backend", AUTHORITY_BACKEND),
                ("match_status", AUTHORITY_MATCH_STATUS),
                ("limitations", json.dumps(AUTHORITY_LIMITATIONS)),
            ),
        )
    temporary.replace(database)
    metadata.write_text(json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8")
