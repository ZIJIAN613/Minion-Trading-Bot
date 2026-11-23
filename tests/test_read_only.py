"""
tests/test_read_only.py — Static import scan for read-only enforcement.

This test does NOT import any data_ingestion code and makes NO network
requests. It works purely on source text — reading .py files as strings
and parsing them into an Abstract Syntax Tree (AST).

The goal: guarantee that data_ingestion/ never imports TradingClient,
BrokerClient, or any module named trading_client / broker_client.
These classes expose order-placement methods, which would violate the
module's read-only contract (Requirements 4.1 and 4.2).

Requirements: 4.1, 4.2
"""

import ast       # Python's built-in AST module — like Clang's AST but for Python
import pathlib   # Python's OOP path API — like std::filesystem::path in C++17

# The list of symbol names and module path fragments that must NEVER appear
# in any import statement inside data_ingestion/.
#
# These cover both the class names (TradingClient, BrokerClient) and the
# snake_case module names (trading_client, broker_client) that the alpaca-py
# SDK uses for its order-placement clients.
FORBIDDEN_SYMBOLS: list[str] = [
    "TradingClient",
    "BrokerClient",
    "trading_client",
    "broker_client",
]


def test_no_forbidden_imports() -> None:
    """
    Scan every .py file under data_ingestion/ and assert that none of them
    import TradingClient, BrokerClient, trading_client, or broker_client.

    Algorithm (mirrors a recursive file walk in C++ with std::filesystem):
      1. Locate the data_ingestion/ directory relative to this test file.
      2. Use pathlib.Path.rglob("*.py") to find all .py files recursively.
         rglob is "recursive glob" — equivalent to std::filesystem::recursive_
         directory_iterator filtered by extension.
      3. Parse each file's source text with ast.parse() — this gives us a tree
         of AST nodes without executing any code.
      4. Walk the tree with ast.walk() — a generator that yields every node
         depth-first, like a recursive tree traversal.
      5. Collect every Import and ImportFrom node (the two AST node types that
         represent Python import statements).
      6. For each import node, extract all names being imported and check them
         against FORBIDDEN_SYMBOLS. Fail immediately if a match is found.
    """

    # --- Step 1: locate the data_ingestion/ directory ---
    #
    # pathlib.Path(__file__) is the absolute path to THIS test file.
    # .parent gives the directory containing it (i.e., tests/).
    # .parent again gives the workspace root (c:\Minions\).
    # Then we append "data_ingestion" to get the package directory.
    #
    # pathlib.Path is Python's OOP path API — like std::filesystem::path in
    # C++17. It overloads the / operator for path joining, which is cleaner
    # than string concatenation.
    tests_dir: pathlib.Path = pathlib.Path(__file__).parent
    package_dir: pathlib.Path = tests_dir.parent / "data_ingestion"

    assert package_dir.is_dir(), (
        f"Expected data_ingestion/ directory at {package_dir} — not found. "
        "Is the test being run from the project root?"
    )

    # --- Step 2: collect all .py files under data_ingestion/ ---
    #
    # .rglob("*.py") is a generator that yields every path matching "*.py"
    # in this directory and all subdirectories. A generator is a lazy iterator
    # — like a C++ input iterator that produces values on demand rather than
    # building a full list upfront. We convert it to a sorted list so the
    # scan order is deterministic across runs (important for reproducibility).
    py_files: list[pathlib.Path] = sorted(package_dir.rglob("*.py"))

    assert py_files, (
        f"No .py files found under {package_dir}. "
        "The directory may be empty or the path is wrong."
    )

    # --- Steps 3–6: parse each file and scan its AST for forbidden imports ---
    for py_file in py_files:
        # Read the source text as a UTF-8 string.
        # pathlib.Path.read_text() is the idiomatic one-liner for "open file,
        # read all contents, close file" — equivalent to:
        #   with open(py_file, "r", encoding="utf-8") as f:
        #       source = f.read()
        source: str = py_file.read_text(encoding="utf-8")

        # ast.parse() reads source code as a tree of nodes — like Clang's AST
        # but built into Python's stdlib; no compilation needed. It returns the
        # root Module node of the AST without executing any of the code.
        try:
            tree: ast.AST = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            # If a file has a syntax error, we can't scan it. Fail loudly so
            # the developer knows the file is broken — this should never happen
            # in a well-maintained codebase.
            raise AssertionError(
                f"Syntax error while parsing {py_file}: {exc}"
            ) from exc

        # ast.walk() is a generator that yields every node in the tree
        # depth-first — Python's equivalent of a recursive tree traversal.
        # Each yielded item is an AST node object whose type we can check with
        # isinstance() (Python's dynamic_cast equivalent).
        for node in ast.walk(tree):

            # --- Handle "import foo, bar" style imports ---
            # ast.Import represents bare import statements like:
            #   import alpaca.trading.client
            #   import trading_client
            # node.names is a list of alias objects, each with a .name attribute
            # holding the full dotted module name (e.g. "alpaca.trading.client").
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # alias.name is a string like "alpaca.trading.client".
                    # We check whether any forbidden symbol appears as a
                    # substring in the module name — this catches both
                    # "import trading_client" and "import alpaca.trading_client".
                    _assert_name_clean(alias.name, py_file)

            # --- Handle "from foo import bar, baz" style imports ---
            # ast.ImportFrom represents statements like:
            #   from alpaca.trading.client import TradingClient
            #   from data_ingestion import trading_client
            # node.module is the left-hand side ("alpaca.trading.client"),
            # node.names is the list of imported names on the right ("TradingClient").
            elif isinstance(node, ast.ImportFrom):
                # Check the module path itself (left side of "from ... import").
                # node.module can be None for relative imports like "from . import x".
                if node.module is not None:
                    _assert_name_clean(node.module, py_file)

                # Also check each name on the right side of "import ...".
                # This catches "from alpaca.trading.client import TradingClient"
                # even if the module name alone does not match.
                for alias in node.names:
                    _assert_name_clean(alias.name, py_file)


def _assert_name_clean(name: str, py_file: pathlib.Path) -> None:
    """
    Assert that `name` does not contain any forbidden symbol as a substring.

    Extracted into a helper to avoid duplicating the loop logic above.
    Using a helper function here is the Python equivalent of a small inline
    function in C++ — keeps the main test readable.

    Args:
        name:    The import name or module path string to check.
        py_file: The source file being scanned (used in the error message).

    Raises:
        AssertionError: If `name` contains a forbidden symbol, with a message
                        that names the offending file and the forbidden symbol.
    """
    for forbidden in FORBIDDEN_SYMBOLS:
        # str.__contains__ (the "in" operator on strings) is a substring check —
        # equivalent to std::string::find() != std::string::npos in C++.
        if forbidden in name:
            raise AssertionError(
                f"Forbidden import found in {py_file}: "
                f"'{name}' contains forbidden symbol '{forbidden}'. "
                f"The data_ingestion module is read-only and must never "
                f"import order-placement clients."
            )
