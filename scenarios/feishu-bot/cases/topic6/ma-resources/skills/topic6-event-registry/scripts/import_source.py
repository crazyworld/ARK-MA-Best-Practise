"""Validate CSV input and retain row_id, source columns/order and explicit heat mapping."""
import argparse
import csv
import math
from pathlib import Path
from c2_runtime import atomic_json, file_hash, unique_ids
from relay import write_csv


def convert(source, output, heat_col, input_level):
    if Path(source).resolve() == Path(output).resolve():
        raise ValueError("Source and output must be different files")
    if Path(output).exists():
        raise ValueError("Refusing to overwrite an existing input snapshot")
    with Path(source).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        rows = list(reader)
    if len(fields) != len(set(fields)) or None in fields:
        raise ValueError("Duplicate or invalid headers")
    if not rows or not {"platform", "title", heat_col} <= set(fields):
        raise ValueError("Missing rows or required title/platform/explicit heat column")
    if not {"row_id", "record_id"} & set(fields):
        raise ValueError("Missing row_id/record_id")
    for row in rows:
        if None in row or any(row[k] is None for k in fields):
            raise ValueError("Malformed CSV row width")
        if "row_id" in row and "record_id" in row and row["row_id"] != row["record_id"]:
            raise ValueError("row_id and record_id conflict")
        if "record_id" not in row:
            row["record_id"] = row["row_id"]
        if not row["title"].strip() or not row["platform"].strip():
            raise ValueError("Blank title/platform")
        if row[heat_col] and not math.isfinite(float(row[heat_col])):
            raise ValueError("Nonfinite heat")
    unique_ids(rows)
    if "record_id" not in fields:
        fields.append("record_id")
    write_csv(Path(output), rows, fields)
    manifest = {"schema": "c2-input/1", "source_sha256": file_hash(source),
                "input_sha256": file_hash(output), "records": len(rows), "columns": fields,
                "heat_col": heat_col, "heat_unit": "source-defined; not inferred",
                "input_level": input_level, "selection": "none; all rows preserved"}
    atomic_json(Path(output).with_suffix(".manifest.json"), manifest)
    return manifest


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--heat-col", required=True)
    ap.add_argument("--input-level", required=True, choices=["RAW_IMPORT_ONLY", "C0_USABLE", "SYNTHETIC"])
    args = ap.parse_args()
    print(convert(args.source, args.out, args.heat_col, args.input_level))
