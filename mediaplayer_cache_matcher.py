#!/usr/bin/env python3
"""
mediaplayer_cache_matcher.py

CLI tool to correlate Windows Media Player (Microsoft.ZuneMusic_8wekyb3d8bbwe)
LocalCache\\Image cached images with entries in MediaPlayer.db by generating
expected cache-key filenames from URI/path-like fields in the DB.

Inputs:
  --cache-dir   Folder containing cached images (e.g., D:\\LocalCache\\Image)
  --db          Path to MediaPlayer.db (SQLite)
  --out         Output CSV (default: file_and_hashes.csv in current dir)

What it does:
  1) Enumerates cache image files and normalizes names (removes repeated .jpg/.jpeg)
  2) Reads MediaPlayer.db, auto-discovers URI/path-like columns
  3) For each extracted URI, generates cache keys for:
       - sha1 and sha256
       - hex-dash and signed-decimal formats
       - sizes provided (default: 512x512,256x256,96x96)
       - variant (default: 0)
       - multiple normalizations of URI (raw, lowercase, forward-slash, file:///)
  4) Finds matches where generated cache_key_name == normalized cache filename
  5) Writes a CSV called "file_and_hashes.csv" (or your --out) containing:
       cache_file_name, cache_full_path, cache_key_name,
       source_table, source_column, db_value, normalized_value_used,
       algo, encoding_form, width, height, variant

Notes:
  - This correlates *cache key name* to a DB-derived string. It does not prove playback by itself.
  - Works best on a copy of evidence.
"""

import argparse
import csv
import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple


def strip_image_exts(filename: str) -> str:
    """Remove repeated .jpg/.jpeg extensions: name.jpg.jpg -> name"""
    base = filename
    while True:
        new = re.sub(r"\.(jpg|jpeg)$", "", base, flags=re.IGNORECASE)
        if new == base:
            return base
        base = new


def bytes_to_hex_dash(b: bytes) -> str:
    return "-".join(f"{x:02X}" for x in b)


def bytes_to_signed_decimal_dash(b: bytes) -> str:
    # Interpret each byte as a signed 8-bit value, then join with dashes.
    parts = []
    for x in b:
        parts.append(str(x if x < 128 else x - 256))
    return "-".join(parts)


def normalize_candidates(v: str) -> List[str]:
    """
    Generate likely-normalized variants that UWP/WinRT might hash.
    Keeps order, de-dupes.
    """
    s = (v or "").strip()
    if not s:
        return []

    cands = [s, s.lower()]

    if "\\" in s:
        fwd = s.replace("\\", "/")
        cands.extend([fwd, fwd.lower()])

    if re.match(r"^[A-Za-z]:\\", s):
        file_uri = "file:///" + s.replace("\\", "/")
        cands.extend([file_uri, file_uri.lower()])

    seen: Set[str] = set()
    out: List[str] = []
    for x in cands:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def list_tables(conn: sqlite3.Connection) -> List[str]:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return [r[0] for r in cur.fetchall()]


def list_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def is_uri_like_column(colname: str) -> bool:
    c = colname.lower()
    keywords = ("uri", "path", "source", "location", "url", "file", "filename")
    return any(k in c for k in keywords)


def fetch_distinct_text_values(
    conn: sqlite3.Connection, table: str, column: str, limit: int
) -> List[str]:
    q = f"""
    SELECT DISTINCT CAST({column} AS TEXT)
    FROM {table}
    WHERE {column} IS NOT NULL
      AND TRIM(CAST({column} AS TEXT)) <> ''
    LIMIT ?
    """
    try:
        cur = conn.execute(q, (limit,))
        out = []
        for (v,) in cur.fetchall():
            if v is None:
                continue
            s = str(v).strip()
            if not s:
                continue
            out.append(s)
        return out
    except sqlite3.Error:
        return []


@dataclass
class MatchRow:
    cache_file_name: str
    cache_full_path: str
    cache_key_name: str
    source_table: str
    source_column: str
    db_value: str
    normalized_value_used: str
    algo: str
    encoding_form: str
    variant: int
    width: int
    height: int


def gen_cache_keys_for_value(
    s: str, variant: int, sizes: List[Tuple[int, int]]
) -> Iterable[Tuple[str, str, int, int, str]]:
    """
    Yields tuples: (algo, encoding_form, width, height, cache_key_name)
    """
    b = s.encode("utf-8", errors="replace")

    digests = [
        ("sha1", hashlib.sha1(b).digest()),
        ("sha256", hashlib.sha256(b).digest()),
    ]

    for algo, d in digests:
        prefix_hex = bytes_to_hex_dash(d)
        for w, h in sizes:
            yield (algo, "hex-dash", w, h, f"{prefix_hex}-{variant}-{w}-{h}")

        prefix_dec = bytes_to_signed_decimal_dash(d)
        for w, h in sizes:
            yield (algo, "signed-decimal", w, h, f"{prefix_dec}-{variant}-{w}-{h}")


