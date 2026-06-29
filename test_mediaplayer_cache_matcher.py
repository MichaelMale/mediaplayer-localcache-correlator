"""
Tests for mediaplayer_cache_matcher.py

These exercise the cache-key generation, URI normalisation, DB column
discovery, cache indexing, and a full end-to-end correlation run.
"""

import csv
import hashlib
import os
import sqlite3
import subprocess
import sys

import pytest

import mediaplayer_cache_matcher as m

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "mediaplayer_cache_matcher.py")


# --------------------------------------------------------------------------
# strip_image_exts
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,expected",
    [
        ("abc.jpg", "abc"),
        ("abc.jpg.jpg", "abc"),
        ("abc.jpeg.jpg.jpeg", "abc"),
        ("ABC.JPG", "ABC"),            # case-insensitive
        ("abc", "abc"),                # no extension
        ("abc.png", "abc.png"),        # non-image extension untouched
        ("name.jpg.png", "name.jpg.png"),  # trailing ext isn't jpg/jpeg
    ],
)
def test_strip_image_exts(name, expected):
    assert m.strip_image_exts(name) == expected


# --------------------------------------------------------------------------
# bytes_to_hex_dash / bytes_to_signed_decimal_dash
# --------------------------------------------------------------------------
def test_bytes_to_hex_dash():
    assert m.bytes_to_hex_dash(b"") == ""
    assert m.bytes_to_hex_dash(bytes([0, 15, 255, 171])) == "00-0F-FF-AB"


def test_bytes_to_signed_decimal_dash():
    # 0 -> 0, 127 -> 127, 128 -> -128, 255 -> -1
    assert m.bytes_to_signed_decimal_dash(bytes([0, 127, 128, 255])) == "0-127--128--1"
    assert m.bytes_to_signed_decimal_dash(b"") == ""


# --------------------------------------------------------------------------
# normalize_candidates
# --------------------------------------------------------------------------
def test_normalize_candidates_empty():
    assert m.normalize_candidates("") == []
    assert m.normalize_candidates("   ") == []
    assert m.normalize_candidates(None) == []


def test_normalize_candidates_simple_uri():
    # No backslash, no drive letter -> just raw + lowercase, de-duped
    out = m.normalize_candidates("file:///C:/Music/song.mp3")
    assert out == ["file:///C:/Music/song.mp3", "file:///c:/music/song.mp3"]


def test_normalize_candidates_windows_path():
    out = m.normalize_candidates(r"C:\Music\song.mp3")
    # raw, lower, forward-slash, forward-slash-lower, file:/// + fwd, file lower
    assert out == [
        r"C:\Music\song.mp3",
        r"c:\music\song.mp3",
        "C:/Music/song.mp3",
        "c:/music/song.mp3",
        "file:///C:/Music/song.mp3",
        "file:///c:/music/song.mp3",
    ]


def test_normalize_candidates_dedupes_order_preserved():
    # Already-lowercase input must not produce duplicate entries
    out = m.normalize_candidates("abc")
    assert out == ["abc"]


# --------------------------------------------------------------------------
# is_uri_like_column
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "col,expected",
    [
        ("Uri", True),
        ("FilePath", True),
        ("source", True),
        ("Location", True),
        ("Url", True),
        ("FileName", True),
        ("file", True),
        ("Title", False),
        ("Id", False),
        ("duration", False),
    ],
)
def test_is_uri_like_column(col, expected):
    assert m.is_uri_like_column(col) is expected


# --------------------------------------------------------------------------
# parse_sizes
# --------------------------------------------------------------------------
def test_parse_sizes_default_when_empty():
    assert m.parse_sizes("") == [(512, 512), (256, 256), (96, 96)]
    assert m.parse_sizes(None) == [(512, 512), (256, 256), (96, 96)]


def test_parse_sizes_explicit():
    assert m.parse_sizes("640x480, 96X96") == [(640, 480), (96, 96)]


def test_parse_sizes_invalid():
    with pytest.raises(ValueError):
        m.parse_sizes("512")


# --------------------------------------------------------------------------
# gen_cache_keys_for_value
# --------------------------------------------------------------------------
def test_gen_cache_keys_for_value_known_vector():
    s = "file:///C:/Users/test/Music/song.mp3"
    b = s.encode("utf-8", errors="replace")
    sha256_hex = "-".join(f"{x:02X}" for x in hashlib.sha256(b).digest())
    expected_key = f"{sha256_hex}-0-256-256"

    keys = list(m.gen_cache_keys_for_value(s, variant=0, sizes=[(256, 256)]))
    # Two algos x one encoding-pair x one size = sha1 hex, sha1 dec, sha256 hex, sha256 dec
    assert len(keys) == 4

    by_form = {(algo, enc): name for algo, enc, w, h, name in keys}
    assert by_form[("sha256", "hex-dash")] == expected_key

    # Every yielded key ends with the -variant-w-h suffix
    for algo, enc, w, h, name in keys:
        assert name.endswith("-0-256-256")
        assert (algo, enc, w, h) in {
            ("sha1", "hex-dash", 256, 256),
            ("sha1", "signed-decimal", 256, 256),
            ("sha256", "hex-dash", 256, 256),
            ("sha256", "signed-decimal", 256, 256),
        }


