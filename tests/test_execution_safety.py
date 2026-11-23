# tests/test_execution_safety.py
#
# Static AST-scan safety tests for the execution module.
#
# PURPOSE: Verify that the execution module's paper-trading safety invariant
# is enforced at the source-code level:
#   1. Every TradingClient instantiation uses paper=True as a LITERAL True.
#   2. The module docstring explicitly forbids live trading.
#
# These tests run WITHOUT executing any execution module code — they parse
# the Python source files into Abstract Syntax Trees (ASTs) and inspect the
# tree structure.  This is similar to running a C++ static analyser that
# checks for forbidden patterns before compilation.
#
# WHY AST SCANNING:
#   A runtime test can verify paper=True at execution time, but an AST scan
#   catches violations BEFORE the code ever runs.  If a developer accidentally
#   changes paper=True to paper=some_variable, this test will catch it
#   immediately — even if that code path is never exercised in other tests.
#
# Validates: Requirements 6.4, 6.5

import ast       # Abstract Syntax Tree module — parses Python source into a tree
import os        # os.path — file path manipulation (like <filesystem> in C++)
import pathlib   # Object-oriented paths (modern Python alternative to os.path)

import pytest    # Test framework


# ---------------------------------------------------------------------------
# Helper: locate the execution/ directory relative to this test file
# ---------------------------------------------------------------------------

def _get_execution_dir() -> pathlib.Path:
    """
    Return the absolute path to the execution/ directory.

    We navigate from this test file's location:
      tests/test_execution_safety.py  →  up one level  →  project root
      project root / execution/

    pathlib.Path(__file__) gives us the path to THIS file.
    .parent goes up one directory level (like ".." in a shell).

    Returns:
        A pathlib.Path pointing to the execution/ directory.
    """
    # __file__ is a Python magic variable holding the path to the current
    # source file — similar to __FILE__ in C/C++ preprocessor macros.
    project_root = pathlib.Path(__file__).parent.parent
    return project_root / "execution"


# ---------------------------------------------------------------------------
# Helper: collect all .py files in the execution/ directory
# ---------------------------------------------------------------------------

def _find_execution_py_files() -> list[pathlib.Path]:
    """
    Return a sorted list of all .py files in the execution/ directory.

    Uses pathlib.Path.rglob("*.py") which recursively searches for files
    matching the glob pattern — like running:
      find execution/ -name "*.py"
    on the command line, or using std::filesystem::recursive_directory_iterator
    with a filter in C++.

    Returns:
        A sorted list of pathlib.Path objects for deterministic test output.
    """
    execution_dir = _get_execution_dir()

    # .rglob("*.py") is a recursive glob — finds *.py in all subdirectories.
    # sorted() ensures deterministic ordering across runs.
    py_files = sorted(execution_dir.rglob("*.py"))

    return py_files


# ---------------------------------------------------------------------------
# Helper: resolve dotted name from an AST attribute chain
# ---------------------------------------------------------------------------

def _resolve_call_name(node: ast.expr) -> str:
    """
    Recursively resolve a function call target into a dotted name string.

    Given an AST node representing the function being called (e.g., the
    `TradingClient` in `TradingClient(...)` or `alpaca.trading.TradingClient`),
    this walks the nested ast.Attribute/ast.Name nodes to reconstruct the
    full name as a string.

    In C++ terms: this is like walking a chain of scope resolution operators
    (namespace::class::method) and concatenating the identifiers.

    Args:
        node: An AST expression node (ast.Name or ast.Attribute).

    Returns:
        The resolved name as a string, or "" if the node is too complex
        to resolve (e.g., a subscript or dynamic expression).
    """
    # ast.Name represents a simple identifier — like a bare variable name.
    # Example: TradingClient → ast.Name(id="TradingClient")
    if isinstance(node, ast.Name):
        return node.id

    # ast.Attribute represents dotted access — like member access in C++.
    # Example: alpaca.trading.TradingClient →
    #   ast.Attribute(value=ast.Attribute(...), attr="TradingClient")
    elif isinstance(node, ast.Attribute):
        parent = _resolve_call_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
        return node.attr

    # For other node types (subscripts, function calls as targets, etc.)
    # we cannot resolve to a simple name.
    return ""


