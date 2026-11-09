"""Checks that every user-visible English template in the package has a Persian entry
with the same {placeholders}, and that no Persian text lives outside i18n.py.

Run:  python -X utf8 tools/check_i18n.py      (exit code 1 on any problem)

Templates are collected from: tr()/trn()/N_() calls with literal arguments, set_note(),
InstallError()/Cancelled(), the string literals of uninstaller.py that look like messages,
and leftovers.REASONS / guard.REASONS. N_() is the "mark for translation" no-op used where
a table of templates is translated later (tr(TABLE[key]))."""
from __future__ import annotations

import ast
import re
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from upstream import guard, leftovers   # noqa: E402
from upstream.i18n import FA            # noqa: E402

CALLS = {"tr", "trn", "N_", "set_note", "InstallError", "Cancelled"}
# Constructors / calls whose positional arguments (by index) or keyword arguments are English templates.
POSITIONS = {"Outcome": (1,), "Verdict": (2,), "Decision": (1, 3), "ask": (0,), "progress": (0,)}
KEYWORDS = {"ask": ("detail", "yes", "no"), "Outcome": ("template",), "Verdict": ("template",),
            "Decision": ("template", "detail"), "Question": ("template", "detail", "yes", "no")}
MESSAGE = re.compile(r"^[A-Z{][^\n]*\s[^\n]*$")


def fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


def docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def is_log_call(node: ast.Call) -> bool:
    f = node.func
    return isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ("log", "logging")


def collect() -> tuple[dict[str, list[str]], list[str]]:
    found: dict[str, list[str]] = {}
    problems: list[str] = []

    def add(text: str, where: str):
        if not text.strip():
            return
        found.setdefault(text, []).append(where)

    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text("utf-8"))
        skip = docstrings(tree)
        log_args = {id(a) for n in ast.walk(tree) if isinstance(n, ast.Call) and is_log_call(n) for a in n.args}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else \
                    node.func.attr if isinstance(node.func, ast.Attribute) else ""
                for index in POSITIONS.get(name, ()):
                    if len(node.args) > index:
                        arg = node.args[index]
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            add(arg.value, f"{rel}:{arg.lineno}")
                for kw in node.keywords:
                    if kw.arg in KEYWORDS.get(name, ()) and isinstance(kw.value, ast.Constant)                             and isinstance(kw.value.value, str):
                        add(kw.value.value, f"{rel}:{kw.value.lineno}")
                if name in CALLS and node.args:
                    count = 2 if name == "trn" else 1
                    for arg in node.args[:count]:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            add(arg.value, f"{rel}:{arg.lineno}")
                        elif isinstance(arg, ast.JoinedStr):
                            problems.append(f"{rel}:{arg.lineno}: f-string given to {name}() cannot be translated")
            if (path.name == "uninstaller.py" and isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in skip and id(node) not in log_args and MESSAGE.match(node.value)):
                add(node.value, f"{rel}:{node.lineno}")
    for text in (*leftovers.REASONS, *guard.REASONS):
        add(text, "leftovers/guard REASONS")
    return found, problems


def persian_outside_i18n() -> list[str]:
    """Persian letters in code or comments; docstrings may quote an example."""
    out = []
    for path in sorted(ROOT.rglob("*.py")):
        if path.name == "i18n.py" or "translations" in path.parts or "tools" in path.parts:
            continue
        text = path.read_text("utf-8")
        quoted = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body                     and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                quoted.update(range(node.body[0].lineno, node.body[0].end_lineno + 1))
        for number, line in enumerate(text.splitlines(), 1):
            if number not in quoted and re.search(r"[؀-ۿ]", line):
                out.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {line.strip()[:70]}")
    return out


def zwnj_warnings() -> list[str]:
    patterns = [(r"(?<![\w‌])(ن?می) (?=[؀-ۿ])", "«می» followed by a space; use می‌ with ZWNJ"),
                (r"[؀-ۿ] (ها|های|هایی|ترین|تر)(?![؀-ۿ])", "plural/comparative suffix after a space"),
                (r"[كي]", "Arabic kaf/yeh instead of Persian ک/ی"),
                (r"[٠-٩]", "Arabic-Indic digits; use Persian ۰-۹"),
                (r"^[A-Za-z][\w.]*(?= [؀-ۿ])", "starts with a Latin word: a label lays the whole sentence out "
                                                       "left-to-right; open with a Persian word")]
    out = []
    for key, value in FA.items():
        for pattern, why in patterns:
            if re.search(pattern, value):
                out.append(f"{why}: {value[:80]}")
    return out


def duplicate_translations() -> list[str]:
    """A key in two places is a silent override: i18n.FA.update(parts) lets a part beat the base dict, and a dict
    literal keeps its last value. Reports a key written twice in one literal, or in both the base dict and a part."""
    from upstream.translations import PARTS

    def literal(path: Path) -> list[str]:
        for node in ast.parse(path.read_text("utf-8")).body:
            value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
            if isinstance(value, ast.Dict) and (node.target if isinstance(node, ast.AnnAssign) else node.targets[0]).id == "FA":
                return [k.value for k in value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        return []

    out = []
    base = literal(ROOT / "upstream" / "i18n.py")
    owners: dict[str, str] = {key: "i18n.py" for key in base}
    for name, keys in (("i18n.py", base), *((p.__name__.rsplit(".", 1)[-1] + ".py", literal(Path(p.__file__))) for p in PARTS)):
        for key in sorted({k for k in keys if keys.count(k) > 1}):
            out.append(f"key written twice in {name}: {key!r}")
        for key in keys:
            if key in owners and owners[key] != name:
                out.append(f"key defined in both {owners[key]} and {name}: {key!r}")
            owners.setdefault(key, name)
    return sorted(set(out))


def main() -> int:
    found, problems = collect()
    problems += duplicate_translations()
    missing = sorted(t for t in found if t not in FA)
    mismatched = sorted(t for t in found if t in FA and fields(t) != fields(FA[t]))
    for t in missing:
        problems.append(f"missing Persian entry: {t!r}  <- {found[t][0]}")
    for t in mismatched:
        problems.append(f"placeholders differ: {t!r} {sorted(fields(t))} vs {sorted(fields(FA[t]))}")
    for line in persian_outside_i18n():
        problems.append(f"Persian text outside i18n.py: {line}")
    warnings = zwnj_warnings()
    unused = sorted(t for t in FA if t not in found)
    print(f"{len(found)} templates in code, {len(FA)} Persian entries, {len(missing)} missing, "
          f"{len(mismatched)} with different placeholders")
    for p in problems:
        print("PROBLEM", p)
    for w in warnings:
        print("WARN", w)
    if "--unused" in sys.argv:
        for t in unused:
            print("unused", repr(t))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
