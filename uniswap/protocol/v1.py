import functools
import logging
from typing import Any, List, Optional, Tuple, Union

from eth_typing.evm import Address
from hexbytes import HexBytes
from web3 import Web3
from web3.contract import Contract
from web3.types import Wei

from uniswap.decorators import check_approval, supports
from uniswap.dto.constants import (
    _factory_contract_addresses_v1,
    ETH_ADDRESS,
)
from uniswap.dto.entities.token import ERC20Token
from uniswap.dto.exceptions import (
    InvalidToken,
    UnsupportedUniSwapVersion,
)
from uniswap.dto.types import AddressLike
from uniswap.protocol.base import BaseUniswap, UniswapV1V2Extention
from uniswap.tokens import get_token, get_tokens
from uniswap.util import (
    _load_contract,
    _load_contract_erc20,
    _str_to_addr,
)

logger = logging.getLogger(__name__)


class UniswapV1Utils:
    def __init__(
        self,
        contract: Contract,
        w3: Web3,
    ) -> None:
        self._contract = contract
        self._w3 = w3

    # ------ ERC20 Pool ----------------------------------------------------------------
    def get_ex_eth_balance(self, token: AddressLike) -> int:
        """Get the balance of ETH in an exchange contract."""
        ex_addr: AddressLike = self._exchange_address_from_token(token)
        return self._w3.eth.get_balance(ex_addr)

    def get_ex_token_balance(self, token: AddressLike) -> int:
        """Get the balance of a token in an exchange contract."""
        erc20 = _load_contract_erc20(self._w3, token)
        balance: int = erc20.functions.balanceOf(self._exchange_address_from_token(token)).call()
        return balance

    # TODO: ADD TOTAL SUPPLY
    def get_exchange_rate(self, token: AddressLike) -> float:
        """Get the current ETH/token exchange rate of the token."""
        eth_reserve = self.get_ex_eth_balance(token)
        token_reserve = self.get_ex_token_balance(token)
        return float(token_reserve / eth_reserve)

    # ------ Old v1 utils --------------------------------------------------------------
    @supports([1])
    def _exchange_address_from_token(self, token_addr: AddressLike) -> AddressLike:
        ex_addr: AddressLike = self._contract.functions.getExchange(token_addr).call()
        # TODO: What happens if the token doesn't have an exchange/doesn't exist?
        #       Should probably raise an Exception (and test it)
        return ex_addr

    @supports([1])
    def _token_address_from_exchange(self, exchange_addr: AddressLike) -> Address:
        token_addr: Address = (
            self.exchange(ex_addr=exchange_addr).functions.tokenAddress(exchange_addr).call()
        )
        return token_addr

    @functools.lru_cache()
    @supports([1])
    def exchange(
        self,
        token_addr: Optional[AddressLike] = None,
        ex_addr: Optional[AddressLike] = None,
    ) -> Contract:
        if not ex_addr and token_addr:
            ex_addr = self._exchange_address_from_token(token_addr)
        if ex_addr is None:
            raise InvalidToken(token_addr)
        abi_name = "uniswap-v1/exchange"
        contract = _load_contract(self._w3, abi_name=abi_name, address=ex_addr)
        logger.info(f"Loaded exchange contract {contract} at {contract.address}")
        return contract

    @supports([1])
    def _get_all_tokens(self) -> List[ERC20Token]:
        """
        Retrieves all token pairs.

        Note: This is a *very* expensive operation and might therefore not work properly.
        """
        # FIXME: This is a very expensive operation, would benefit greatly from caching.
        tokenCount = self._contract.functions.tokenCount().call()
        tokens = []
        for idx in range(tokenCount):
            address = self._contract.functions.getTokenWithId(idx).call()
            if address == ETH_ADDRESS:
                # Token is ETH
                continue
            token = get_token(w3=self._w3, address=address)
            tokens.append(token)
        return tokens