# ---------------------------------------------------------------------------
# Test 1: AST scan for paper=True enforcement
# ---------------------------------------------------------------------------

def test_paper_true_ast_scan():
    """
    AST-scan all .py files in execution/ verifying paper-trading safety.

    Checks:
      1. Every TradingClient(...) instantiation has paper=True as a keyword
         argument where True is a LITERAL (ast.Constant with value True).
      2. No TradingClient call uses paper=False.
      3. No TradingClient call omits the paper parameter entirely.
      4. No TradingClient call uses a variable or expression for the paper
         parameter value — it MUST be the literal True.
      5. At least one TradingClient instantiation exists (the module MUST
         instantiate it somewhere).

    This test does NOT execute any code — it only reads source files and
    inspects their AST structure statically.

    Validates: Requirement 6.4
    """
    py_files = _find_execution_py_files()

    # Sanity check: execution/ must contain at least one .py file.
    assert len(py_files) > 0, (
        "No .py files found in execution/ directory — "
        "check that the test is running from the project root"
    )

    # Track all violations and the total count of TradingClient calls found.
    violations: list[str] = []
    trading_client_call_count = 0

    for filepath in py_files:
        # Read the source file as text.
        # encoding="utf-8" handles any non-ASCII characters in comments.
        source_code = filepath.read_text(encoding="utf-8")

        # ast.parse() converts Python source text into an Abstract Syntax Tree.
        # This is analogous to a C++ parser front-end — it analyses the syntax
        # structure without executing the code.
        tree = ast.parse(source_code, filename=str(filepath))

        # ast.walk() yields every node in the tree in breadth-first order.
        # This is like a BFS traversal of a tree data structure in C++.
        for node in ast.walk(tree):
            # We only care about ast.Call nodes — these represent function
            # or constructor invocations (like `TradingClient(...)` or
            # `some_module.TradingClient(...)`).
            if not isinstance(node, ast.Call):
                continue

            # Resolve the name of the function being called.
            call_name = _resolve_call_name(node.func)

            # Check if this call is to TradingClient.
            # We match if the resolved name IS "TradingClient" or ENDS WITH
            # ".TradingClient" (to catch qualified calls like
            # `alpaca.trading.client.TradingClient(...)`).
            if call_name != "TradingClient" and not call_name.endswith(".TradingClient"):
                continue

            # We found a TradingClient instantiation — count it.
            trading_client_call_count += 1

            # Now inspect the keyword arguments of this call.
            # node.keywords is a list of ast.keyword objects, each with:
            #   .arg  — the keyword name (string), e.g. "paper"
            #   .value — the AST node for the value expression
            #
            # This is like inspecting named parameters in a C++ constructor
            # call (if C++ had named parameters).

            # Find the 'paper' keyword argument.
            paper_kwarg = None
            for kw in node.keywords:
                # kw.arg is None for **kwargs expansion (e.g., **config).
                # We skip those — they can't be statically verified.
                if kw.arg == "paper":
                    paper_kwarg = kw
                    break

            # Case 1: The 'paper' keyword is entirely missing.
            if paper_kwarg is None:
                violations.append(
                    f"{filepath.name}: line {node.lineno} — "
                    f"TradingClient instantiation is missing the 'paper' "
                    f"keyword argument (it MUST be paper=True)"
                )
                continue

            # Case 2: The 'paper' keyword exists — verify its value is the
            # literal True (an ast.Constant node with value == True).
            #
            # ast.Constant represents a literal value in the source code:
            #   True  → ast.Constant(value=True)
            #   False → ast.Constant(value=False)
            #   42    → ast.Constant(value=42)
            #   "hi"  → ast.Constant(value="hi")
            #
            # If the value is NOT an ast.Constant, it means a variable or
            # expression was used (e.g., paper=some_var, paper=get_mode()).
            # That's forbidden — only the literal True is acceptable.

            value_node = paper_kwarg.value

            # isinstance() checks if an object is of a given type — like
            # dynamic_cast<> in C++ (returns True/False instead of nullptr).
            if not isinstance(value_node, ast.Constant):
                # The value is NOT a literal — it's a variable or expression.
                violations.append(
                    f"{filepath.name}: line {node.lineno} — "
                    f"TradingClient 'paper' parameter uses a variable or "
                    f"expression instead of the literal True"
                )
                continue

            # The value IS a constant — now verify it equals True (not False,
            # not 1, not "True", but exactly the boolean True).
            if value_node.value is not True:
                violations.append(
                    f"{filepath.name}: line {node.lineno} — "
                    f"TradingClient 'paper' parameter is set to "
                    f"{value_node.value!r} — it MUST be the literal True"
                )

    # After scanning all files: verify at least one TradingClient call exists.
    # If no calls were found, the module isn't instantiating the client at all,
    # which means the safety mechanism has nothing to enforce.
    assert trading_client_call_count > 0, (
        "No TradingClient instantiation found in any .py file in execution/ — "
        "the module MUST instantiate TradingClient somewhere with paper=True"
    )

    # If any violations were found, fail with a detailed report.
    if violations:
        # "\n".join() concatenates list elements with newline separator —
        # like boost::algorithm::join() in C++.
        violation_report = "\n".join(violations)
        pytest.fail(
            f"Found {len(violations)} paper-trading safety violation(s) in "
            f"execution/ module:\n{violation_report}"
        )


