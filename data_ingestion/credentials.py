"""
credentials.py — Alpaca API credential loading for the data_ingestion module.

This is the ONLY file in data_ingestion/ that reads credentials. No other
module should access APCA_API_KEY_ID or APCA_API_SECRET_KEY directly.

Security contract (Requirement 1.3):
    The VALUES of these credentials must NEVER be logged, printed, or included
    in any exception message. Only the variable NAMES may appear in output.
    This is enforced by the implementation below — inspect carefully before
    adding any logging or print statements to this file.

Requirements: 1.1, 1.2, 1.3
"""

import os

# dotenv is a third-party library (python-dotenv package) that reads a .env
# file from disk and copies each KEY=VALUE line into os.environ. This is the
# standard Python pattern for loading secrets from a file without hard-coding
# them in source code — equivalent to reading a config file at startup in C++.
import dotenv


def load_credentials() -> tuple[str, str]:
    """
    Load APCA_API_KEY_ID and APCA_API_SECRET_KEY from the .env file.

    The function looks for .env in the current working directory (or any
    parent directory) — the default behaviour of python-dotenv's load_dotenv().

    Returns:
        A tuple (api_key, secret_key), both guaranteed to be non-empty strings.
        In Python, a tuple[str, str] is like a std::pair<std::string, std::string>
        in C++ — an immutable, fixed-size sequence of two values.

    Raises:
        EnvironmentError: If the .env file is absent, or if either variable is
                          missing from the file or resolves to an empty string.
                          The error message names the specific variable, but
                          NEVER includes its value.
    """

    # context manager-free dotenv call — modifies os.environ in place
    # (Unlike a "with open(...)" context manager, load_dotenv() is a plain
    # function call. It opens the .env file internally, reads each line, and
    # writes each KEY=VALUE pair into os.environ. After this line returns,
    # os.environ contains the new variables. If the .env file is missing,
    # load_dotenv() silently does nothing — the missing variables will be caught
    # by the validation loop below, which raises EnvironmentError as required.)
    dotenv.load_dotenv()

    # os.getenv(key) is Python's equivalent of std::getenv(key) in C:
    # it returns the string value if the variable is set, or None if it is not.
    # We use it inside the loop below instead of os.environ[key] because the
    # dict-style access raises KeyError on a missing key — we want None so we
    # can give a uniform error message for both "absent" and "empty" cases.

    # Validate that both required keys are present and non-empty.
    # We check them in a specific order so that the error message always names
    # the FIRST missing key (matching the order Alpaca expects them).
    required_keys: list[str] = ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY"]

    # A list literal in Python is like std::vector in C++ — dynamically sized,
    # ordered, and heterogeneous. Here it holds the two key name strings.
    for key in required_keys:
        # Iterate over the list with a simple for-in loop.
        # Python's for-in loop is the equivalent of C++11's range-based for:
        #   for (const auto& key : required_keys)
        # Each iteration binds 'key' to the next element in the list.

        value: str | None = os.getenv(key)
        # str | None is Python's type annotation for "either a str or None".
        # This is equivalent to std::optional<std::string> in C++17.

        if value is None or value == "":
            # The variable is either absent from the environment (None) or was
            # explicitly set to an empty string in the .env file ("").
            # Both cases are treated as invalid — an empty key is as useless
            # as a missing one.
            #
            # f-strings (f"...") are Python's formatted string literals —
            # equivalent to std::format("...", key) in C++20. Curly braces {}
            # embed the value of the enclosed expression directly into the string.
            #
            # SECURITY: We only embed 'key' (the VARIABLE NAME, e.g.,
            # "APCA_API_KEY_ID") — never 'value' (the credential VALUE).
            raise EnvironmentError(f"Missing or empty credential: {key}")

    # Both variables passed validation. Retrieve their values.
    # At this point os.getenv() is guaranteed to return a non-empty string
    # for both keys, so we assign directly without another None-check.
    api_key: str = os.getenv("APCA_API_KEY_ID")      # type: ignore[assignment]
    secret_key: str = os.getenv("APCA_API_SECRET_KEY")  # type: ignore[assignment]

    # Return as a two-element tuple. The caller unpacks it like:
    #   api_key, secret_key = load_credentials()
    # This is Python's tuple unpacking — equivalent to std::tie() in C++.
    #
    # SECURITY: This tuple is returned directly to the caller. The values must
    # never be passed to any logging or print function downstream. They should
    # only travel to the Alpaca SDK constructor.
    return (api_key, secret_key)
