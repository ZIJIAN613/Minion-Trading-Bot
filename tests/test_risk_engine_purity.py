# tests/test_risk_engine_purity.py
#
# Static AST-scan smoke test for the risk_engine module.
#
# PURPOSE: Verify that no Python file in the risk_engine/ directory imports
# any forbidden module, references any forbidden symbol, or invokes any
# forbidden function call.  This enforces Requirements 10.1, 10.2, 10.3,
# 10.4, 10.5, 10.6, and 10.7 from the risk-engine spec.
#
# WHY THIS TEST EXISTS:
#   The Risk Engine must NEVER place, modify, or cancel orders.  A bug in
#   this module must not be able to cause unintended trades.  By scanning
#   the source code statically (without executing it), we get an early,
#   deterministic guarantee that no networking, subprocess, or trading-client
#   code has crept into the module — even before the implementation is
#   complete.  This test runs early so every subsequent implementation step
#   is immediately validated against the no-order-placement constraint.
#
# HOW IT WORKS (two-pass scan):
#   Pass 1 — Import statements:
#     Walk every ast.Import and ast.ImportFrom node in the AST.  Check each
#     imported module name (and symbol name) against the forbidden lists.
#     This catches statements like "import socket", "from requests import get",
#     or "from alpaca.trading import TradingClient".
#
#   Pass 2 — Forbidden function calls:
#     Walk every ast.Call node in the AST.  Inspect the function being called:
#       - If the call target is an ast.Name node (bare function call like
#         eval(...)), check whether node.id is one of {"eval", "exec",
#         "__import__"}.
#       - If the call target is an ast.Attribute node (dotted call like
#         importlib.import_module(...)), check whether the full dotted name
#         matches a forbidden callable (os.system, os.popen, os.exec*,
#         os.spawn*, importlib.import_module).
#
#     Pass 1 alone (walking Import/ImportFrom nodes) will NOT catch bare
#     eval() or exec() calls since those are built-in function invocations
#     with no import statement.  Pass 2 is required to detect them.
#
# This test does NOT execute any risk_engine code — it only reads source
# files and inspects their structure statically (like a C++ static analyser).

import ast       # Abstract Syntax Tree module — parses Python source into a tree
import pathlib   # Object-oriented file system paths (modern alternative to os.path)

import pytest    # Test framework


# ---------------------------------------------------------------------------
# Forbidden modules and symbols (Pass 1)
# ---------------------------------------------------------------------------
# These sets define what is NOT allowed in any import statement within the
# risk_engine/ directory.
#
# FORBIDDEN_MODULES: full module names that must never appear in an
# "import X" or "from X import ..." statement.
#
# FORBIDDEN_SYMBOLS: individual symbol names that must never appear in
# "from X import Y" statements (regardless of which module they come from).

FORBIDDEN_MODULES: set[str] = {
    # Networking libraries (Requirement 10.1)
    "socket",
    "urllib",
    "http.client",
    "http",
    "requests",
    "httpx",
    "websocket",
    "aiohttp",

    # Subprocess / process spawning (Requirement 10.3)
    "subprocess",

    # Alpaca SDK packages (Requirement 10.1, 10.2)
    "alpaca",
    "alpaca.trading",
    "alpaca.broker",
    "alpaca.data",

    # Dynamic import mechanisms (Requirement 10.7)
    "importlib",
}

FORBIDDEN_SYMBOLS: set[str] = {
    # Trading client classes/instances (Requirement 10.2)
    "TradingClient",
    "BrokerClient",
    "trading_client",
    "broker_client",

    # os functions that spawn subprocesses (Requirement 10.3)
    # These would appear as "from os import system" or "from os import popen"
    "system",
    "popen",
}


# ---------------------------------------------------------------------------
# Forbidden function calls (Pass 2)
# ---------------------------------------------------------------------------
# These sets define what bare function calls and dotted method calls are NOT
# allowed anywhere in risk_engine/ source files.
#
# FORBIDDEN_CALLS: bare function names that must never appear as a direct
# call (ast.Name nodes).  Example: eval("code") → ast.Name(id="eval")
#
# FORBIDDEN_DOTTED_CALLS: full dotted names that must never appear as a
# method/attribute call (ast.Attribute nodes).
# Example: importlib.import_module("x") → ast.Attribute chain
#
# FORBIDDEN_DOTTED_PREFIXES: dotted name prefixes checked with startswith().
# This catches the os.exec* and os.spawn* family of functions which have
# many variants (os.execl, os.execle, os.execv, os.spawnl, etc.).

FORBIDDEN_CALLS: set[str] = {
    # Dynamic code execution and import (Requirement 10.7)
    "eval",
    "exec",
    "__import__",
}

FORBIDDEN_DOTTED_CALLS: set[str] = {
    # Dynamic import (Requirement 10.7)
    "importlib.import_module",

    # os functions that spawn subprocesses (Requirement 10.3)
    "os.system",
    "os.popen",
}

