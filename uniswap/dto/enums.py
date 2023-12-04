import enum
import logging
from typing import final, Final, Optional, Self

from uniswap.dto.exceptions import ExplicitFeeTierRequred, InvalidFeeTier

logger: Final = logging.getLogger(__name__)


@final
@enum.unique
class FeeTier(enum.IntEnum):
    """
    Available fee tiers represented as 1e-6 percentages (i.e. 0.5% is 5000)

    V1 supports only 0.3% fee tier.
    V2 supports only 0.3% fee tier.
    V3 supports 1%, 0.3%, 0.05%, and 0.01% fee tiers.

    Reference: https://support.uniswap.org/hc/en-us/articles/20904283758349-What-are-fee-tiers
    """

    TIER_100 = 100
    TIER_500 = 500
    TIER_3000 = 3000
    TIER_10000 = 10000

    @classmethod
    def create(cls, value: Optional[int], version: int) -> Self:
        """
        Creates a `FeeTier` instance checking the protocol version requirements.
        """
        if version == 3 and value is None:
            raise ExplicitFeeTierRequred(
                """
                Explicit fee tier is required for Uniswap V3. Refer to the following link for more information:
                https://support.uniswap.org/hc/en-us/articles/20904283758349-What-are-fee-tiers
                """
            )

        value = value or FeeTier.TIER_3000

        if version < 3 and value != FeeTier.TIER_3000:
            raise InvalidFeeTier(
                f"Unsupported fee tier {value} for Uniswap V{version}. Choices are: {FeeTier.TIER_3000}"
            )
        try:
            return FeeTier(value)
        except ValueError as exc:
            raise InvalidFeeTier(
                f"Unsupported fee tier {value} for Uniswap V{version}. Choices are: {FeeTier.TIER_100}, {FeeTier.TIER_500}, {FeeTier.TIER_3000}, {FeeTier.TIER_10000}"
            ) from exc
