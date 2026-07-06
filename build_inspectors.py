#!/usr/bin/env python3
"""
Convert the coworker-provided "Inspector List.xls" into a clean inspectors.json
that the app loads at runtime (so production doesn't need to read .xls).

Run after the source file changes:
    python3 -m pip install xlrd
    python3 build_inspectors.py

It reads data/Inspector List.xls and writes inspectors.json.
"""

from __future__ import annotations

import json
import re

import xlrd

SRC = "data/Inspector List.xls"
OUT = "inspectors.json"


def normalize(name: str) -> str:
    """Normalization key for matching municipality names across sources."""
    n = name.strip().lower()
    n = n.replace(".", "")
    n = re.sub(r"\s+", " ", n)
    # common abbreviation expansions so "Mt Horeb" == "Mount Horeb", etc.
    n = re.sub(r"^mt ", "mount ", n)
    n = re.sub(r"^st ", "saint ", n)
    n = n.replace("mc farland", "mcfarland").replace("de forest", "deforest")
    return n


def parse_label(raw: str) -> tuple[str, str]:
    """Return (base_name, muni_type) from a label like 'City of Madison'."""
    label = re.sub(r"\s+", " ", raw.strip())
    low = label.lower()
    # fix a known typo in the source ("Ctiy of Waterloo")
    low = low.replace("ctiy of", "city of")
    label = re.sub(r"(?i)ctiy of", "City of", label)

    if low.startswith("city/village of "):
        return label[len("city/village of "):].strip(), "any"
    if low.startswith("city of "):
        return label[len("city of "):].strip(), "city"
    if low.startswith("village of "):
        return label[len("village of "):].strip(), "village"
    if low.startswith("town of "):
        return label[len("town of "):].strip(), "town"
    if low.startswith("township of "):
        return label[len("township of "):].strip(), "town"
    if low.endswith(" township"):
        return label[: -len(" township")].strip(), "town"
    # bare name (e.g. "Brooklyn") -> matches any type
    return label, "any"


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip())


def main() -> None:
    book = xlrd.open_workbook(SRC)
    sheet = book.sheet_by_index(0)

    records: dict[str, list[dict]] = {}
    count = 0
    for r in range(1, sheet.nrows):  # skip header row 0
        cells = [clean(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
        label = cells[0] if cells else ""
        if not label:
            continue  # note-only rows have no municipality
        base, mtype = parse_label(label)
        if not base:
            continue
        name = cells[1] if len(cells) > 1 else ""
        phone = cells[2] if len(cells) > 2 else ""
        cell = cells[3] if len(cells) > 3 else ""
        notes = " ".join(x for x in cells[4:] if x).strip()

        # Combine the two phone columns; some rows use one, some the other.
        phones = [p for p in (phone, cell) if p]

        entry = {
            "label": label,
            "type": mtype,
            "inspector": name,
            "phones": phones,
            "notes": notes,
        }
        key = f"{normalize(base)}|{mtype}"
        records.setdefault(key, []).append(entry)
        count += 1

    with open(OUT, "w") as fh:
        json.dump(records, fh, indent=2, ensure_ascii=False)
    print(f"Parsed {count} inspector rows -> {len(records)} keys. Wrote {OUT}.")


if __name__ == "__main__":
    main()
