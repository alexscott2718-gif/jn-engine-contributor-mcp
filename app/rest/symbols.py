"""Protected structured-symbol REST adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.symbol_index import SymbolIndex, SymbolRequestError
from app.models.symbols import SymbolLookupOutput, symbol_lookup_output
from app.rest.errors import GatewayHTTPError

ContributorDependency = Callable[..., object]


def create_symbols_router(
    symbols: SymbolIndex,
    require_contributor: ContributorDependency,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_contributor)])

    @router.get("/v1/re/symbols", response_model=SymbolLookupOutput)
    def lookup_symbol(
        name: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        address: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
        class_name: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        fourcc: Annotated[str | None, Query(min_length=4, max_length=4)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ) -> SymbolLookupOutput:
        try:
            result = symbols.lookup(
                name=name,
                address=address,
                class_name=class_name,
                fourcc=fourcc,
                limit=limit,
            )
            return symbol_lookup_output(result, symbols.snapshot)
        except SymbolRequestError as exc:
            raise GatewayHTTPError(
                400,
                "invalid_request",
                "symbol parameters are invalid",
            ) from exc

    return router