# Prefixes for the os.exec* and os.spawn* family (Requirement 10.3).
# os.execl, os.execle, os.execlp, os.execlpe, os.execv, os.execve,
# os.execvp, os.execvpe, os.spawnl, os.spawnle, os.spawnlp, etc.
FORBIDDEN_DOTTED_PREFIXES: list[str] = [
    "os.exec",
    "os.spawn",
]


# ---------------------------------------------------------------------------
# Helper: collect all .py file paths in the risk_engine/ directory
# ---------------------------------------------------------------------------

def _find_risk_engine_py_files() -> list[pathlib.Path]:
    """
    Return a sorted list of all .py files in the risk_engine/ directory.

    Uses pathlib.Path.rglob() which recursively searches for files matching
    a glob pattern.  This is like running 'find risk_engine/ -name "*.py"'
    on the command line, or recursively iterating a directory in C++ with
    std::filesystem::recursive_directory_iterator.

    Returns:
        A list of pathlib.Path objects, sorted alphabetically for
        deterministic test output.
    """
    # __file__ is the path to THIS test file; we go up one level to
    # reach the project root, then down into risk_engine/.
    project_root = pathlib.Path(__file__).parent.parent
    risk_engine_dir = project_root / "risk_engine"

    # .rglob("*.py") recursively finds all files matching the *.py pattern.
    # list() materializes the generator into a list so we can sort it.
    py_files = sorted(risk_engine_dir.rglob("*.py"))

    return py_files


# ---------------------------------------------------------------------------
# Helper: resolve dotted name from an ast.Attribute chain
# ---------------------------------------------------------------------------

def _resolve_dotted_name(node: ast.expr) -> str:
    """
    Recursively resolve a dotted attribute chain into a string.

    Given an AST node representing something like `os.path.join`, this
    function walks the nested ast.Attribute nodes to reconstruct the full
    dotted name as a string "os.path.join".

    In C++ terms, think of this as walking a linked list of member-access
    operators (a.b.c) and concatenating the identifiers.

    Args:
        node: An AST expression node (typically ast.Attribute or ast.Name).

    Returns:
        The dotted name as a string (e.g. "os.system"), or an empty string
        if the node structure is not a simple name/attribute chain.
    """
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        # Recursively resolve the left-hand side (the object being accessed).
        parent = _resolve_dotted_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
        # If parent resolution failed, just return the attribute name.
        return node.attr
    # For other node types (subscripts, calls, etc.) we can't resolve.
    return ""


# ---------------------------------------------------------------------------
# Pass 1 Helper: check a single AST node for forbidden imports
# ---------------------------------------------------------------------------

def _check_import_violations(
    node: ast.AST,
    filepath: pathlib.Path,
) -> list[str]:
    """
    Inspect a single AST node and return a list of import violation messages.

    We check two types of import nodes:
      - ast.Import:     represents "import X" or "import X, Y, Z"
      - ast.ImportFrom: represents "from X import Y" or "from X import Y, Z"

    Args:
        node:     An AST node from the parsed source tree.
        filepath: The file being scanned (for error messages).

    Returns:
        A list of human-readable violation strings.  Empty if no violations.
    """
    violations: list[str] = []

    # --- Handle "import X" statements ---
    # ast.Import has a .names attribute — a list of alias objects, each with
    # a .name field (the dotted module name) and an optional .asname field.
    # Example: "import socket" → names=[alias(name='socket', asname=None)]
    if isinstance(node, ast.Import):
        for alias in node.names:
            module_name = alias.name  # e.g. "socket", "alpaca.trading"

            # Check if the full module name is forbidden.
            if module_name in FORBIDDEN_MODULES:
                violations.append(
                    f"{filepath}: line {node.lineno} — "
                    f"forbidden import '{module_name}'"
                )
            # Also check if it starts with a forbidden module prefix.
            # For example, "import alpaca.trading.foo" should be caught
            # because "alpaca.trading" is forbidden.
            else:
                for forbidden in FORBIDDEN_MODULES:
                    if module_name.startswith(forbidden + "."):
                        violations.append(
                            f"{filepath}: line {node.lineno} — "
                            f"forbidden import '{module_name}' "
                            f"(matches forbidden prefix '{forbidden}')"
                        )
                        break  # One violation per alias is enough

    # --- Handle "from X import Y" statements ---
    # ast.ImportFrom has:
    #   .module — the module being imported from (e.g. "os" in "from os import path")
    #   .names  — list of alias objects for the imported symbols
    # Example: "from os import system" → module='os', names=[alias(name='system')]
    elif isinstance(node, ast.ImportFrom):
        module_name = node.module or ""  # module can be None for relative imports

        # Check if the module itself is forbidden.
        if module_name in FORBIDDEN_MODULES:
            violations.append(
                f"{filepath}: line {node.lineno} — "
                f"forbidden import from '{module_name}'"
            )
        else:
            # Check if the module starts with a forbidden prefix.
            for forbidden in FORBIDDEN_MODULES:
                if module_name.startswith(forbidden + "."):
                    violations.append(
                        f"{filepath}: line {node.lineno} — "
                        f"forbidden import from '{module_name}' "
                        f"(matches forbidden prefix '{forbidden}')"
                    )
                    break

        # Check each imported symbol against FORBIDDEN_SYMBOLS.
        # This catches cases like "from alpaca.trading import TradingClient"
        # or "from os import system".
        for alias in node.names:
            if alias.name in FORBIDDEN_SYMBOLS:
                violations.append(
                    f"{filepath}: line {node.lineno} — "
                    f"forbidden symbol '{alias.name}' "
                    f"imported from '{module_name}'"
                )

    return violations


