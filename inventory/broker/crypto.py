"""KMS envelope encryption for values stored in Redis."""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from botocore.exceptions import BotoCoreError, ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from inventory.aws import AwsClient
from inventory.broker import BrokerError

ENVELOPE_VERSION = 1
DECRYPTED_KEY_CACHE_SIZE = 8


@dataclass(frozen=True, slots=True)
class _DataKey:
    plaintext: bytes = field(repr=False)
    encrypted: bytes
    created: float


class EnvelopeCipher:
    def __init__(
        self,
        kms_client: AwsClient,
        key_id: str,
        data_key_ttl_seconds: int = 3600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._kms = kms_client
        self._key_id = key_id
        self._ttl = data_key_ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._current: dict[str, _DataKey] = {}
        self._decrypted: OrderedDict[bytes, bytes] = OrderedDict()

    def encrypt(self, plaintext: bytes, kind: str) -> str:
        key = self._data_key(kind)
        nonce = os.urandom(12)
        ciphertext = AESGCM(key.plaintext).encrypt(nonce, plaintext, kind.encode())
        return json.dumps(
            {
                "v": ENVELOPE_VERSION,
                "ek": _b64(key.encrypted),
                "iv": _b64(nonce),
                "ct": _b64(ciphertext),
            }
        )

    def decrypt(self, envelope: str, kind: str) -> bytes:
        try:
            payload = json.loads(envelope)
            encrypted_key = base64.b64decode(payload["ek"])
            nonce = base64.b64decode(payload["iv"])
            ciphertext = base64.b64decode(payload["ct"])
        except (ValueError, KeyError, TypeError) as exc:
            raise BrokerError("INTERNAL", "malformed encrypted cache entry") from exc
        key = self._plaintext_key(encrypted_key, kind)
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, kind.encode())
        except InvalidTag as exc:
            raise BrokerError("INTERNAL", "cache entry failed authentication") from exc

    def _data_key(self, kind: str) -> _DataKey:
        with self._lock:
            current = self._current.get(kind)
            if current and self._clock() - current.created < self._ttl:
                return current
        try:
            response = self._kms.generate_data_key(
                KeyId=self._key_id, KeySpec="AES_256", EncryptionContext=_context(kind)
            )
        except (ClientError, BotoCoreError) as exc:
            raise BrokerError("INTERNAL", "KMS GenerateDataKey failed") from exc
        key = _DataKey(response["Plaintext"], response["CiphertextBlob"], self._clock())
        with self._lock:
            self._current[kind] = key
            self._remember(key.encrypted, key.plaintext)
        return key

    def _plaintext_key(self, encrypted_key: bytes, kind: str) -> bytes:
        with self._lock:
            cached = self._decrypted.get(encrypted_key)
            if cached is not None:
                self._decrypted.move_to_end(encrypted_key)
                return cached
        try:
            response = self._kms.decrypt(
                CiphertextBlob=encrypted_key, EncryptionContext=_context(kind)
            )
        except (ClientError, BotoCoreError) as exc:
            raise BrokerError("INTERNAL", "KMS Decrypt failed") from exc
        plaintext: bytes = response["Plaintext"]
        with self._lock:
            self._remember(encrypted_key, plaintext)
        return plaintext

    def _remember(self, encrypted: bytes, plaintext: bytes) -> None:
        self._decrypted[encrypted] = plaintext
        self._decrypted.move_to_end(encrypted)
        while len(self._decrypted) > DECRYPTED_KEY_CACHE_SIZE:
            self._decrypted.popitem(last=False)


def _context(kind: str) -> dict[str, str]:
    return {"app": "inventory-broker", "kind": kind}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
