# tests/test_strategy_purity.py
#
# Static AST-scan smoke test for the strategy module.
#
# PURPOSE: Verify that no Python file in the strategy/ directory imports
# any forbidden module — specifically networking libraries, subprocess
# utilities, or Alpaca SDK classes.  This enforces Requirements 5.1, 5.2,
# 5.3, and 5.6 from the strategy-engine spec.
#
# HOW IT WORKS:
#   1. Find every .py file in the strategy/ directory.
#   2. Parse each file into an Abstract Syntax Tree (AST) using Python's
#      built-in `ast` module.  An AST is a tree representation of source
#      code — similar to how a C++ compiler builds a parse tree before
#      code generation.
#   3. Walk the AST nodes looking for Import and ImportFrom statements.
#   4. Check each imported module name (and symbol name) against a
#      forbidden list.
#   5. Fail the test with a clear message if any violation is found.
#
# This test does NOT execute any strategy code — it only reads source
# files and inspects their structure statically.

import ast       # Abstract Syntax Tree module — parses Python source into a tree
import os        # File system utilities (os.walk, os.path.join, etc.)
import pathlib   # Object-oriented file system paths (modern alternative to os.path)

import pytest    # Test framework


# ---------------------------------------------------------------------------
# Forbidden modules and symbols
# ---------------------------------------------------------------------------
# These sets define what is NOT allowed in any import statement within the
# strategy/ directory.
#
# FORBIDDEN_MODULES: full module names that must never appear in an
# "import X" or "from X import ..." statement.
#
# FORBIDDEN_SYMBOLS: individual symbol names that must never appear in
# "from X import Y" statements (regardless of which module they come from).

FORBIDDEN_MODULES: set[str] = {
    # Networking libraries (Requirement 5.1)
    "socket",
    "urllib",
    "http.client",
    "http",
    "requests",
    "httpx",
    "websocket",
    "aiohttp",

    # Subprocess / process spawning (Requirement 5.3)
    "subprocess",

    # Alpaca SDK packages (Requirement 5.1, 5.2)
    "alpaca",
    "alpaca.trading",
    "alpaca.broker",
    "alpaca.data",
}

FORBIDDEN_SYMBOLS: set[str] = {
    # Trading client classes/instances (Requirement 5.2)
    "TradingClient",
    "BrokerClient",
    "trading_client",
    "broker_client",

    # os functions that spawn subprocesses (Requirement 5.3)
    # These would appear as "from os import system" or "from os import popen"
    "system",
    "popen",
}

# We also need to catch "os.system" and "os.popen" when used via attribute
# access in import statements like "import os" followed by os.system().
# However, since we're only scanning imports (not function calls), we handle
# the "from os import system/popen" case via FORBIDDEN_SYMBOLS above, and
# the "import os" case is allowed (os is used legitimately for os.path).
# The AST scan focuses on import statements per Requirement 5.6.


# ---------------------------------------------------------------------------
# Helper: collect all .py file paths in the strategy/ directory
# ---------------------------------------------------------------------------

def _find_strategy_py_files() -> list[pathlib.Path]:
    """
    Return a sorted list of all .py files in the strategy/ directory.

    Uses pathlib.Path.rglob() which recursively searches for files matching
    a glob pattern.  This is like running 'find strategy/ -name "*.py"' on
    the command line, or recursively iterating a directory in C++ with
    std::filesystem::recursive_directory_iterator.

    Returns:
        A list of pathlib.Path objects, sorted alphabetically for
        deterministic test output.
    """
    # Determine the path to the strategy/ directory relative to the project
    # root.  __file__ is the path to THIS test file; we go up one level to
    # reach the project root, then down into strategy/.
    project_root = pathlib.Path(__file__).parent.parent
    strategy_dir = project_root / "strategy"

    # .rglob("*.py") recursively finds all files matching the *.py pattern.
    # list() materializes the generator into a list so we can sort it.
    py_files = sorted(strategy_dir.rglob("*.py"))

    return py_files


# ---------------------------------------------------------------------------
# Helper: check a single AST node for forbidden imports
# ---------------------------------------------------------------------------

def _check_node_for_violations(
    node: ast.AST,
    filepath: pathlib.Path,
) -> list[str]:
    """
    Inspect a single AST node and return a list of violation messages.

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
# The actual test function
# ---------------------------------------------------------------------------

def test_no_forbidden_imports():
    """
    AST-scan all .py files in strategy/ and verify no forbidden imports exist.

    This is a static analysis test — it reads source code files, parses them
    into Abstract Syntax Trees, and inspects every import statement.  It does
    NOT execute any strategy code.

    Validates: Requirements 5.1, 5.2, 5.3, 5.6
    """
    py_files = _find_strategy_py_files()

    # Sanity check: the strategy/ directory should contain at least one .py file.
    # If this assert fires, the directory structure is wrong or the test is
    # running from the wrong working directory.
    assert len(py_files) > 0, (
        "No .py files found in strategy/ directory — "
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
            violations = _check_node_for_violations(node, filepath)
            all_violations.extend(violations)

    # If any violations were found, fail with a clear multi-line message.
    if all_violations:
        # "\n".join(list) concatenates all strings with newline separator —
        # similar to std::accumulate with "\n" as separator in C++.
        violation_report = "\n".join(all_violations)
        pytest.fail(
            f"Found {len(all_violations)} forbidden import(s) in strategy/ "
            f"module:\n{violation_report}"
        )