# ---------------------------------------------------------------------------
# Pass 2 Helper: check a single AST node for forbidden function calls
# ---------------------------------------------------------------------------

def _check_call_violations(
    node: ast.AST,
    filepath: pathlib.Path,
) -> list[str]:
    """
    Inspect a single AST node and return a list of forbidden-call violations.

    We check ast.Call nodes — these represent any function/method invocation
    in the source code (e.g. eval("x"), os.system("cmd")).

    For each call, we look at the function being called (.func attribute):
      - ast.Name:      bare function call like eval(...).
                       node.func.id gives us the function name.
      - ast.Attribute: dotted call like os.system(...).
                       We resolve the full dotted name and check it.

    Args:
        node:     An AST node from the parsed source tree.
        filepath: The file being scanned (for error messages).

    Returns:
        A list of human-readable violation strings.  Empty if no violations.
    """
    violations: list[str] = []

    if not isinstance(node, ast.Call):
        return violations

    func = node.func  # The expression representing the function being called.

    # --- Case 1: bare function call like eval(...), exec(...), __import__(...) ---
    # ast.Name represents a simple identifier.  node.func.id is the name string.
    if isinstance(func, ast.Name):
        if func.id in FORBIDDEN_CALLS:
            violations.append(
                f"{filepath}: line {node.lineno} — "
                f"forbidden function call '{func.id}()'"
            )

    # --- Case 2: dotted call like os.system(...), importlib.import_module(...) ---
    # ast.Attribute represents member access (obj.attr).  We reconstruct the
    # full dotted name and check against our forbidden lists.
    elif isinstance(func, ast.Attribute):
        dotted_name = _resolve_dotted_name(func)

        # Check exact matches (e.g. "os.system", "importlib.import_module")
        if dotted_name in FORBIDDEN_DOTTED_CALLS:
            violations.append(
                f"{filepath}: line {node.lineno} — "
                f"forbidden dotted call '{dotted_name}()'"
            )
        else:
            # Check prefix matches for os.exec* and os.spawn* families.
            # startswith() is Python's equivalent of strncmp()/starts_with().
            for prefix in FORBIDDEN_DOTTED_PREFIXES:
                if dotted_name.startswith(prefix):
                    violations.append(
                        f"{filepath}: line {node.lineno} — "
                        f"forbidden dotted call '{dotted_name}()' "
                        f"(matches forbidden prefix '{prefix}')"
                    )
                    break  # One violation per call node is enough

    return violations


# ---------------------------------------------------------------------------
# The actual test function
# ---------------------------------------------------------------------------

def test_no_forbidden_imports_or_calls():
    """
    AST-scan all .py files in risk_engine/ and verify no forbidden imports
    or function calls exist.

    This is a two-pass static analysis test:
      Pass 1: Scans import statements for forbidden modules and symbols.
      Pass 2: Scans function calls for forbidden bare calls and dotted calls.

    It does NOT execute any risk_engine code — it only reads source code
    files, parses them into Abstract Syntax Trees, and inspects structure.

    Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7
    """
    py_files = _find_risk_engine_py_files()

    # Sanity check: the risk_engine/ directory should contain at least one
    # .py file.  If this assert fires, the directory structure is wrong or
    # the test is running from the wrong working directory.
    assert len(py_files) > 0, (
        "No .py files found in risk_engine/ directory — "
        "check that the test is running from the project root"
    )

    # Accumulate all violations across all files so the failure message
    # reports everything at once (rather than stopping at the first one).
    all_violations: list[str] = []

    for filepath in py_files:
        # Read the source file as a string.
        # encoding="utf-8" ensures we handle any non-ASCII characters.
        source_code = filepath.read_text(encoding="utf-8")

        # ast.parse() converts source code text into an AST (a tree of nodes).
        # This is similar to calling a C++ parser front-end — it only analyses
        # syntax, it doesn't execute anything.
        tree = ast.parse(source_code, filename=str(filepath))

        # ast.walk() yields every node in the tree in breadth-first order.
        # This is like doing a BFS traversal of a tree in C++.
        for node in ast.walk(tree):
            # Pass 1: Check for forbidden imports.
            import_violations = _check_import_violations(node, filepath)
            all_violations.extend(import_violations)

            # Pass 2: Check for forbidden function calls.
            call_violations = _check_call_violations(node, filepath)
            all_violations.extend(call_violations)

    # If any violations were found, fail with a clear multi-line message.
    if all_violations:
        # "\n".join(list) concatenates all strings with newline separator —
        # similar to std::accumulate with "\n" as separator in C++.
        violation_report = "\n".join(all_violations)
        pytest.fail(
            f"Found {len(all_violations)} forbidden import(s)/call(s) in "
            f"risk_engine/ module:\n{violation_report}"
        )
