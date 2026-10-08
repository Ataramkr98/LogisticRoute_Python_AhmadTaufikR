import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)


def _fernet():
    raw = settings.FIELD_ENCRYPTION_KEY or settings.SECRET_KEY
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    return Fernet(key)


class DecryptionFailed(RuntimeError):
    """Raised when an encrypted column cannot be read with the current key.

    Surfaced as an explicit error rather than an empty string because the
    alternative is silent, permanent data loss: rotating FIELD_ENCRYPTION_KEY
    (or SECRET_KEY, which is the fallback) makes every existing ciphertext
    undecryptable, and returning "" turned each affected customer email, phone
    number and webhook secret into a blank field that looked like an empty
    record. A loud failure in one request is recoverable; a quiet wipe of a
    column is not.
    """


class EncryptedTextField(models.TextField):
    prefix = "enc::"

    def from_db_value(self, value, expression, connection):
        return self.to_python(value)

    def to_python(self, value):
        if value is None or not isinstance(value, str) or not value.startswith(self.prefix):
            return value
        try:
            return _fernet().decrypt(value[len(self.prefix) :].encode()).decode()
        except InvalidToken:
            logger.error(
                "encrypted_field_decrypt_failed field=%s key_source=%s",
                self.name,
                "FIELD_ENCRYPTION_KEY" if settings.FIELD_ENCRYPTION_KEY else "SECRET_KEY",
            )
            raise DecryptionFailed(
                f"Cannot decrypt {self.name}: the configured encryption key does not match "
                "the stored value. Restore FIELD_ENCRYPTION_KEY or re-encrypt the column."
            ) from None

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "" or value.startswith(self.prefix):
            return value
        return self.prefix + _fernet().encrypt(value.encode()).decode()

