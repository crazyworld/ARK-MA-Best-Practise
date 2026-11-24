"""Lossless Excel text representation, with ordered ASCII JSON overflow chunks."""
import hashlib
import json

TABLE_CONTRACT = 'v0.11.1-lossless-xlsx-json-chunks'
LEGACY_TABLE_CONTRACT = 'v0.9.4-cell-roundtrip-json-containers'
SHEET = 'text_overflow'
HEADERS = ['data_row', 'column', 'part', 'total', 'sha256', 'json_chunk']
PREFIX = '[SSJH_TEXT:'


def unsafe(value):
    # The stdlib XML writer emits literal CR, which XML readers normalize to LF.
    # Preserve CR/CRLF through the existing lossless channel on every backend.
    return isinstance(value, str) and (value.startswith(PREFIX) or
        '\r' in value or
        sum(2 if ord(c) > 0xffff else 1 for c in value) > 32767 or
        any(ord(c) < 32 and c not in '\t\n\r' or 0xd800 <= ord(c) <= 0xdfff or
            ord(c) in (0xfffe, 0xffff) for c in value))


def encode(value, row, column):
    if not unsafe(value):
        return value, []
    encoded = json.dumps(value, ensure_ascii=True)
    digest = hashlib.sha256(encoded.encode('ascii')).hexdigest()
    chunks = [encoded[i:i+30000] for i in range(0, len(encoded), 30000)]
    records = [[row, column, i+1, len(chunks), digest, chunk] for i, chunk in enumerate(chunks)]
    return f'{PREFIX}{row}:{column}] 完整内容见 text_overflow；按 part 拼接 json_chunk 后 JSON 解码。', records


def decode(records):
    """Validate sequence and hash before reconstructing one exceptional cell."""
    from errors import SkillError
    if not records:
        raise SkillError('EXPORT_OVERFLOW_MISMATCH')
    row, column, _, total, digest, _ = records[0]
    if len(records) != total or any(r[:5] != [row, column, i+1, total, digest] for i, r in enumerate(records)):
        raise SkillError('EXPORT_OVERFLOW_MISMATCH')
    encoded = ''.join(r[5] for r in records)
    if hashlib.sha256(encoded.encode('ascii')).hexdigest() != digest:
        raise SkillError('EXPORT_OVERFLOW_MISMATCH')
    return json.loads(encoded)
