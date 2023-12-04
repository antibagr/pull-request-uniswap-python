import enum
import functools
import logging
import os
import time
from collections import namedtuple
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from eth_typing.evm import Address, ChecksumAddress
from hexbytes import HexBytes
from web3 import Web3
from web3._utils.abi import map_abi_data
from web3._utils.normalizers import BASE_RETURN_NORMALIZERS
from web3.contract import Contract
from web3.contract.contract import ContractFunction
from web3.exceptions import BadFunctionCallOutput, ContractLogicError
from web3.types import Nonce, TxParams, TxReceipt, Wei

from uniswap.dto.constants import (
    _factory_contract_addresses_v1,
    _factory_contract_addresses_v2,
    _netid_to_name,
    _router_contract_addresses_v2,
    _tick_bitmap_range,
    _tick_spacing,
    EMPTY_PRIVATE_KEY,
    ETH_ADDRESS,
    MAX_TICK,
    MAX_UINT_128,
    MIN_TICK,
    WETH9_ADDRESS,
)
from uniswap.dto.entities.token import ERC20Token
from uniswap.dto.enums import FeeTier
from uniswap.dto.exceptions import (
    InsufficientBalance,
    InvalidToken,
    InvalidTokenArgument,
    UniswapUnsupportedFunctionality,
    UnknownNetworkId,
    UnsupportedUniSwapVersion,
)
from uniswap.dto.types import AddressLike
from uniswap.util import (
    _addr_to_str,
    _get_eth_simple_cache_middleware,
    _load_contract,
    _load_contract_erc20,
    _str_to_addr,
    _validate_address,
    chunks,
    create_web3,
    encode_sqrt_ratioX96,
    is_same_address,
    nearest_tick,
    realised_fee_percentage,
)

from uniswap.decorators import check_approval, supports

logger = logging.getLogger(__name__)