# ---------------------------------------------------------------------------
# Test 2: __init__.py docstring forbids live trading
# ---------------------------------------------------------------------------

def test_init_docstring_forbids_live():
    """
    Verify that execution/__init__.py has a module-level docstring that
    explicitly states live trading is forbidden.

    Checks (both must pass):
      1. The docstring contains "live trading" (case-insensitive).
      2. The docstring contains "forbidden" (case-insensitive).

    This ensures that anyone reading the module header immediately sees
    the safety constraint — similar to a prominent warning comment at the
    top of a C++ header file.

    Validates: Requirement 6.5
    """
    execution_dir = _get_execution_dir()
    init_path = execution_dir / "__init__.py"

    # Verify the file exists before attempting to parse it.
    assert init_path.exists(), (
        f"execution/__init__.py not found at {init_path}"
    )

    # Read and parse the source file.
    source_code = init_path.read_text(encoding="utf-8")
    tree = ast.parse(source_code, filename=str(init_path))

    # Extract the module-level docstring.
    # In Python, a module docstring is the first statement in the file IF
    # that statement is a string expression (ast.Expr containing an
    # ast.Constant with a string value).
    #
    # ast.get_docstring() is a built-in helper that does exactly this —
    # it returns the docstring text as a string, or None if there is no
    # docstring.  It's like reading the Doxygen comment block at the top
    # of a C++ file, except in Python the docstring is actual syntax (a
    # string literal), not a comment.
    docstring = ast.get_docstring(tree)

    # Verify a docstring exists.
    assert docstring is not None, (
        "execution/__init__.py does not have a module-level docstring — "
        "it MUST include a docstring stating that live trading is forbidden"
    )

    # Convert to lowercase for case-insensitive substring checks.
    # str.lower() returns a new string with all characters converted to
    # lowercase — like std::transform with std::tolower in C++.
    docstring_lower = docstring.lower()

    # Check 1: Must mention "live trading".
    assert "live trading" in docstring_lower, (
        "execution/__init__.py module docstring does not contain "
        "'live trading' (case-insensitive) — it MUST state that "
        "live trading is forbidden"
    )

    # Check 2: Must mention "forbidden".
    assert "forbidden" in docstring_lower, (
        "execution/__init__.py module docstring does not contain "
        "'forbidden' (case-insensitive) — it MUST explicitly state "
        "that live trading is FORBIDDEN"
    )


# ---------------------------------------------------------------------------
# Test 3: AST scan verifying execution/ does not import dotenv or read .env
# ---------------------------------------------------------------------------

