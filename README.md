# Google Japanese Weekday Dictionary Updater

This script updates Google Japanese Input user dictionary entries so relative day words (きょう, あした, etc.) include weekday-aware date formats.

Confirmed working on macOS 26.2 (25C56).

## Usage

```bash
python3 update_dictionary.py
```

On macOS, the script will also trigger a reload by briefly launching the Google Japanese Input Dictionary Tool.

Dry run with a fixed date:

```bash
python3 update_dictionary.py --dry-run --today 2026-01-02
```

Skip backups:

```bash
python3 update_dictionary.py --no-backup
```

Skip the reload step:

```bash
python3 update_dictionary.py --no-reload
```

If the Dictionary Tool is installed in a non-standard location, pass its path (DictionaryTool.app in recent versions, GoogleJapaneseInputTool.app in older ones):

```bash
python3 update_dictionary.py --tool-path "/path/to/DictionaryTool.app"
```

## Config

Edit `config.json` to change the keywords, date offsets, or formats. The script reads `config.json` by default.

Key fields:

- `dictionary_name`: Dictionary name to create/update in `user_dictionary.db`.
- `db_path`: Path to Google Japanese Input dictionary DB.
- `days`: List of `{ "key": "...", "offset_days": N }`.
- `formats`: List of format strings; `(w)` inserts a weekday like `(金)`.
- `pos`: Part-of-speech ID used for new entries (default `1`).

### Format tokens

- `yyyy`: 4-digit year.
- `mm` / `dd`: Month/day without zero padding.
- `MM` / `DD`: Month/day with zero padding.
- `(w)`: Weekday marker in Japanese, e.g. `(金)`.

Note: `yyyy-mm-dd` is treated as an ISO-like format and uses zero-padded month/day even though it contains `mm`/`dd`.

## Notes

- The script makes a timestamped backup of `user_dictionary.db` before writing.
- If the dictionary file does not exist, it will be created.
- When running the update script, close the Google Japanese Input Dictionary Tool window.
- Reloading is only attempted on macOS; use `--no-reload` to disable it.
