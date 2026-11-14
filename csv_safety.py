"""Versioned text escaping for spreadsheet-facing queue CSV files."""
import re


ENCODING_COLUMN = '__DHB_CSV_Encoding'
ENCODING_VERSION = 'apostrophe-v1'
_FORMULA_PREFIX = re.compile(r'^[\s\x00-\x1f\ufeff]*[=+@-]')


def _needs_escape(value: str) -> bool:
    # Escape original apostrophes as well so decoding never loses user text.
    return value.startswith(("'", '\t', '\r', '\n')) or bool(_FORMULA_PREFIX.match(value))


def encode_csv_cell(value):
    if isinstance(value, str) and _needs_escape(value):
        return "'" + value
    return value


def decode_csv_cell(value: str, encoding: str) -> str:
    # An ordinary external CSV has no marker: its apostrophes are literal.
    if encoding == ENCODING_VERSION and value.startswith("'") and _needs_escape(value[1:]):
        return value[1:]
    return value