class BaseUniswap:
    """
    Wrapper around Uniswap contracts.
    """

    address: AddressLike
    version: int

    w3: Web3
    netid: int
    netname: str

    default_slippage: float
    use_estimate_gas: bool

    _version: int = 0

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
        """
        :param address: The public address of the ETH wallet to use.
        :param private_key: The private key of the ETH wallet to use.
        :param provider: Can be optionally set to a Web3 provider URI. If none set, will fall back to the PROVIDER environment variable, or web3 if set.
        :param web3: Can be optionally set to a custom Web3 instance.
        :param version: Which version of the Uniswap contracts to use.
        :param default_slippage: Default slippage for a trade, as a float (0.01 is 1%). WARNING: slippage is untested.
        :param factory_contract_addr: Can be optionally set to override the address of the factory contract.
        :param router_contract_addr: Can be optionally set to override the address of the router contract (v2 only).
        :param enable_caching: Optionally enables middleware caching RPC method calls.
        """
        self.address = _str_to_addr(address or ETH_ADDRESS)
        self.private_key = private_key or EMPTY_PRIVATE_KEY

        # TODO: Write tests for slippage
        self.default_slippage = default_slippage
        self.use_estimate_gas = use_estimate_gas
        self.w3 = web3 or create_web3(provider=provider)

        if enable_caching:
            self.w3.middleware_onion.inject(_get_eth_simple_cache_middleware(), layer=0)

        self.netid = int(self.w3.net.version)
        try:
            self.netname = _netid_to_name[self.netid]
        except KeyError as exc:
            raise UnknownNetworkId(f"Unknown netid: {self.netid}") from exc

        logger.info(f"Using {self.w3} ('{self.netname}', netid: {self.netid})")

        self.last_nonce: Nonce = self.w3.eth.get_transaction_count(self.address)

        # This code automatically approves you for trading on the exchange.
        # max_approval is to allow the contract to exchange on your behalf.
        # max_approval_check checks that current approval is above a reasonable number
        # The program cannot check for max_approval each time because it decreases
        # with each trade.
        self.max_approval_int = int(f"0x{'f' * 64}", 16)
        max_approval_check_hex = f"0x{'0' * 15}{'f' * 49}"
        self.max_approval_check_int = int(max_approval_check_hex, 16)
        self.factory_contract = self._create_factory_contract(factory_contract_addr)
        logger.info(f"Using factory contract: {self.factory_contract}")

    def _create_factory_contract(self, address: AddressLike) -> Contract:
        raise NotImplementedError

    # ------ Market --------------------------------------------------------------------

    def get_price_input(
        self,
        token0: AddressLike,  # input token
        token1: AddressLike,  # output token
        qty: int,
        fee: Optional[int] = None,
        route: Optional[List[AddressLike]] = None,
    ) -> int:
        """Given `qty` amount of the input `token0`, returns the maximum output amount of output `token1`."""
        fee = FeeTier.create(value=fee, version=self._version)

        if token0 == ETH_ADDRESS:
            return self._get_eth_token_input_price(token1, Wei(qty), fee)
        elif token1 == ETH_ADDRESS:
            return self._get_token_eth_input_price(token0, qty, fee)
        else:
            return self._get_token_token_input_price(token0, token1, qty, fee, route)

    def get_price_output(
        self,
        token0: AddressLike,
        token1: AddressLike,
        qty: int,
        fee: Optional[int] = None,
        route: Optional[List[AddressLike]] = None,
    ) -> int:
        """Returns the minimum amount of `token0` required to buy `qty` amount of `token1`."""
        fee = FeeTier.create(value=fee, version=self._version)

        if is_same_address(token0, ETH_ADDRESS):
            return self._get_eth_token_output_price(token1, qty, fee)
        elif is_same_address(token1, ETH_ADDRESS):
            return self._get_token_eth_output_price(token0, Wei(qty), fee)
        else:
            return self._get_token_token_output_price(token0, token1, qty, fee, route)

    def _get_eth_token_input_price(
        self,
        token: AddressLike,  # output token
        qty: Wei,
        fee: int,
    ) -> Wei:
        """Public price (i.e. amount of output token received) for ETH to token trades with an exact input."""
        raise NotImplementedError

    def _get_token_eth_input_price(
        self,
        token: AddressLike,  # input token
        qty: int,
        fee: int,
    ) -> int:
        """Public price (i.e. amount of ETH received) for token to ETH trades with an exact input."""
        raise NotImplementedError

    def _get_token_token_input_price(
        self,
        token0: AddressLike,  # input token
        token1: AddressLike,  # output token
        qty: int,
        fee: int,
        route: Optional[List[AddressLike]] = None,
    ) -> int:
        """
        Public price (i.e. amount of output token received) for token to token trades with an exact input.

        :param fee: (v3 only) The pool's fee in hundredths of a bip, i.e. 1e-6 (3000 is 0.3%)
        """
        raise NotImplementedError

    def _get_eth_token_output_price(
        self,
        token: AddressLike,  # output token
        qty: int,
        fee: Optional[int] = None,
    ) -> Wei:
        """Public price (i.e. amount of ETH needed) for ETH to token trades with an exact output."""
        raise NotImplementedError

    def _get_token_eth_output_price(
        self, token: AddressLike, qty: Wei, fee: Optional[int] = None  # input token
    ) -> int:
        """Public price (i.e. amount of input token needed) for token to ETH trades with an exact output."""

        raise NotImplementedError

    def _get_token_token_output_price(
        self,
        token0: AddressLike,  # input token
        token1: AddressLike,  # output token
        qty: int,
        fee: Optional[int] = None,
        route: Optional[List[AddressLike]] = None,
    ) -> int:
        """
        Public price (i.e. amount of input token needed) for token to token trades with an exact output.

        :param fee: (v3 only) The pool's fee in hundredths of a bip, i.e. 1e-6 (3000 is 0.3%)
        """

        raise NotImplementedError

    # ------ Make Trade ----------------------------------------------------------------
    @check_approval
    def make_trade(
        self,
        input_token: AddressLike,
        output_token: AddressLike,
        qty: Union[int, Wei],
        recipient: Optional[AddressLike] = None,
        fee: Optional[int] = None,
        slippage: Optional[float] = None,
        fee_on_transfer: bool = False,
    ) -> HexBytes:
        """Make a trade by defining the qty of the input token."""
        if not isinstance(qty, int):
            raise TypeError("swapped quantity must be an integer")

        fee = FeeTier.create(value=fee, version=self._version)

        if slippage is None:
            slippage = self.default_slippage

        if input_token == output_token:
            raise InvalidTokenArgument("input and output tokens cannot be the same")

        if input_token == ETH_ADDRESS:
            return self._wrap_eth_to_token_swap_input(
                output_token, Wei(qty), recipient, fee, slippage, fee_on_transfer
            )
        elif output_token == ETH_ADDRESS:
            return self._wrap_token_to_eth_swap_input(
                input_token, qty, recipient, fee, slippage, fee_on_transfer
            )
        else:
            return self._wrap_token_to_token_swap_input(
                input_token,
                output_token,
                qty,
                recipient,
                fee,
                slippage,
                fee_on_transfer,
            )

    @check_approval
    def make_trade_output(
        self,
        input_token: AddressLike,
        output_token: AddressLike,
        qty: Union[int, Wei],
        recipient: Optional[AddressLike] = None,
        fee: Optional[int] = None,
        slippage: Optional[float] = None,
    ) -> HexBytes:
        """Make a trade by defining the qty of the output token."""
        fee = FeeTier.create(value=fee, version=self._version)

        if slippage is None:
            slippage = self.default_slippage

        if input_token == output_token:
            raise InvalidTokenArgument("input and output tokens cannot be the same")

        if input_token == ETH_ADDRESS:
            balance = self.get_eth_balance()
            need = self._get_eth_token_output_price(output_token, qty, fee)
            if balance < need:
                raise InsufficientBalance(balance, need)
            return self._wrap_eth_to_token_swap_output(output_token, qty, recipient, fee, slippage)
        elif output_token == ETH_ADDRESS:
            return self._wrap_token_to_eth_swap_output(
                input_token, Wei(qty), recipient, fee, slippage
            )
        else:
            return self._wrap_token_to_token_swap_output(
                input_token, output_token, qty, recipient, fee, slippage
            )

    def _wrap_eth_to_token_swap_input(
        self,
        output_token: AddressLike,
        qty: Wei,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
        fee_on_transfer: bool = False,
    ) -> HexBytes:
        """Convert ETH to tokens given an input amount."""

        if output_token == ETH_ADDRESS:
            raise InvalidTokenArgument("output token cannot be ETH")

        eth_balance = self.get_eth_balance()
        if qty > eth_balance:
            raise InsufficientBalance(eth_balance, qty)

        return self._eth_to_token_swap_input(
            output_token=output_token,
            qty=qty,
            recipient=recipient,
            fee=fee,
            slippage=slippage,
            fee_on_transfer=fee_on_transfer,
        )

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

        raise NotImplementedError

    def _wrap_token_to_eth_swap_input(
        self,
        input_token: AddressLike,
        qty: int,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
        fee_on_transfer: bool = False,
    ) -> HexBytes:
        """Convert tokens to ETH given an input amount."""

        if input_token == ETH_ADDRESS:
            raise InvalidTokenArgument("input token cannot be ETH")

        # Balance check
        input_balance = self.get_token_balance(input_token)
        if qty > input_balance:
            raise InsufficientBalance(input_balance, qty)
        return self._token_to_eth_swap_input(
            input_token=input_token,
            qty=qty,
            recipient=recipient,
            fee=fee,
            slippage=slippage,
            fee_on_transfer=fee_on_transfer,
        )

    def _token_to_eth_swap_input(
        self,
        input_token: AddressLike,
        qty: int,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
        fee_on_transfer: bool = False,
    ) -> HexBytes:
        raise NotImplementedError

    def _wrap_token_to_token_swap_input(
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
        # Balance check
        input_balance = self.get_token_balance(input_token)
        if qty > input_balance:
            raise InsufficientBalance(input_balance, qty)

        if ETH_ADDRESS in (input_token, output_token):
            raise InvalidTokenArgument("ETH cannot be input or output token")

        recipient = recipient or self.address

        return self._token_to_token_swap_input(
            input_token=input_token,
            output_token=output_token,
            qty=qty,
            recipient=recipient,
            fee=fee,
            slippage=slippage,
            fee_on_transfer=fee_on_transfer,
        )

    def _token_to_token_swap_input(
        self,
        input_token: AddressLike,
        output_token: AddressLike,
        qty: int,
        recipient: AddressLike,
        fee: int,
        slippage: float,
        fee_on_transfer: bool = False,
    ) -> HexBytes:
        """Convert tokens to tokens given an input amount."""

        raise NotImplementedError

    def _wrap_eth_to_token_swap_output(
        self,
        output_token: AddressLike,
        qty: int,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
    ) -> HexBytes:
        """Convert ETH to tokens given an output amount."""
        if output_token == ETH_ADDRESS:
            raise InvalidTokenArgument("output token cannot be ETH")

        # Balance check
        eth_balance = self.get_eth_balance()
        cost = self._get_eth_token_output_price(output_token, qty, fee)
        amount_in_max = Wei(int((1 + slippage) * cost))

        # We check balance against amount_in_max rather than cost to be conservative
        if amount_in_max > eth_balance:
            raise InsufficientBalance(eth_balance, amount_in_max)

        return self._eth_to_token_swap_output(
            output_token=output_token,
            qty=qty,
            recipient=recipient,
            fee=fee,
            slippage=slippage,
        )

    def _eth_to_token_swap_output(
        self,
        output_token: AddressLike,
        qty: int,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
    ) -> HexBytes:
        """Convert ETH to tokens given an output amount."""

        raise NotImplementedError

    def _wrap_token_to_eth_swap_output(
        self,
        input_token: AddressLike,
        qty: Wei,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
    ) -> HexBytes:
        """Convert tokens to ETH given an output amount."""
        if input_token == ETH_ADDRESS:
            raise InvalidTokenArgument("input token cannot be ETH")

        # Balance check
        input_balance = self.get_token_balance(input_token)
        cost = self._get_token_eth_output_price(input_token, qty, fee)
        amount_in_max = int((1 + slippage) * cost)

        # We check balance against amount_in_max rather than cost to be conservative
        if amount_in_max > input_balance:
            raise InsufficientBalance(input_balance, amount_in_max)

        return self._token_to_eth_swap_output(
            input_token=input_token,
            qty=qty,
            recipient=recipient,
            fee=fee,
            slippage=slippage,
        )

    def _token_to_eth_swap_output(
        self,
        input_token: AddressLike,
        qty: Wei,
        recipient: Optional[AddressLike],
        fee: int,
        slippage: float,
    ) -> HexBytes:
        """Convert tokens to ETH given an output amount."""

        raise NotImplementedError

    def _wrap_token_to_token_swap_output(
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
        if ETH_ADDRESS in (input_token, output_token):
            raise InvalidTokenArgument("ETH cannot be input or output token")

        # Balance check
        input_balance = self.get_token_balance(input_token)
        cost = self._get_token_token_output_price(input_token, output_token, qty, fee)
        amount_in_max = int((1 + slippage) * cost)
        if (
            amount_in_max > input_balance
        ):  # We check balance against amount_in_max rather than cost to be conservative
            raise InsufficientBalance(input_balance, amount_in_max)

        return self._token_to_token_swap_output(
            input_token=input_token,
            output_token=output_token,
            qty=qty,
            recipient=recipient,
            fee=fee,
            slippage=slippage,
        )

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

        raise NotImplementedError

    # ------ Wallet balance ------------------------------------------------------------
    def get_eth_balance(self) -> Wei:
        """Get the balance of ETH for your address."""
        return self.w3.eth.get_balance(self.address)

    def get_token_balance(self, token: AddressLike) -> int:
        """Get the balance of a token for your address."""
        _validate_address(token)
        if _addr_to_str(token) == ETH_ADDRESS:
            return self.get_eth_balance()
        erc20 = _load_contract_erc20(self.w3, token)
        balance: int = erc20.functions.balanceOf(self.address).call()
        return balance

    # ------ Tx Utils ------------------------------------------------------------------
    def _deadline(self) -> int:
        """Get a predefined deadline. 10min by default (same as the Uniswap SDK)."""
        return int(time.time()) + 10 * 60

    def _build_and_send_tx(
        self, function: ContractFunction, tx_params: Optional[TxParams] = None
    ) -> HexBytes:
        """Build and send a transaction."""
        if not tx_params:
            tx_params = self._get_tx_params()
        transaction = function.build_transaction(tx_params)

        if "gas" not in tx_params:
            # `use_estimate_gas` needs to be True for networks like Arbitrum (can't assume 250000 gas),
            # but it breaks tests for unknown reasons because estimate_gas takes forever on some tx's.
            # Maybe an issue with ganache? (got GC warnings once...)
            if self.use_estimate_gas:
                # The Uniswap V3 UI uses 20% margin for transactions
                transaction["gas"] = Wei(int(self.w3.eth.estimate_gas(transaction) * 1.2))
            else:
                transaction["gas"] = Wei(250_000)

        signed_txn = self.w3.eth.account.sign_transaction(transaction, private_key=self.private_key)
        # TODO: This needs to get more complicated if we want to support replacing a transaction
        # FIXME: This does not play nice if transactions are sent from other places using the same wallet.
        try:
            return self.w3.eth.send_raw_transaction(signed_txn.rawTransaction)
        finally:
            logger.debug(f"nonce: {tx_params['nonce']}")
            self.last_nonce = Nonce(tx_params["nonce"] + 1)

    def _get_tx_params(self, value: Wei = Wei(0), gas: Optional[Wei] = None) -> TxParams:
        """Get generic transaction parameters."""
        params: TxParams = {
            "from": _addr_to_str(self.address),
            "value": value,
            "nonce": max(self.last_nonce, self.w3.eth.get_transaction_count(self.address)),
        }

        if gas:
            params["gas"] = gas

        return params

    # ------ Helpers ------------------------------------------------------------


class UniswapV1V2Extention:
    @supports([1, 2])
    def get_fee_maker(self) -> float:
        """Get the maker fee."""
        return 0

    @supports([1, 2])
    def get_fee_taker(self) -> float:
        """Get the taker fee."""
        return 0.003
