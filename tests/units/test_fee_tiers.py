from uniswap.uniswap import Uniswap
from uniswap.dto.enums import FeeTier
from uniswap.dto.exceptions import ExplicitFeeTierRequred

import pytest

funcs = (
    Uniswap.get_price_input,
    Uniswap.get_price_output,
    "_get_eth_token_output_price",
    "_get_token_eth_output_price",
    "_get_token_token_output_price", # 2, 3
    "make_trade",
    "make_trade_output",
)


# Using Pytest generate tests to create tests
def pytest_generate_tests(metafunc):
    if "func" in metafunc.fixturenames:
        metafunc.parametrize("func", funcs)


def create_client(version: int) -> Uniswap:
    return Uniswap(
        address=None,
        private_key=None,
        version=version,
    )


# @pytest.mark.usefixtures("client", "web3")
class TestFeeTiers(object):
    def test_explicit_fee_tier_required_v3(self, func: str) -> None:
        client = create_client(version=3)
        with pytest.raises(ExplicitFeeTierRequred) as exc:
            func(client, token0="0x00000000", token1="0x00000000", qty=1, fee=None)
