# Methodology & Resources

This document records how `mediaplayer_cache_matcher.py` was produced and the
resources used, so the result is auditable.

## Summary

The repository shipped with `README.md` (describing the tool) and
`mediaplayer_LocalCache_file.zip`. The zip did **not** contain Python source —
it contained a single Windows executable, `mediaplayer_LocalCache_file.exe`
(PE32+, x86-64 console app).

That executable turned out to be a **PyInstaller bundle of the exact script the
README describes**. Rather than guess at the cache-key algorithm, the script in
this repo was recovered directly from the bundled program's compiled bytecode,
so it is byte-for-byte equivalent to the original (see *Verification* below).

## How the executable was analysed

1. **Identify the file.** `file` reported a `PE32+ executable (console)`.
   `strings` on it revealed `python313.dll`, `_MEIPASS`, `PYZ`, and
   `Could not load PyInstaller's embedded PKG archive…` — i.e. a
   **PyInstaller** bundle built with **CPython 3.13**.

2. **Unpack the PyInstaller archive.** Used
   [`pyinstxtractor`](https://github.com/extremecoders-re/pyinstxtractor)
   (PyInstaller Extractor) to extract the embedded files. The reported entry
   point was `mediaplayer_cache_hashes.pyc`. (The internal module is named
   `mediaplayer_cache_hashes.py`; its own docstring and the README both call the
   tool `mediaplayer_cache_matcher.py`, which is the filename used here.)

3. **Recover the logic from bytecode.** The local default Python is 3.11, which
   cannot `marshal.loads` a 3.13 `.pyc`. CPython **3.13** is available on the
   box (`/usr/bin/python3.13`), so the `.pyc` was loaded and disassembled with
   the standard-library [`marshal`](https://docs.python.org/3/library/marshal.html)
   and [`dis`](https://docs.python.org/3/library/dis.html) modules. Every
   function's disassembly, constants, and type annotations were read off
   directly and re-expressed as readable Python.

No third-party decompiler was needed; the reconstruction was done by reading the
CPython 3.13 bytecode instruction-by-instruction.

## Verification

To confirm the reconstruction is faithful (not just plausible), the source in
this repo was recompiled with CPython 3.13 and its bytecode diffed against the
original `.pyc`, function by function:

- **All 16 code objects** (every function, generator expression, and the
  `MatchRow` dataclass body) are **byte-for-byte identical** in their
  instruction streams.
- The **only** remaining difference is the value of `MatchRow.__firstlineno__`
  (the dataclass's source line number, 151 in the original vs. its line number
  here). That is metadata emitted by the compiler from line positions; it has no
  effect on behaviour.

A functional self-test was also run: a synthetic `MediaPlayer.db` (with a `Uri`
column) plus a cache directory whose filenames were the expected
`SHA256/hex-dash` and `SHA1/signed-decimal` cache keys. The tool correctly
matched both and ignored a decoy file.

## What the cache-key algorithm actually is (recovered)

For a candidate string `s` (a URI/path value from the DB, after normalisation):

```
b      = s.encode("utf-8", errors="replace")
digest = sha1(b).digest()   and   sha256(b).digest()

hex-dash       form: "-".join(f"{byte:02X}" for byte in digest)      # uppercase hex per byte
signed-decimal form: "-".join(str(byte if byte < 128 else byte-256)) # each byte as signed int8

cache_key_name = f"{prefix}-{variant}-{width}-{height}"
```

Both hash algorithms (`sha1`, `sha256`), both encodings (`hex-dash`,
`signed-decimal`), each requested size (default `512x512,256x256,96x96`), and a
`variant` (default `0`) are generated. Candidate strings are also expanded into
several normalised forms — raw, lowercase, backslash→forward-slash, and a
`file:///`-prefixed form for drive-letter paths — because UWP/WinRT may hash any
of these. A generated key counts as a match when it equals a cache filename
(after stripping repeated `.jpg`/`.jpeg` extensions).

> Note: this is the algorithm as **implemented by the bundled tool**. It is a set
> of well-chosen heuristic candidates for how the Zune/Media Player UWP app keys
> its `LocalCache\Image` thumbnails; matches correlate a cache filename to a
> DB-derived string but, as the README cautions, do not by themselves prove
> playback.

## Tools & references used

- `file`, `strings`, `unzip` — initial triage of the archive/executable.
- [pyinstxtractor](https://github.com/extremecoders-re/pyinstxtractor) — unpacking
  the PyInstaller bundle.
- CPython 3.13 standard library
  [`marshal`](https://docs.python.org/3/library/marshal.html) and
  [`dis`](https://docs.python.org/3/library/dis.html) — loading and
  disassembling the recovered `.pyc`.
- Python standard library only at runtime (`argparse`, `csv`, `hashlib`, `os`,
  `re`, `sqlite3`, `dataclasses`, `typing`) — the script has **no third-party
  dependencies**.