def test_gen_cache_keys_respects_variant_and_sizes():
    keys = list(m.gen_cache_keys_for_value("x", variant=3, sizes=[(96, 96), (512, 512)]))
    # 2 algos * 2 encodings * 2 sizes = 8
    assert len(keys) == 8
    assert all(name.endswith("-3-96-96") or name.endswith("-3-512-512") for *_, name in keys)


# --------------------------------------------------------------------------
# build_cache_index
# --------------------------------------------------------------------------
def test_build_cache_index_groups_duplicates(tmp_path):
    img = tmp_path / "Image"
    img.mkdir()
    (img / "abc").write_bytes(b"1")
    (img / "abc.jpg").write_bytes(b"2")
    (img / "abc.jpg.jpg").write_bytes(b"3")
    (img / "other.jpg").write_bytes(b"4")
    (img / "subdir").mkdir()  # directories must be ignored

    idx = m.build_cache_index(str(img))
    assert set(idx.keys()) == {"abc", "other"}
    assert len(idx["abc"]) == 3
    assert len(idx["other"]) == 1


# --------------------------------------------------------------------------
# End-to-end via the CLI
# --------------------------------------------------------------------------
def _make_evidence(tmp_path, uri="file:///C:/Users/test/Music/song.mp3"):
    cache = tmp_path / "Image"
    cache.mkdir()
    db = tmp_path / "MediaPlayer.db"

    b = uri.encode("utf-8", errors="replace")
    sha256_hex = "-".join(f"{x:02X}" for x in hashlib.sha256(b).digest())
    sha1_dec = "-".join(
        str(x if x < 128 else x - 256) for x in hashlib.sha1(b).digest()
    )

    # sha256 hex-dash thumbnail, stored with a .jpg extension
    (cache / f"{sha256_hex}-0-256-256.jpg").write_bytes(b"\xff\xd8\xff jpeg")
    # sha1 signed-decimal thumbnail, stored extension-less
    (cache / f"{sha1_dec}-0-512-512").write_bytes(b"raw")
    # a decoy that must never match
    (cache / "decoy.jpg").write_bytes(b"nope")

    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE Video (Id INTEGER, Uri TEXT, Title TEXT)")
    conn.execute("INSERT INTO Video VALUES (1, ?, 'Song')", (uri,))
    conn.execute(
        "INSERT INTO Video VALUES (2, 'file:///C:/Users/test/Music/uncached.mp3', 'No cache')"
    )
    conn.commit()
    conn.close()
    return cache, db


def _run(cache, db, out, *extra):
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--cache-dir", str(cache), "--db", str(db), "--out", str(out), *extra],
        capture_output=True,
        text=True,
    )
    return proc


def test_end_to_end_matches(tmp_path):
    cache, db = _make_evidence(tmp_path)
    out = tmp_path / "file_and_hashes.csv"
    proc = _run(cache, db, out)
    assert proc.returncode == 0, proc.stderr

    with open(out, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2
    forms = {(r["algo"], r["encoding_form"], r["width"], r["height"]) for r in rows}
    assert ("sha256", "hex-dash", "256", "256") in forms
    assert ("sha1", "signed-decimal", "512", "512") in forms
    # The decoy must not appear, and only the cached URI should be referenced
    assert all(r["source_table"] == "Video" and r["source_column"] == "Uri" for r in rows)
    assert all("uncached" not in r["db_value"] for r in rows)


def test_end_to_end_missing_cache_dir_errors(tmp_path):
    _, db = _make_evidence(tmp_path)
    out = tmp_path / "out.csv"
    proc = _run(tmp_path / "does_not_exist", db, out)
    assert proc.returncode != 0
    assert "Cache dir not found" in proc.stderr


def test_end_to_end_no_uri_columns_errors(tmp_path):
    cache = tmp_path / "Image"
    cache.mkdir()
    db = tmp_path / "MediaPlayer.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE T (Id INTEGER, Title TEXT)")  # no URI-like columns
    conn.commit()
    conn.close()
    out = tmp_path / "out.csv"
    proc = _run(cache, db, out)
    assert proc.returncode != 0
    assert "No URI-like columns found" in proc.stderr
