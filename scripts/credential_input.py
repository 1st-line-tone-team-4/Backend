"""Strict file-input normalization; never discover credentials or report their contents."""

import codecs
import re


MAX_INPUT_BYTES = 4096
MAX_KEY_LENGTH = 512


class CredentialInputError(ValueError):
    def __init__(self):
        self.code = "CREDENTIAL_INPUT_INVALID"
        super().__init__(self.code)


def _decode(data):
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif data.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif b"\x00" in data:
        # Only ASCII UTF-16 has this unambiguous byte pattern. Do not guess an
        # encoding from a handful of NULs in arbitrary or damaged input.
        little = len(data) % 2 == 0 and all(b == 0 for b in data[1::2]) and all(data[::2])
        big = len(data) % 2 == 0 and all(b == 0 for b in data[::2]) and all(data[1::2])
        if little == big:
            raise CredentialInputError()
        encoding = "utf-16-le" if little else "utf-16-be"
    else:
        encoding = "utf-8"
    try:
        return data.decode(encoding)
    except UnicodeError:
        pass
    # Outside the exception handler: no decode exception retaining source bytes.
    raise CredentialInputError()


def parse_credential_bytes(data: bytes) -> str:
    """Accept exactly one opaque token, optionally quoted or explicitly labeled.

    Only ordinary ASCII outer whitespace is discarded. Authentication belongs
    to the provider, so this parser deliberately assumes no provider key prefix.
    No diagnostic object contains the token or original input.
    """
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_INPUT_BYTES:
        raise CredentialInputError()
    value = _decode(data).strip(" \t\r\n")
    assignment = re.fullmatch(r"ANTHROPIC_API_KEY[ \t]*[=:][ \t\r\n]*(.*)", value, re.DOTALL)
    if assignment:
        value = assignment[1].strip(" \t\r\n")
    elif re.match(r"[A-Za-z_][A-Za-z0-9_]*[ \t]*[=:]", value):
        # An assignment to another name is not a bare credential.
        raise CredentialInputError()
    if value.startswith(("'", '"')):
        if len(value) < 2 or value[-1] != value[0]:
            raise CredentialInputError()
        value = value[1:-1]
    if (
        not 1 <= len(value) <= MAX_KEY_LENGTH
        or value.startswith(("=", ":"))
        or any(ord(c) < 33 or ord(c) > 126 or c in "\"',;{}[]" for c in value)
        or value == "ANTHROPIC_API_KEY"
        or re.match(r"[A-Za-z_][A-Za-z0-9_]*[ \t]*[=:]", value)
    ):
        raise CredentialInputError()
    return value