class UniswapV1(BaseUniswap, UniswapV1V2Extention):
    def __init__(
        self,
        address: Union[AddressLike, str, None],
        private_key: Optional[str],
        provider: Optional[str] = None,
        web3: Optional[Web3] = None,
        default_slippage: float = 0.01,
        use_estimate_gas: bool = True,
        enable_caching: bool = False,
        factory_contract_addr: Optional[str] = None,
    ) -> None:
        super().__init__(
            address=address,
            private_key=private_key,
            provider=provider,
            web3=web3,
            default_slippage=default_slippage,
            use_estimate_gas=use_estimate_gas,
            enable_caching=enable_caching,
            factory_contract_addr=factory_contract_addr,
        )
        self._utils = UniswapV1Utils(contract=self.factory_contract, w3=self.w3)

    def _create_factory_contract(self, address: AddressLike) -> Contract:
        address = address or _factory_contract_addresses_v1[self.netname]
        return _load_contract(
            self.w3,
            abi_name="uniswap-v1/factory",
            address=_str_to_addr(address),
        )

    # ------ Market --------------------------------------------------------------------

    def _get_eth_token_input_price(
        self,
        token: AddressLike,  # output token
        qty: Wei,
        fee: int,
    ) -> Wei:
        """
        Public price (i.e. amount of output token received) for ETH to
        token trades with an exact input.
        """
        ex = self._utils.exchange(token)
        return ex.functions.getEthToTokenInputPrice(qty).call()

    def _get_token_eth_input_price(
        self,
        token: AddressLike,  # input token
        qty: int,
        fee: int,
    ) -> int:
        """
        Public price (i.e. amount of ETH received) for token to ETH trades
        with an exact input.
        """
        ex = self._utils.exchange(token)
        return ex.functions.getTokenToEthInputPrice(qty).call()

    def _get_eth_token_output_price(
        self,
        token: AddressLike,  # output token
        qty: int,
        fee: Optional[int] = None,
    ) -> Wei:
        """
        Public price (i.e. amount of ETH needed) for ETH to token trades with
        an exact output.
        """
        ex = self._utils.exchange(token)
        return ex.functions.getEthToTokenOutputPrice(qty).call()

    def _get_token_eth_output_price(
        self,
        token: AddressLike,
        qty: Wei,
        fee: Optional[int] = None,
    ) -> int:
        """
        Public price (i.e. amount of input token needed) for token to ETH
        trades with an exact output.
        """
        ex = self._utils.exchange(token)
        return ex.functions.getTokenToEthOutputPrice(qty).call()

    def _get_token_token_input_price(
        self,
        token0: AddressLike,  # input token
        token1: AddressLike,  # output token
        qty: int,
        fee: int,
        route: Optional[List[AddressLike]] = None,
    ) -> int:
        raise UnsupportedUniSwapVersion(self._version)

    def _get_token_token_output_price(
        self,
        token0: AddressLike,  # input token
        token1: AddressLike,  # output token
        qty: int,
        fee: Optional[int] = None,
        route: Optional[List[AddressLike]] = None,
    ) -> int:
        raise UnsupportedUniSwapVersion(self._version)

    # ------ Make Trade ----------------------------------------------------------------

    def _eth_to_token_swap_input(
        self,
        output_token: AddressLike,
        qty: Wei,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
        fee_on_transfer: bool = False,
    ) -> HexBytes:
        """Convert ETH to tokens given an input amount."""
        token_funcs = self._utils.exchange(output_token).functions
        tx_params = self._get_tx_params(qty)
        func_params: List[Any] = [qty, self._deadline()]
        if not recipient:
            function = token_funcs.ethToTokenSwapInput(*func_params)
        else:
            func_params.append(recipient)
            function = token_funcs.ethToTokenTransferInput(*func_params)
        return self._build_and_send_tx(function, tx_params)

    def _token_to_eth_swap_input(
        self,
        input_token: AddressLike,
        qty: int,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
        fee_on_transfer: bool = False,
    ) -> HexBytes:
        """Convert tokens to ETH given an input amount."""

        token_funcs = self._utils.exchange(input_token).functions
        func_params: List[Any] = [qty, 1, self._deadline()]
        if not recipient:
            function = token_funcs.tokenToEthSwapInput(*func_params)
        else:
            func_params.append(recipient)
            function = token_funcs.tokenToEthTransferInput(*func_params)
        return self._build_and_send_tx(function)

    def _token_to_token_swap_input(
        self,
        input_token: AddressLike,
        output_token: AddressLike,
        qty: int,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
        fee_on_transfer: bool = False,
    ) -> HexBytes:
        """Convert tokens to tokens given an input amount."""

        token_funcs = self._utils.exchange(input_token).functions
        # TODO: This might not be correct
        min_tokens_bought, min_eth_bought = self._calculate_max_output_token(
            input_token, qty, output_token
        )
        func_params = [
            qty,
            min_tokens_bought,
            min_eth_bought,
            self._deadline(),
            output_token,
        ]
        if not recipient:
            function = token_funcs.tokenToTokenSwapInput(*func_params)
        else:
            func_params.insert(len(func_params) - 1, recipient)
            function = token_funcs.tokenToTokenTransferInput(*func_params)
        return self._build_and_send_tx(function)

    def _eth_to_token_swap_output(
        self,
        output_token: AddressLike,
        qty: int,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
    ) -> HexBytes:
        """Convert ETH to tokens given an output amount."""
        token_funcs = self._utils.exchange(output_token).functions
        eth_qty = self._get_eth_token_output_price(output_token, qty)
        tx_params = self._get_tx_params(eth_qty)
        func_params: List[Any] = [qty, self._deadline()]
        if not recipient:
            function = token_funcs.ethToTokenSwapOutput(*func_params)
        else:
            func_params.append(recipient)
            function = token_funcs.ethToTokenTransferOutput(*func_params)
        return self._build_and_send_tx(function, tx_params)

    def _token_to_eth_swap_output(
        self,
        input_token: AddressLike,
        qty: Wei,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
    ) -> HexBytes:
        """Convert tokens to ETH given an output amount."""
        # From https://uniswap.org/docs/v1/frontend-integration/trade-tokens/
        # Is all this really necessary? Can't we just use `cost` for max_tokens?
        outputAmount = qty
        inputReserve = self._utils.get_ex_token_balance(input_token)
        outputReserve = self._utils.get_ex_eth_balance(input_token)

        numerator = outputAmount * inputReserve * 1000
        denominator = (outputReserve - outputAmount) * 997
        inputAmount = numerator / denominator + 1

        max_tokens = int((1 + slippage) * inputAmount)

        ex = self._utils.exchange(input_token)
        func_params: List[Any] = [qty, max_tokens, self._deadline()]
        if not recipient:
            function = ex.functions.tokenToEthSwapOutput(*func_params)
        else:
            func_params.append(recipient)
            function = ex.functions.tokenToEthTransferOutput(*func_params)
        return self._build_and_send_tx(function)

    def _token_to_token_swap_output(
        self,
        input_token: AddressLike,
        output_token: AddressLike,
        qty: int,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
    ) -> HexBytes:
        """Convert tokens to tokens given an output amount.

        :param fee: TODO
        """
        token_funcs = self._utils.exchange(input_token).functions
        max_tokens_sold, max_eth_sold = self._calculate_max_input_token(
            input_token, qty, output_token
        )
        tx_params = self._get_tx_params()
        func_params = [
            qty,
            max_tokens_sold,
            max_eth_sold,
            self._deadline(),
            output_token,
        ]
        if not recipient:
            function = token_funcs.tokenToTokenSwapOutput(*func_params)
        else:
            func_params.insert(len(func_params) - 1, recipient)
            function = token_funcs.tokenToTokenTransferOutput(*func_params)
        return self._build_and_send_tx(function, tx_params)

    # ------ Price Calculation Utils ---------------------------------------------------
    def _calculate_max_input_token(
        self, input_token: AddressLike, qty: int, output_token: AddressLike
    ) -> Tuple[int, int]:
        """
        For buy orders (exact output), the cost (input) is calculated.
        Calculate the max input and max eth sold for a token to token output swap.
        Equation from:
         - https://hackmd.io/hthz9hXKQmSyXfMbPsut1g
         - https://uniswap.org/docs/v1/frontend-integration/trade-tokens/
        """
        # Buy TokenB with ETH
        output_amount_b = qty
        input_reserve_b = self._utils.get_ex_eth_balance(output_token)
        output_reserve_b = self._utils.get_ex_token_balance(output_token)

        # Cost
        numerator_b = output_amount_b * input_reserve_b * 1000
        denominator_b = (output_reserve_b - output_amount_b) * 997
        input_amount_b = numerator_b / denominator_b + 1

        # Buy ETH with TokenA
        output_amount_a = input_amount_b
        input_reserve_a = self._utils.get_ex_token_balance(input_token)
        output_reserve_a = self._utils.get_ex_eth_balance(input_token)

        # Cost
        numerator_a = output_amount_a * input_reserve_a * 1000
        denominator_a = (output_reserve_a - output_amount_a) * 997
        input_amount_a = numerator_a / denominator_a - 1

        return int(input_amount_a), int(1.2 * input_amount_b)

    def _calculate_max_output_token(
        self, output_token: AddressLike, qty: int, input_token: AddressLike
    ) -> Tuple[int, int]:
        """
        For sell orders (exact input), the amount bought (output) is calculated.
        Similar to _calculate_max_input_token, but for an exact input swap.
        """
        # TokenA (ERC20) to ETH conversion
        inputAmountA = qty
        inputReserveA = self._utils.get_ex_token_balance(input_token)
        outputReserveA = self._utils.get_ex_eth_balance(input_token)

        # Cost
        numeratorA = inputAmountA * outputReserveA * 997
        denominatorA = inputReserveA * 1000 + inputAmountA * 997
        outputAmountA = numeratorA / denominatorA

        # ETH to TokenB conversion
        inputAmountB = outputAmountA
        inputReserveB = self._utils.get_ex_token_balance(output_token)
        outputReserveB = self._utils.get_ex_eth_balance(output_token)

        # Cost
        numeratorB = inputAmountB * outputReserveB * 997
        denominatorB = inputReserveB * 1000 + inputAmountB * 997
        outputAmountB = numeratorB / denominatorB

        return int(outputAmountB), int(1.2 * outputAmountA)

    # ------ Liquidity -----------------------------------------------------------------
    @check_approval
    def add_liquidity(self, token: AddressLike, max_eth: Wei, min_liquidity: int = 1) -> HexBytes:
        """Add liquidity to the pool."""
        tx_params = self._get_tx_params(max_eth)
        # Add 1 to avoid rounding errors, per
        # https://hackmd.io/hthz9hXKQmSyXfMbPsut1g#Add-Liquidity-Calculations
        max_token = int(max_eth * self._utils.get_exchange_rate(token)) + 10
        func_params = [min_liquidity, max_token, self._deadline()]
        function = self._utils.exchange(token).functions.addLiquidity(*func_params)
        return self._build_and_send_tx(function, tx_params)

    @check_approval
    def remove_liquidity(self, token: str, max_token: int) -> HexBytes:
        """Remove liquidity from the pool."""
        func_params = [int(max_token), 1, 1, self._deadline()]
        function = self._utils.exchange(token).functions.removeLiquidity(*func_params)
        return self._build_and_send_tx(function)