def test_no_direct_env_reading():
    """
    AST-scan all .py files in execution/ verifying that the execution module
    does NOT import dotenv or read .env files directly.

    The execution module MUST use data_ingestion.credentials.load_credentials()
    for all credential loading (Requirement 9.2).  Direct .env reading would:
      - Duplicate credential logic (maintenance burden)
      - Bypass centralized credential validation
      - Potentially expose credential values in unexpected ways

    Checks:
      1. No `import dotenv`, `import python_dotenv`, `from dotenv import ...`,
         `from python_dotenv import ...`, or `from load_dotenv import ...`
         appears in any .py file in execution/.
      2. No open() call uses a string literal containing ".env" as its
         argument — that would indicate direct file-path reading of the
         .env file instead of using the shared credential loader.

    This test does NOT execute any code — it only reads source files and
    inspects their AST structure statically.

    Validates: Requirement 9.2
    """
    py_files = _find_execution_py_files()

    # Sanity check: execution/ must contain at least one .py file.
    assert len(py_files) > 0, (
        "No .py files found in execution/ directory — "
        "check that the test is running from the project root"
    )

    # Set of forbidden module names that would indicate direct .env reading.
    # We check for variations of the dotenv package name.
    forbidden_modules = {"dotenv", "python_dotenv", "python-dotenv", "load_dotenv"}

    # Track all violations found across all files.
    violations: list[str] = []

    for filepath in py_files:
        # Read the source file as text.
        source_code = filepath.read_text(encoding="utf-8")

        # Parse the source into an AST (Abstract Syntax Tree).
        # This is like running a C++ parser to inspect the #include directives.
        tree = ast.parse(source_code, filename=str(filepath))

        # Walk every node in the AST tree.
        # ast.walk() yields ALL nodes in breadth-first order — like a BFS
        # traversal of a tree data structure in C++.
        for node in ast.walk(tree):

            # ----- Check 1: ast.Import nodes -----
            # ast.Import represents statements like: `import dotenv`
            # node.names is a list of ast.alias objects, each with a .name
            # attribute (the module name string).
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # alias.name is the imported module name (e.g., "dotenv").
                    # We normalize with replace("-", "_") because Python
                    # import names use underscores but the package name on
                    # PyPI might use hyphens (e.g., "python-dotenv").
                    normalized_name = alias.name.replace("-", "_")
                    if normalized_name in forbidden_modules:
                        violations.append(
                            f"{filepath.name}: line {node.lineno} — "
                            f"imports '{alias.name}' directly. "
                            f"Use data_ingestion.credentials.load_credentials() instead."
                        )

            # ----- Check 2: ast.ImportFrom nodes -----
            # ast.ImportFrom represents statements like: `from dotenv import load_dotenv`
            # node.module is the module name string (e.g., "dotenv").
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    # Normalize the module name the same way.
                    normalized_module = node.module.replace("-", "_")
                    if normalized_module in forbidden_modules:
                        violations.append(
                            f"{filepath.name}: line {node.lineno} — "
                            f"imports from '{node.module}' directly. "
                            f"Use data_ingestion.credentials.load_credentials() instead."
                        )

            # ----- Check 3: open() calls with a ".env" string argument -----
            # ast.Call represents function calls like: open(".env")
            # We look for calls where the function name resolves to "open"
            # and the first positional argument is a string containing ".env".
            elif isinstance(node, ast.Call):
                # Resolve the function name being called.
                call_name = _resolve_call_name(node.func)

                # We only care about calls to the built-in `open()` function.
                if call_name != "open":
                    continue

                # Check if the first positional argument (the file path) is
                # a string literal containing ".env".
                # node.args is a list of positional arguments to the call.
                if node.args and isinstance(node.args[0], ast.Constant):
                    # ast.Constant.value holds the literal value.
                    arg_value = node.args[0].value
                    if isinstance(arg_value, str) and ".env" in arg_value:
                        violations.append(
                            f"{filepath.name}: line {node.lineno} — "
                            f"open() call with '.env' in the path string: "
                            f"{arg_value!r}. "
                            f"Use data_ingestion.credentials.load_credentials() instead."
                        )

    # If any violations were found, fail with a detailed report.
    if violations:
        violation_report = "\n".join(violations)
        pytest.fail(
            f"Found {len(violations)} direct .env reading violation(s) in "
            f"execution/ module:\n{violation_report}\n\n"
            f"The execution module MUST use "
            f"data_ingestion.credentials.load_credentials() for credential loading."
        )


