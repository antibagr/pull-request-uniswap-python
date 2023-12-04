from typing import Any


class InvalidToken(Exception):
    """Raised when an invalid token address is used."""

    def __init__(self, address: Any) -> None:
        Exception.__init__(self, f"Invalid token address: {address}")


class InsufficientBalance(Exception):
    """Raised when the account has insufficient balance for a transaction."""

    def __init__(self, had: int, needed: int) -> None:
        Exception.__init__(self, f"Insufficient balance. Had {had}, needed {needed}")


class UnknownNetworkId(Exception):
    """Raised when an unknown network id is used."""


class UnsupportedUniSwapVersion(Exception):
    """Raised when an unsupported UniSwap version is used."""

    def __init__(self, version: int) -> None:
        Exception.__init__(self, f"Unsupported UniSwap version: {version}")


class ExplicitFeeTierRequred(Exception):
    """
    Explicit fee tier is required for protocols that support multiple fee tiers (Uniswap V3 currently).
    """


class InvalidFeeTier(Exception):
    """
    Raised when an invalid fee tier is used for a protocol that supports multiple fee tiers (Uniswap V3 currently).
    """


class InvalidTokenArgument(Exception):
    """
    Raised when an invalid token argument is used.
    """


class UniswapUnsupportedFunctionality:
    """
    Container for exceptions that are raised when a functionality is not supported by a specific UniSwap version.
    """

    class V3:
        """
        Container for exceptions that are raised when a functionality is not supported by UniSwap V3.
        """

        class FeeOnTranfer(Exception):
            pass

        class CustomRoutes(Exception):
            pass
