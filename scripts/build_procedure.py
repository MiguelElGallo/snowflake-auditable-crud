"""Generate ``sources/definitions/procedures.sql`` from the shared Python package.

The ``CORE.SP_DEV_API`` and ``CORE.SP_ADMIN_API`` procedures carry their code
inline. This script concatenates
the tested modules of ``auditable_crud`` (removing intra-package imports) into
the procedure body. ``tests/test_procedure_bundle.py`` fails if the committed
file is stale, so the procedure always runs exactly the code the tests cover.

Usage: ``uv run python scripts/build_procedure.py``
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "streamlit" / "common" / "auditable_crud"
OUTPUT = ROOT / "sources" / "definitions" / "procedures.sql"

# Dependency order: every module only uses names defined by modules before it.
MODULES = (
    "naming",
    "sql_guard",
    "state",
    "diff",
    "drift",
    "config",
    "runner",
    "store",
    "engine",
)
# Text that would break the $$-quoted body or be interpreted by DCM's Jinja.
FORBIDDEN_MARKERS = ("$$", "{{", "{%", "{#")
PACKAGES = "('snowflake-snowpark-python', 'sqlglot==30.17.0')"

HEADER = """\
-- GENERATED FILE - do not edit. Source: streamlit/common/auditable_crud/*.py
-- Regenerate with: uv run python scripts/build_procedure.py

-- Owner's-rights entry points for every state-changing operation, one per app:
-- the developer app calls SP_DEV_API, the admin app and the cleanup task call
-- SP_ADMIN_API. Both run the same bundled code; each accepts only its own
-- operations (engine.DEVELOPER_OPS / engine.ADMIN_OPS). See engine.dispatch.
"""

PROCEDURE = """
DEFINE PROCEDURE {{ app_db }}.CORE.%(name)s(OP VARCHAR, ARGS VARIANT)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = %(packages)s
HANDLER = 'handler'
COMMENT = 'Auditable CRUD %(purpose)s API (owner''s rights): %(ops)s'
EXECUTE AS OWNER
AS
$$
%(bundle)s

# --------------------------------------------------------------------- handler
_CORE_SCHEMA = '"{{ app_db }}"."CORE"'


def handler(session, op, args):
    \"\"\"Stored-procedure handler: dispatch one %(purpose)s operation with owner's rights.\"\"\"
    return dispatch(SnowparkRunner(session), _CORE_SCHEMA, op, args, %(ops_name)s)
$$;
"""

# (procedure name, purpose, engine constant holding its operations)
PROCEDURES = (
    ("SP_DEV_API", "developer", "DEVELOPER_OPS"),
    ("SP_ADMIN_API", "admin", "ADMIN_OPS"),
)


class BundleError(RuntimeError):
    """The package cannot be safely bundled into a procedure body."""


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _strip_module(name: str, source: str) -> tuple[str, set[str]]:
    """Remove ``__future__`` and intra-package imports; return code and defined names."""
    tree = ast.parse(source)
    drop: set[int] = set()
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module == "__future__" or module.startswith("auditable_crud"):
            if module.startswith("auditable_crud") and any(a.asname for a in node.names):
                raise BundleError(f"{name}.py: aliased package imports cannot be bundled")
            drop.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        elif any(a.name == "*" for a in node.names):
            raise BundleError(f"{name}.py: star imports cannot be bundled")
    top_level = set(map(id, tree.body))
    for nested in ast.walk(tree):
        if (
            isinstance(nested, ast.ImportFrom)
            and id(nested) not in top_level
            and (nested.module or "").startswith("auditable_crud")
        ):
            raise BundleError(f"{name}.py: package imports must be at module level")
    lines = [line for i, line in enumerate(source.splitlines(), 1) if i not in drop]
    return "\n".join(lines).strip() + "\n", _top_level_names(tree)


def build_bundle() -> str:
    """Return the Python source of the procedure body (without the handler)."""
    parts = ["from __future__ import annotations\n"]
    seen: dict[str, str] = {}
    for module in MODULES:
        code, names = _strip_module(module, (PACKAGE / f"{module}.py").read_text("utf-8"))
        clashes = {n: seen[n] for n in names if n in seen}
        if clashes:
            raise BundleError(f"{module}.py redefines names from other modules: {clashes}")
        seen.update(dict.fromkeys(names, module))
        parts.append(f"\n# {'=' * 66}\n# auditable_crud/{module}.py\n# {'=' * 66}\n{code}")
    bundle = "".join(parts)
    for marker in FORBIDDEN_MARKERS:
        if marker in bundle:
            raise BundleError(f"Bundled code contains {marker!r}, which breaks the definition")
    return bundle


def _engine_op_sets() -> dict[str, list[str]]:
    """Read the ``*_OPS`` frozenset literals from engine.py without importing it."""
    tree = ast.parse((PACKAGE / "engine.py").read_text("utf-8"))
    sets: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.endswith("_OPS") and isinstance(node.value, ast.Call):
                sets[name] = sorted(ast.literal_eval(node.value.args[0]))
    return sets


def render_definition() -> str:
    """Full contents of ``procedures.sql``."""
    bundle = build_bundle().rstrip("\n")
    op_sets = _engine_op_sets()
    parts = [HEADER]
    for name, purpose, ops_name in PROCEDURES:
        parts.append(
            PROCEDURE
            % {
                "name": name,
                "purpose": purpose,
                "ops": ", ".join(op_sets[ops_name]),
                "ops_name": ops_name,
                "packages": PACKAGES,
                "bundle": bundle,
            }
        )
    return "".join(parts)


def main() -> int:
    """Write the definition file; print where it went."""
    OUTPUT.write_text(render_definition(), "utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