# ---------------------------------------------------------------------------
# Test 4: Behavioral test — no retry on submission failure
# ---------------------------------------------------------------------------

def test_no_retry_on_submission_failure():
    """
    Behavioral test verifying that execute_decision does NOT retry a failed
    order submission.

    Design Decision (from design.md):
      "No automatic retries: The module attempts each API call once.
       Retry logic is the pipeline orchestrator's responsibility."

    This test:
      1. Creates a valid APPROVED SELL Decision_Dict (all fields valid).
         We use SELL (not BUY) because SELL has a single submission path
         with no fallback.  BUY has a designed OTO→fallback pattern that
         is NOT a retry but would show as 2 submit_order calls.
      2. Creates a mock Alpaca client where submit_order raises RuntimeError
         on the FIRST call (simulating a network/API failure).
      3. Calls execute_decision(decision_dict, config, client=mock_client).
      4. Asserts the result has status="error" (failure was caught).
      5. Asserts mock_client.submit_order was called exactly ONCE — proving
         no retry was attempted.

    This tests the "no retry" property DIRECTLY (behavioral verification)
    rather than inferring it from source code shape (AST scanning).

    Validates: Requirement 12.5
    """
    # unittest.mock.MagicMock creates a fake object that records all method
    # calls made on it — like a test spy / mock object in Google Mock for C++.
    from unittest.mock import MagicMock

    import pandas as pd

    # Import the function under test and the config dataclass.
    from execution.execute import execute_decision
    from execution.config_loader import ExecutionConfig

    # --- Step 1: Create a valid ExecutionConfig ---
    # These values are within the valid range [5,300] and [0.5,30].
    config = ExecutionConfig(fill_timeout_seconds=5, poll_interval_seconds=1.0)

    # --- Step 2: Create a valid APPROVED SELL Decision_Dict ---
    # We use SELL because it has a single submit_order call with no fallback.
    # BUY has an intentional OTO→fallback pattern (not a retry) that would
    # produce 2 submit_order calls even without retries.
    decision_dict = {
        "decision": "APPROVED",
        "signal": "SELL",
        "symbol": "AAPL",
        "position_size": 10,
        "stop_loss": 145.50,
        "reason": "Test: verifying no-retry behavior on submission failure",
        "timestamp": pd.Timestamp.now("UTC"),
    }

    # --- Step 3: Create a mock Alpaca client ---
    # MagicMock() creates a mock object.  We configure submit_order to raise
    # an exception — simulating a network failure on the first (and only) call.
    #
    # side_effect is a MagicMock feature: when set to an exception class or
    # instance, calling the mock will raise that exception instead of returning
    # a value.  This is like setting up EXPECT_CALL(...).WillOnce(Throw(...))
    # in Google Mock for C++.
    mock_client = MagicMock()
    mock_client.submit_order.side_effect = RuntimeError("Connection refused")

    # --- Step 4: Call execute_decision ---
    # Since we pass client=mock_client, the function will use our mock
    # instead of creating a real Alpaca paper client.
    result = execute_decision(decision_dict, config, client=mock_client)

    # --- Step 5: Assert status is "error" ---
    # The function should catch the RuntimeError and return an error result
    # rather than crashing or propagating the exception.
    assert result["status"] == "error", (
        f"Expected status='error' when submit_order raises, "
        f"but got status='{result['status']}'"
    )

    # --- Step 6: Assert submit_order was called exactly ONCE ---
    # call_count is a MagicMock attribute that tracks how many times the
    # mock method was invoked — like EXPECT_CALL(...).Times(1) in Google Mock.
    # If the execution module retried, call_count would be > 1.
    assert mock_client.submit_order.call_count == 1, (
        f"Expected submit_order to be called exactly 1 time (no retry), "
        f"but it was called {mock_client.submit_order.call_count} time(s). "
        f"The execution module must NOT retry failed submissions — "
        f"retry logic is the pipeline orchestrator's responsibility."
    )
