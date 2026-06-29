# MediaPlayer LocalCache Cache Key Correlator

A forensic utility for correlating Windows Media Player (UWP) cached thumbnails located in:

C:\Users\<User>\AppData\Local\Packages\Microsoft.ZuneMusic_8wekyb3d8bbwe\LocalCache\Image

with URI values stored in:

MediaPlayer.db

## Purpose

This tool reconstructs cache filenames using:

SHA256(URI_string_UTF8)

It allows deterministic correlation between:

- MediaPlayer.db entries (Video.Uri / File.Uri)
- Cached thumbnail artefacts
- Original media file paths

## Features

- Auto-discovers URI/path-like columns in MediaPlayer.db
- Supports SHA1 and SHA256
- Generates dash-hex and signed-decimal formats
- Supports multiple thumbnail sizes
- Outputs structured CSV report

## Usage
python mediaplayer_cache_matcher.py --cache-dir "C:\Users\username\AppData\Local\Packages\Microsoft.ZuneMusic_8wekyb3d8bbwe\LocalCache\Image" --db "MediaPlayer.db path"

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--cache-dir` | *(required)* | Path to the `LocalCache\Image` folder. |
| `--db` | *(required)* | Path to `MediaPlayer.db` (SQLite). |
| `--out` | `file_and_hashes.csv` | Output CSV file. |
| `--variant` | `0` | Cache variant flag used in the generated key name. |
| `--sizes` | `512x512,256x256,96x96` | Comma-separated thumbnail sizes to generate. |
| `--limit-per-column` | `50000` | Max distinct values read per URI-like column. |
| `--include-nonimage-ext` | off | Also consider cache files whose extension is not `.jpg`/`.jpeg`. |

### Output columns

The CSV contains: `cache_file_name`, `cache_full_path`, `cache_key_name`,
`source_table`, `source_column`, `db_value`, `normalized_value_used`, `algo`,
`encoding_form`, `variant`, `width`, `height`.

## Requirements

Python 3 standard library only — no third-party packages required.

## Testing

A `pytest` suite covers the cache-key generation, URI normalisation, column
discovery, cache indexing, and an end-to-end correlation run:

```
pip install pytest
pytest
```

## Provenance

`mediaplayer_cache_matcher.py` was recovered from the PyInstaller-bundled
executable in `mediaplayer_LocalCache_file.zip` and verified to be byte-for-byte
equivalent to the original bytecode. See [`METHODOLOGY.md`](METHODOLOGY.md) for
the full process and the resources used.

## Note

This is a forensic correlation aid. As stated above, it correlates a *cache key
name* to a DB-derived string; it does not by itself prove playback. Work on a
copy of the evidence.
