"""Isolated device OAuth provider for explicit break-glass operation.

Device mode is never combined with GitHub mode. It provides an enrollment-gated
OAuth 2.1 authorization server plus an RS256 resource-server verifier. Persistent
state is limited to the host-provisioned signing key and a private client registry;
authorization codes and enrollment transactions are short-lived and in memory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import urlencode

from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp.server.auth import AccessToken, OAuthProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.auth.redirect_validation import validate_redirect_uri
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from app.auth import AuthDecision, AuthDecisionState, Principal
from app.config import Settings, read_private_secret_bytes

logger = logging.getLogger(__name__)

JN_READ_SCOPE = "jn.read"
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
AUTH_CODE_TTL_SECONDS = 300
ENROLL_TXN_TTL_SECONDS = 300
MAX_ENROLL_ATTEMPTS = 5
ENROLL_FAILURE_DELAY_SECONDS = 1.0
CLIENT_REGISTRY_FILE = "device_oauth_clients.json"
MAX_REGISTERED_CLIENTS = 1_000
MAX_CLIENT_REGISTRY_BYTES = 1024 * 1024

_DEVICE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


def _write_private_file(path: Path, data: bytes) -> None:
    """Atomically replace one mode-0600 file in its existing private directory."""
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}."
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_private_registry(path: Path) -> bytes:
    """Read one bounded, regular, mode-0600 registry without following links."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("device OAuth client registry cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("device OAuth client registry must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("device OAuth client registry must have mode 0600")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            value = handle.read(MAX_CLIENT_REGISTRY_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(value) > MAX_CLIENT_REGISTRY_BYTES:
        raise ValueError("device OAuth client registry is too large")
    return value


@dataclass(frozen=True)
class SigningKey:
    """A validated RSA private key and its public verification material."""

    private_pem: str
    public_pem: str
    kid: str

    def public_jwk(self) -> dict[str, str]:
        key = JsonWebKey.import_key(self.public_pem, {"kty": "RSA"})
        return {
            **key.as_dict(),
            "kid": self.kid,
            "use": "sig",
            "alg": "RS256",
        }


def load_signing_key(path: Path) -> SigningKey:
    """Load the host-provisioned RSA key after config has checked file safety."""
    private_bytes = read_private_secret_bytes(
        path,
        label="OAuth JWT signing key",
        minimum_bytes=32,
    )
    try:
        private_key = serialization.load_pem_private_key(
            private_bytes,
            password=None,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "OAuth JWT signing key must be an unencrypted PKCS8 RSA private key"
        ) from exc
    if not isinstance(private_key, rsa.RSAPrivateKey) or private_key.key_size < 2048:
        raise ValueError("OAuth JWT signing key must be RSA with at least 2048 bits")

    private_pem = private_bytes.decode("ascii")
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    kid = JsonWebKey.import_key(public_pem, {"kty": "RSA"}).thumbprint()
    return SigningKey(
        private_pem=private_pem,
        public_pem=public_pem,
        kid=kid,
    )


class ClientRegistry:
    """Persist the bounded DCR registry in one private JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._clients: dict[str, OAuthClientInformationFull] = {}
        if not path.exists():
            return
        raw = _read_private_registry(path)
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("device OAuth client registry is malformed") from exc
        if not isinstance(document, dict) or len(document) > MAX_REGISTERED_CLIENTS:
            raise ValueError("device OAuth client registry is malformed or too large")
        try:
            self._clients = {
                client_id: OAuthClientInformationFull.model_validate(client)
                for client_id, client in document.items()
                if isinstance(client_id, str)
            }
        except Exception as exc:
            raise ValueError("device OAuth client registry is malformed") from exc
        if len(self._clients) != len(document):
            raise ValueError("device OAuth client registry has an invalid client ID")

    def get(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    def put(self, client: OAuthClientInformationFull) -> None:
        if client.client_id is None:
            raise ValueError("client_id is required for client registration")
        if (
            client.client_id not in self._clients
            and len(self._clients) >= MAX_REGISTERED_CLIENTS
        ):
            raise RegistrationError(
                "invalid_client_metadata",
                "device client registry capacity reached",
            )
        self._clients[client.client_id] = client
        payload = {
            client_id: item.model_dump(mode="json", exclude_none=True)
            for client_id, item in self._clients.items()
        }
        _write_private_file(
            self._path,
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        )


@dataclass
class _EnrollTransaction:
    client_id: str
    params: AuthorizationParams
    expires_at: float
    attempts: int = 0


class DeviceOAuthProvider(OAuthProvider):
    """Enrollment-gated OAuth provider and shared device authenticator."""

    def __init__(self, settings: Settings) -> None:
        if settings.auth_mode != "device":
            raise ValueError("DeviceOAuthProvider requires AUTH_MODE=device")
        os.umask(0o077)
        issuer = settings.public_base_url.rstrip("/")
        super().__init__(
            base_url=issuer,
            required_scopes=[JN_READ_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[JN_READ_SCOPE],
                default_scopes=[JN_READ_SCOPE],
            ),
            revocation_options=RevocationOptions(enabled=False),
        )
        self._settings = settings
        self._issuer = issuer
        self._audience = f"{issuer}{settings.mcp_path}"
        self._refresh_audience = f"{issuer}/token"
        self._allowed_redirects = list(settings.oauth_allowed_client_redirect_uris)
        self._key = load_signing_key(settings.oauth_jwt_signing_key_file)
        self._registry = ClientRegistry(
            settings.gateway_secrets_dir / CLIENT_REGISTRY_FILE
        )
        self._registry_lock = asyncio.Lock()
        self._jwt = JsonWebToken(["RS256"])
        self._verifier = JWTVerifier(
            public_key=self._key.public_pem,
            issuer=self._issuer,
            audience=self._audience,
            algorithm="RS256",
            required_scopes=[JN_READ_SCOPE],
        )
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._enroll_transactions: dict[str, _EnrollTransaction] = {}

    async def authenticate_and_authorize(self, token: str) -> AuthDecision:
        identity = await self._verifier.verify_token(token)
        if identity is None:
            return AuthDecision(AuthDecisionState.UNAUTHENTICATED)
        claims = identity.claims
        client_id = claims.get("client_id")
        subject = claims.get("sub")
        if (
            claims.get("token_use") != "access"
            or not isinstance(client_id, str)
            or not isinstance(subject, str)
            or _DEVICE_IDENTIFIER.fullmatch(client_id) is None
            or _DEVICE_IDENTIFIER.fullmatch(subject) is None
        ):
            return AuthDecision(AuthDecisionState.UNAUTHENTICATED)
        principal = Principal(
            provider="device",
            subject=f"device:{subject}",
            login=client_id,
            auth_mode="device",
        )
        return AuthDecision(
            AuthDecisionState.ALLOWED,
            principal=principal,
            access_token=identity,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        decision = await self.authenticate_and_authorize(token)
        return decision.access_token if decision.state is AuthDecisionState.ALLOWED else None

    async def verify_token(self, token: str) -> AccessToken | None:
        decision = await self.authenticate_and_authorize(token)
        return decision.access_token if decision.state is AuthDecisionState.ALLOWED else None

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._registry.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        redirects = client_info.redirect_uris or []
        if not redirects or any(
            not validate_redirect_uri(redirect, self._allowed_redirects)
            for redirect in redirects
        ):
            raise RegistrationError(
                "invalid_redirect_uri",
                "client redirect URI is not in the deployment allowlist",
            )
        async with self._registry_lock:
            self._registry.put(client_info)

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        self._prune()
        client_id = client.client_id or ""
        if not client_id:
            raise ValueError("registered client is missing client_id")
        transaction_id = secrets.token_urlsafe(32)
        self._enroll_transactions[transaction_id] = _EnrollTransaction(
            client_id=client_id,
            params=params,
            expires_at=time.time() + ENROLL_TXN_TTL_SECONDS,
        )
        return f"{self._issuer}/enroll?{urlencode({'txn': transaction_id})}"

    def _prune(self) -> None:
        now = time.time()
        self._enroll_transactions = {
            transaction_id: transaction
            for transaction_id, transaction in self._enroll_transactions.items()
            if transaction.expires_at > now
        }
        self._auth_codes = {
            code: authorization_code
            for code, authorization_code in self._auth_codes.items()
            if authorization_code.expires_at > now
        }

    def _get_transaction(self, transaction_id: str) -> _EnrollTransaction | None:
        transaction = self._enroll_transactions.get(transaction_id)
        if transaction is None or transaction.expires_at < time.time():
            self._enroll_transactions.pop(transaction_id, None)
            return None
        return transaction

    @staticmethod
    def _no_store(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    def _enroll_form(
        self,
        transaction_id: str,
        transaction: _EnrollTransaction,
        error: str | None = None,
    ) -> HTMLResponse:
        client = self._registry.get(transaction.client_id)
        client_name = (client.client_name if client else None) or transaction.client_id
        scopes = " ".join(transaction.params.scopes or [JN_READ_SCOPE])
        redirect_host = transaction.params.redirect_uri.host or ""
        error_html = f'<p class="error">{escape(error)}</p>' if error else ""
        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(self._settings.app_name)} device enrollment</title>
<style>body{{font-family:sans-serif;max-width:34rem;margin:4rem auto;padding:0 1rem}}
.error{{color:#9b1c1c}}code{{overflow-wrap:anywhere}}</style></head>
<body><h1>Enroll this device</h1>
<p><b>{escape(client_name)}</b> will redirect to
<code>{escape(redirect_host)}</code> and requests <code>{escape(scopes)}</code>.</p>
{error_html}
<form method="post" action="/enroll">
<input type="hidden" name="txn" value="{escape(transaction_id)}">
<label>Enrollment secret <input type="password" name="secret" required
autocomplete="off" autofocus></label>
<button type="submit">Approve</button>
</form></body></html>"""
        response = HTMLResponse(body, status_code=401 if error else 200)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return self._no_store(response)  # type: ignore[return-value]

    async def _enroll_endpoint(self, request: Request) -> Response:
        if request.method == "GET":
            transaction_id = request.query_params.get("txn", "")
            transaction = self._get_transaction(transaction_id)
            if transaction is None:
                return self._no_store(
                    JSONResponse(
                        {"detail": "unknown or expired enrollment request"},
                        status_code=400,
                    )
                )
            return self._enroll_form(transaction_id, transaction)

        form = await request.form()
        transaction_id = str(form.get("txn", ""))
        submitted = str(form.get("secret", ""))
        transaction = self._get_transaction(transaction_id)
        if transaction is None:
            return self._no_store(
                JSONResponse(
                    {"detail": "unknown or expired enrollment request"},
                    status_code=400,
                )
            )

        expected = self._settings.mcp_enrollment_secret()
        if not secrets.compare_digest(submitted.encode(), expected.encode()):
            transaction.attempts += 1
            logger.info(
                "device enrollment rejected for client %s (attempt %d/%d)",
                transaction.client_id,
                transaction.attempts,
                MAX_ENROLL_ATTEMPTS,
            )
            if transaction.attempts >= MAX_ENROLL_ATTEMPTS:
                self._enroll_transactions.pop(transaction_id, None)
            await asyncio.sleep(ENROLL_FAILURE_DELAY_SECONDS)
            if transaction_id not in self._enroll_transactions:
                return self._no_store(
                    JSONResponse(
                        {"detail": "enrollment request rejected"},
                        status_code=401,
                    )
                )
            return self._enroll_form(
                transaction_id,
                transaction,
                error="wrong enrollment secret",
            )

        self._enroll_transactions.pop(transaction_id, None)
        params = transaction.params
        client = self._registry.get(transaction.client_id)
        scopes = params.scopes or (
            client.scope.split() if client and client.scope else [JN_READ_SCOPE]
        )
        code_value = secrets.token_urlsafe(32)
        self._auth_codes[code_value] = AuthorizationCode(
            code=code_value,
            client_id=transaction.client_id,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            scopes=scopes,
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
            code_challenge=params.code_challenge,
        )
        logger.info("device enrolled for client %s", transaction.client_id)
        return self._no_store(
            RedirectResponse(
                construct_redirect_uri(
                    str(params.redirect_uri),
                    code=code_value,
                    state=params.state,
                ),
                status_code=302,
            )
        )

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        self._prune()
        code = self._auth_codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        if self._auth_codes.pop(authorization_code.code, None) is None:
            raise TokenError(
                "invalid_grant",
                "authorization code not found or already used",
            )
        return self._issue_tokens(client.client_id or "", authorization_code.scopes)

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        try:
            claims = self._jwt.decode(refresh_token, self._key.public_pem)
        except JoseError:
            return None
        expires_at = claims.get("exp")
        client_id = claims.get("client_id")
        subject = claims.get("sub")
        scopes = str(claims.get("scope", "")).split()
        if (
            claims.get("token_use") != "refresh"
            or claims.get("iss") != self._issuer
            or claims.get("aud") != self._refresh_audience
            or client_id != client.client_id
            or subject != client.client_id
            or not isinstance(expires_at, int)
            or expires_at < time.time()
            or scopes != [JN_READ_SCOPE]
        ):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return self._issue_tokens(
            client.client_id or "",
            scopes or refresh_token.scopes,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        return None

    def _sign(self, payload: dict[str, object]) -> str:
        header = {"alg": "RS256", "typ": "JWT", "kid": self._key.kid}
        return self._jwt.encode(header, payload, self._key.private_pem).decode()

    def _issue_tokens(self, client_id: str, scopes: list[str]) -> OAuthToken:
        if (
            _DEVICE_IDENTIFIER.fullmatch(client_id) is None
            or scopes != [JN_READ_SCOPE]
        ):
            raise TokenError("invalid_scope", "device identity or scope is invalid")
        now = int(time.time())
        common = {
            "iss": self._issuer,
            "sub": client_id,
            "client_id": client_id,
            "scope": JN_READ_SCOPE,
            "iat": now,
        }
        access_token = self._sign(
            {
                **common,
                "aud": self._audience,
                "exp": now + ACCESS_TOKEN_TTL_SECONDS,
                "jti": secrets.token_urlsafe(16),
                "token_use": "access",
            }
        )
        refresh_token = self._sign(
            {
                **common,
                "aud": self._refresh_audience,
                "exp": now + REFRESH_TOKEN_TTL_SECONDS,
                "jti": secrets.token_urlsafe(16),
                "token_use": "refresh",
            }
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token,
            scope=JN_READ_SCOPE,
        )

    async def _metadata_endpoint(self, request: Request) -> Response:
        assert self.base_url is not None
        metadata = build_metadata(
            issuer_url=self.base_url,
            service_documentation_url=None,
            client_registration_options=(
                self.client_registration_options or ClientRegistrationOptions()
            ),
            revocation_options=self.revocation_options or RevocationOptions(),
        )
        document = metadata.model_dump(mode="json", exclude_none=True)
        document["jwks_uri"] = f"{self._issuer}/.well-known/jwks.json"
        return JSONResponse(document)

    async def _jwks_endpoint(self, request: Request) -> Response:
        return JSONResponse({"keys": [self._key.public_jwk()]})

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        for index, route in enumerate(routes):
            if (
                isinstance(route, Route)
                and route.path == "/.well-known/oauth-authorization-server"
            ):
                routes[index] = Route(
                    route.path,
                    endpoint=cors_middleware(
                        self._metadata_endpoint,
                        ["GET", "OPTIONS"],
                    ),
                    methods=["GET", "OPTIONS"],
                )
        routes.extend(
            [
                Route(
                    "/.well-known/jwks.json",
                    endpoint=cors_middleware(
                        self._jwks_endpoint,
                        ["GET", "OPTIONS"],
                    ),
                    methods=["GET", "OPTIONS"],
                ),
                Route(
                    "/enroll",
                    endpoint=self._enroll_endpoint,
                    methods=["GET", "POST"],
                ),
            ]
        )
        return routes


def create_device_provider(settings: Settings) -> DeviceOAuthProvider:
    """Create the provider only after an explicit device-mode configuration."""
    return DeviceOAuthProvider(settings)