def parse_sizes(s: str) -> List[Tuple[int, int]]:
    sizes: List[Tuple[int, int]] = []
    for part in (s or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        if "x" not in part:
            raise ValueError(f"Invalid size '{part}'. Use format like 512x512.")
        w_s, h_s = part.split("x", 1)
        sizes.append((int(w_s), int(h_s)))
    if not sizes:
        sizes = [(512, 512), (256, 256), (96, 96)]
    return sizes


def build_cache_index(cache_dir: str) -> Dict[str, List[Tuple[str, str]]]:
    """
    Returns a dict:
      normalized_key -> list of (filename, full_path)
    because you may have duplicates like .jpg and .jpg.jpg.
    """
    idx: Dict[str, List[Tuple[str, str]]] = {}
    for name in os.listdir(cache_dir):
        full = os.path.join(cache_dir, name)
        if not os.path.isfile(full):
            continue
        norm = strip_image_exts(name)
        idx.setdefault(norm, []).append((name, full))
    return idx


def main():
    ap = argparse.ArgumentParser(
        description="Match LocalCache\\Image cached files to MediaPlayer.db by generating cache hash key filenames."
    )
    ap.add_argument("--cache-dir", required=True, help="Path to LocalCache\\Image folder")
    ap.add_argument("--db", required=True, help="Path to MediaPlayer.db (SQLite)")
    ap.add_argument(
        "--out",
        default="file_and_hashes.csv",
        help="Output CSV file (default: file_and_hashes.csv)",
    )
    ap.add_argument("--variant", type=int, default=0, help="Cache variant flag (default: 0)")
    ap.add_argument(
        "--sizes",
        default="512x512,256x256,96x96",
        help="Comma-separated sizes (default: 512x512,256x256,96x96)",
    )
    ap.add_argument(
        "--limit-per-column",
        type=int,
        default=50000,
        help="Max distinct values to read per URI-like column (default: 50000)",
    )
    ap.add_argument(
        "--include-nonimage-ext",
        action="store_true",
        help="Also consider cache files with extensions other than .jpg/.jpeg (default: off).",
    )

    args = ap.parse_args()

    cache_dir = os.path.abspath(args.cache_dir)
    db_path = os.path.abspath(args.db)
    out_path = os.path.abspath(args.out)

    if not os.path.isdir(cache_dir):
        raise SystemExit(f"Cache dir not found: {cache_dir}")
    if not os.path.isfile(db_path):
        raise SystemExit(f"DB not found: {db_path}")

    sizes = parse_sizes(args.sizes)
    variant = args.variant

    # Index the cache directory.
    cache_index = build_cache_index(cache_dir)

    # By default, only keep image-like cache files (.jpg/.jpeg, repeated, or
    # extension-less). Use --include-nonimage-ext to keep everything.
    if not args.include_nonimage_ext:
        filtered: Dict[str, List[Tuple[str, str]]] = {}
        for k, lst in cache_index.items():
            keep = []
            for fname, fpath in lst:
                ext = os.path.splitext(fname)[1].lower()
                if ext in (".jpg", ".jpeg", "") or re.search(
                    r"\.(jpg|jpeg)(\.(jpg|jpeg))+$", fname, re.I
                ):
                    keep.append((fname, fpath))
            if keep:
                filtered[k] = keep
        cache_index = filtered

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    tables = list_tables(conn)

    # Auto-discover URI/path-like columns across all user tables.
    candidate_cols: List[Tuple[str, str]] = []
    for t in tables:
        for c in list_columns(conn, t):
            if is_uri_like_column(c):
                candidate_cols.append((t, c))

    if not candidate_cols:
        conn.close()
        raise SystemExit(
            "No URI-like columns found. You may need to adjust the heuristic or query manually."
        )

    matches: List[MatchRow] = []
    seen_match_keys: Set[Tuple[str, str, str, str, str]] = set()

    # For every candidate value, generate cache keys and look for hits.
    for t, c in candidate_cols:
        values = fetch_distinct_text_values(conn, t, c, args.limit_per_column)
        for v in values:
            for nv in normalize_candidates(v):
                for algo, enc, w, h, keyname in gen_cache_keys_for_value(nv, variant, sizes):
                    if keyname in cache_index:
                        for fname, fpath in cache_index[keyname]:
                            dedup = (keyname, fpath, t, c, nv)
                            if dedup in seen_match_keys:
                                continue
                            seen_match_keys.add(dedup)
                            matches.append(
                                MatchRow(
                                    cache_file_name=fname,
                                    cache_full_path=fpath,
                                    cache_key_name=keyname,
                                    source_table=t,
                                    source_column=c,
                                    db_value=v,
                                    normalized_value_used=nv,
                                    algo=algo,
                                    encoding_form=enc,
                                    variant=variant,
                                    width=w,
                                    height=h,
                                )
                            )

    conn.close()

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "cache_file_name",
                "cache_full_path",
                "cache_key_name",
                "source_table",
                "source_column",
                "db_value",
                "normalized_value_used",
                "algo",
                "encoding_form",
                "variant",
                "width",
                "height",
            ]
        )
        for m in matches:
            writer.writerow(
                [
                    m.cache_file_name,
                    m.cache_full_path,
                    m.cache_key_name,
                    m.source_table,
                    m.source_column,
                    m.db_value,
                    m.normalized_value_used,
                    m.algo,
                    m.encoding_form,
                    m.variant,
                    m.width,
                    m.height,
                ]
            )

    print(f"Cache files indexed: {sum(len(v) for v in cache_index.values())}")
    print(f"Candidate DB columns scanned: {len(candidate_cols)}")
    print(f"Matches found: {len(matches)}")
    print(f"Output CSV: {out_path}")


if __name__ == "__main__":
    main()
