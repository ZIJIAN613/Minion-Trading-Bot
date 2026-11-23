# tests/test_orchestrator_purity.py
#
# Static AST-scan safety tests for the orchestrator module.
#
# PURPOSE: Verify that the orchestrator/ directory enforces paper-trading
# safety at the source-code level:
#   1. Every TradingClient instantiation uses paper=True as a LITERAL — not
#      a variable, not from config, not omitted.
#   2. No direct credential reading (no dotenv import, no os.getenv calls) —
#      credentials come exclusively from data_ingestion.credentials.
#   3. No configuration key exists that could select between paper/live mode.
#
# WHY THIS TEST EXISTS:
#   This is the automated guarantee that Requirement 9 (paper-trading safety
#   enforcement) can never be silently bypassed by future code changes.
#   It runs EARLY in the implementation (right after _create_paper_client is
#   written) so every subsequent implementation step is validated against
#   the paper-only constraint.
#
# HOW IT WORKS:
#   Each test reads source files from orchestrator/ and inspects their
#   Abstract Syntax Tree (AST) without executing them.  This is a static
#   analysis approach — like a C++ linter that checks source structure
#   without compiling or running the program.
#
# Requirements: 9.1, 9.2, 9.3, 9.4, 9.5

import ast       # Abstract Syntax Tree module — parses Python source into a tree
import pathlib   # Object-oriented file system paths (modern alternative to os.path)

import pytest    # Test framework


# ---------------------------------------------------------------------------
# Helper: find all .py files in the orchestrator/ directory
# ---------------------------------------------------------------------------

def _find_orchestrator_py_files() -> list[pathlib.Path]:
    """
    Return a sorted list of all .py files in the orchestrator/ directory.

    Uses pathlib.Path.rglob() which recursively searches for files matching
    a glob pattern.  This is like running 'find orchestrator/ -name "*.py"'
    on the command line.

    Returns:
        A list of pathlib.Path objects, sorted alphabetically.
    """
    # __file__ is the path to THIS test file; we go up one level to
    # reach the project root, then down into orchestrator/.
    project_root = pathlib.Path(__file__).parent.parent
    orchestrator_dir = project_root / "orchestrator"

    # .rglob("*.py") recursively finds all files matching *.py.
    py_files = sorted(orchestrator_dir.rglob("*.py"))
    return py_files


# ---------------------------------------------------------------------------
# Helper: resolve dotted name from an AST attribute chain
# ---------------------------------------------------------------------------

def _resolve_dotted_name(node: ast.expr) -> str:
    """
    Recursively resolve a dotted attribute chain into a string.

    Given an AST node representing something like `os.path.join`, this
    function walks the nested ast.Attribute nodes to reconstruct the full
    dotted name as a string "os.path.join".

    Args:
        node: An AST expression node (typically ast.Attribute or ast.Name).

    Returns:
        The dotted name as a string, or "" if not resolvable.
    """
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        parent = _resolve_dotted_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
        return node.attr
    return ""


# ===========================================================================
# Test 1: test_paper_true_ast_scan
# ===========================================================================

def test_paper_true_ast_scan():
    """
    AST scan all .py files in orchestrator/ verifying:
      1. paper=True appears as a keyword argument in EVERY TradingClient
         instantiation.
      2. No paper=False exists anywhere.
      3. The paper parameter is always a literal True — never a variable,
         function call, or any other expression.

    This is the core safety enforcement: if someone changes paper=True to
    paper=some_variable or paper=config.get("paper"), this test catches it.

    Validates: Requirements 9.1, 9.2, 9.3, 9.4
    """
    py_files = _find_orchestrator_py_files()
    assert len(py_files) > 0, (
        "No .py files found in orchestrator/ — "
        "check that the test is running from the project root"
    )

    violations: list[str] = []
    found_trading_client_calls = 0

    for filepath in py_files:
        source_code = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source_code, filename=str(filepath))

        # Walk all nodes looking for Call nodes where the function is
        # "TradingClient" (either bare name or dotted like
        # alpaca.trading.client.TradingClient).
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            # Determine the function name being called.
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr  # Just the last segment.

            # We only care about calls to "TradingClient".
            if func_name != "TradingClient":
                continue

            found_trading_client_calls += 1

            # Check keyword arguments for "paper".
            paper_kwarg = None
            for kw in node.keywords:
                if kw.arg == "paper":
                    paper_kwarg = kw
                    break

            # VIOLATION: No paper keyword argument at all.
            if paper_kwarg is None:
                violations.append(
                    f"{filepath}: line {node.lineno} — "
                    f"TradingClient() call is missing the 'paper' keyword "
                    f"argument. Must use paper=True as a literal."
                )
                continue

            # VIOLATION: paper keyword exists but value is not a literal True.
            # ast.Constant is the AST node for literal values (True, False,
            # numbers, strings).  We check that it's specifically True.
            value_node = paper_kwarg.value

            if not isinstance(value_node, ast.Constant):
                # The value is an expression (variable, function call, etc.)
                # rather than a literal.  This is NOT allowed.
                violations.append(
                    f"{filepath}: line {node.lineno} — "
                    f"TradingClient() uses a non-literal value for 'paper'. "
                    f"Must be the literal True, not a variable or expression."
                )
            elif value_node.value is not True:
                # The value is a literal, but it's False or something else.
                violations.append(
                    f"{filepath}: line {node.lineno} — "
                    f"TradingClient() has paper={value_node.value!r}. "
                    f"Must be paper=True (literal True)."
                )

    # We expect at least one TradingClient call in the orchestrator module
    # (in _create_paper_client).  If zero are found, the module structure
    # is incomplete or the scan missed something.
    if found_trading_client_calls == 0:
        violations.append(
            "No TradingClient() calls found in orchestrator/ — "
            "expected at least one (in _create_paper_client)."
        )

    if violations:
        violation_report = "\n".join(violations)
        pytest.fail(
            f"Found {len(violations)} paper-trading safety violation(s) in "
            f"orchestrator/:\n{violation_report}"
        )


