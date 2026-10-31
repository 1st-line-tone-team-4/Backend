import codecs
import traceback

import pytest

from scripts.credential_input import CredentialInputError, parse_credential_bytes


TOKEN = "synthetic-opaque-token_123456"


@pytest.mark.parametrize(
    "encode",
    [
        lambda text: text.encode("utf-8"),
        lambda text: codecs.BOM_UTF8 + text.encode("utf-8"),
        lambda text: codecs.BOM_UTF16_LE + text.encode("utf-16-le"),
        lambda text: codecs.BOM_UTF16_BE + text.encode("utf-16-be"),
        lambda text: text.encode("utf-16-le"),
        lambda text: text.encode("utf-16-be"),
    ],
)
@pytest.mark.parametrize(
    "text",
    [
        TOKEN,
        " \t\r\n" + TOKEN + "\n ",
        '"' + TOKEN + '"',
        "'" + TOKEN + "'",
        "ANTHROPIC_API_KEY=" + TOKEN,
        'ANTHROPIC_API_KEY = "' + TOKEN + '"\r\n',
        "ANTHROPIC_API_KEY:\r\n  '" + TOKEN + "'\n",
    ],
)
def test_single_credential_formats(encode, text):
    assert parse_credential_bytes(encode(text)) == TOKEN


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b" \r\n\t",
        b"x" * 513,
        b"x" * 4097,
        b"\xffsynthetic",
        codecs.BOM_UTF16_LE + b"x",
        b"x\x00y",
        b"\x00\x00",
        b"a\x00\x00b",
        b"\x00" + TOKEN.encode(),
        b"\xef\xbb\xbf\xef\xbb\xbf" + TOKEN.encode(),
        (TOKEN + "\x00").encode(),
        (TOKEN + "\x7f").encode(),
        (TOKEN + "\x1b").encode(),
        (TOKEN + "\v").encode(),
        ("\u00a0" + TOKEN).encode(),
        (TOKEN + "\u200b").encode(),
        b'"synthetic key"',
        b"synthetic\nkey",
        b"synthetic\tkey",
        ('"' + TOKEN + "'").encode(),
        ('"' + TOKEN).encode(),
        (TOKEN + '"').encode(),
        ('""' + TOKEN + '""').encode(),
        ("' " + TOKEN + " '").encode(),
        b"ANTHROPIC_API_KEY",
        b"ANTHROPIC_API_KEY=",
        b"ANTHROPIC_API_KEY:",
        ("OTHER_KEY=" + TOKEN).encode(),
        ("anthropic_api_key=" + TOKEN).encode(),
        ("export ANTHROPIC_API_KEY=" + TOKEN).encode(),
        ("ANTHROPIC_API_KEY " + TOKEN).encode(),
        ("ANTHROPIC_API_KEY==" + TOKEN).encode(),
        ("ANTHROPIC_API_KEY=" + TOKEN + "\nANTHROPIC_API_KEY=" + TOKEN).encode(),
        ("ANTHROPIC_API_KEY=" + TOKEN + ";ANTHROPIC_API_KEY=" + TOKEN).encode(),
        ("ANTHROPIC_API_KEY=" + TOKEN + " # comment").encode(),
        (TOKEN + " " + TOKEN).encode(),
        (TOKEN + "\n" + TOKEN).encode(),
        (TOKEN + "," + TOKEN).encode(),
        (TOKEN + ";" + TOKEN).encode(),
        ('{"ANTHROPIC_API_KEY":"' + TOKEN + '"}').encode(),
    ],
)
def test_invalid_or_ambiguous_input_is_redacted(data):
    with pytest.raises(CredentialInputError) as captured:
        parse_credential_bytes(data)
    error = captured.value
    assert str(error) == "CREDENTIAL_INPUT_INVALID"
    assert repr(error) == "CredentialInputError('CREDENTIAL_INPUT_INVALID')"
    assert vars(error) == {"code": "CREDENTIAL_INPUT_INVALID"}
    assert error.__cause__ is None and error.__context__ is None
    assert TOKEN not in "".join(traceback.format_exception(error))


def test_printable_opaque_value_does_not_require_a_provider_prefix():
    assert parse_credential_bytes(b"synthetic.opaque+token/123") == "synthetic.opaque+token/123"
