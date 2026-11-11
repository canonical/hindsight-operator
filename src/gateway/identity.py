# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Who a member is: verify their token and read its email (002 FR-001, FR-002).

The issuer's signing keys come from its OpenID Connect discovery document and
are cached. A token whose key ID the cache does not hold refetches them, at
most once a minute, so a rotated key is picked up without letting a stream of
made-up key IDs reach the issuer.

This module runs inside the Hindsight image (Python 3.11), on its PyJWT.
"""

from __future__ import annotations

import json
import time

import httpx
import jwt

# Only asymmetric algorithms: a symmetric one would let anyone holding the
# public key sign tokens.
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
KEYS_TTL = 600.0
REFETCH_INTERVAL = 60.0
LEEWAY = 30


class IdentityError(Exception):
    """The token cannot be accepted. ``status`` is 401, or 503 if the issuer is unreachable."""

    def __init__(self, reason: str, status: int = 401):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def looks_like_token(value: str) -> bool:
    """Whether a bearer value has a JSON Web Token's three dot-separated parts."""
    parts = value.split(".")
    return len(parts) == 3 and all(parts)


class MemberVerifier:
    def __init__(self, issuer: str, audience: str, http: httpx.AsyncClient, clock=time.monotonic):
        # Compared with the token's `iss` as configured: some issuers end it with a slash.
        self.issuer = issuer
        self.audience = audience
        self._http = http
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None

    async def email(self, token: str) -> str:
        """The lowercased email the token names, or raise :class:`IdentityError`."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise IdentityError("the token is not a JSON Web Token") from exc
        if header.get("alg") not in ALGORITHMS:
            raise IdentityError("the token's algorithm is not accepted")
        key = await self._key(header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise IdentityError(f"the token is not valid: {exc}") from exc
        email = claims.get("email")
        if not isinstance(email, str) or "@" not in email:
            raise IdentityError("the token names no email")
        if claims.get("email_verified") is False:
            raise IdentityError("the token's email is not verified")
        return email.strip().lower()

    async def _key(self, kid: str | None) -> jwt.PyJWK:
        now = self._clock()
        stale = self._fetched_at is None or now - self._fetched_at >= KEYS_TTL
        missing = kid not in self._keys if kid else not self._keys
        may_refetch = self._fetched_at is None or now - self._fetched_at >= REFETCH_INTERVAL
        if stale or (missing and may_refetch):
            await self._fetch()
        if kid:
            if kid not in self._keys:
                raise IdentityError("the token is signed with a key the issuer does not publish")
            return self._keys[kid]
        if len(self._keys) != 1:
            raise IdentityError("the token names no key and the issuer publishes several")
        return next(iter(self._keys.values()))

    async def _fetch(self) -> None:
        try:
            discovery = await self._get_json(
                f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
            )
            jwks = await self._get_json(discovery["jwks_uri"])
            keys = {}
            for index, data in enumerate(jwks.get("keys", [])):
                if data.get("use", "sig") != "sig":
                    continue
                try:
                    keys[data.get("kid") or f"#{index}"] = jwt.PyJWK(data)
                except jwt.PyJWTError:
                    continue  # a key type PyJWT cannot use, such as one for encryption
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            if self._fetched_at is not None and self._keys:
                # Keep the keys we have, and try again at the next refetch.
                self._fetched_at = self._clock()
                return
            raise IdentityError("the identity provider's keys cannot be fetched", 503) from exc
        self._keys = keys
        self._fetched_at = self._clock()

    async def _get_json(self, url: str) -> dict:
        response = await self._http.get(url)
        response.raise_for_status()
        data = json.loads(response.content)
        if not isinstance(data, dict):
            raise ValueError(f"{url} is not a JSON object")
        return data