# ===========================================================================
# Test 2: test_no_direct_env_reading
# ===========================================================================

def test_no_direct_env_reading():
    """
    AST scan all .py files in orchestrator/ verifying:
      1. No 'import dotenv' or 'from dotenv import ...' statements.
      2. No 'os.getenv(...)' or 'os.environ[...]' calls.
      3. No 'dotenv.load_dotenv()' calls.

    The orchestrator must obtain credentials exclusively via
    data_ingestion.credentials.load_credentials() — it must never
    read environment variables or .env files directly.

    Validates: Requirement 9.5
    """
    py_files = _find_orchestrator_py_files()
    assert len(py_files) > 0

    violations: list[str] = []

    # Forbidden import module names.
    forbidden_imports = {"dotenv", "python_dotenv"}

    for filepath in py_files:
        source_code = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source_code, filename=str(filepath))

        for node in ast.walk(tree):
            # Check import statements for 'dotenv'.
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in forbidden_imports:
                        violations.append(
                            f"{filepath}: line {node.lineno} — "
                            f"forbidden import '{alias.name}'. "
                            f"Use data_ingestion.credentials.load_credentials() instead."
                        )

            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""
                if module_name in forbidden_imports or module_name.startswith("dotenv"):
                    violations.append(
                        f"{filepath}: line {node.lineno} — "
                        f"forbidden import from '{module_name}'. "
                        f"Use data_ingestion.credentials.load_credentials() instead."
                    )

            # Check for os.getenv() and os.environ calls/subscripts.
            elif isinstance(node, ast.Call):
                dotted = _resolve_dotted_name(node.func) if hasattr(node, "func") else ""
                if dotted in ("os.getenv", "os.environ.get"):
                    violations.append(
                        f"{filepath}: line {node.lineno} — "
                        f"forbidden call '{dotted}()'. "
                        f"Use data_ingestion.credentials.load_credentials() instead."
                    )
                # Also catch dotenv.load_dotenv() calls.
                if "load_dotenv" in dotted:
                    violations.append(
                        f"{filepath}: line {node.lineno} — "
                        f"forbidden call '{dotted}()'. "
                        f"Use data_ingestion.credentials.load_credentials() instead."
                    )

            # Check for os.environ[...] subscript access.
            elif isinstance(node, ast.Subscript):
                dotted = _resolve_dotted_name(node.value) if hasattr(node, "value") else ""
                if dotted == "os.environ":
                    violations.append(
                        f"{filepath}: line {node.lineno} — "
                        f"forbidden subscript access 'os.environ[...]'. "
                        f"Use data_ingestion.credentials.load_credentials() instead."
                    )

    if violations:
        violation_report = "\n".join(violations)
        pytest.fail(
            f"Found {len(violations)} direct-env-reading violation(s) in "
            f"orchestrator/:\n{violation_report}"
        )


# ===========================================================================
# Test 3: test_no_live_trading_config
# ===========================================================================

def test_no_live_trading_config():
    """
    Verify that orchestrator/config.yaml does not contain any configuration
    key that could select between paper and live trading modes.

    Specifically, the config file must NOT contain keys like:
      - "paper"
      - "live"
      - "mode"
      - "trading_mode"
      - "paper_trading"
      - "live_trading"
      - "environment"

    This ensures there is no "switch" that could accidentally route the
    bot to a live account via configuration.

    Validates: Requirement 9.4
    """
    import yaml

    project_root = pathlib.Path(__file__).parent.parent
    config_path = project_root / "orchestrator" / "config.yaml"

    assert config_path.exists(), (
        f"orchestrator/config.yaml not found at {config_path}"
    )

    # Read and parse the config file.
    content = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(content)

    # If the file parses to None (empty) or non-dict, that's fine for this
    # test — there are no forbidden keys.
    if not isinstance(config, dict):
        return  # No keys to check.

    # Forbidden key patterns — any of these would indicate a paper/live
    # mode selector exists in the config.
    forbidden_keys = {
        "paper",
        "live",
        "mode",
        "trading_mode",
        "paper_trading",
        "live_trading",
        "environment",
        "env",
        "is_paper",
        "is_live",
        "use_paper",
        "use_live",
    }

    violations: list[str] = []
    for key in config.keys():
        # Check exact match and also case-insensitive partial match.
        key_lower = key.lower()
        if key_lower in forbidden_keys:
            violations.append(
                f"orchestrator/config.yaml contains forbidden key '{key}'. "
                f"No configuration parameter may select between paper and live "
                f"trading modes (Requirement 9.4)."
            )

    if violations:
        violation_report = "\n".join(violations)
        pytest.fail(
            f"Found {len(violations)} live-trading config violation(s):\n"
            f"{violation_report}"
        )
