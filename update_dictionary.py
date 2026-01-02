#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Union

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LENGTH_DELIMITED = 2
WIRE_32BIT = 5

STORAGE_FIELD_DICTIONARY = 2

DICT_FIELD_ID = 1
DICT_FIELD_NAME = 3
DICT_FIELD_ENTRY = 4

ENTRY_FIELD_KEY = 1
ENTRY_FIELD_VALUE = 2
ENTRY_FIELD_COMMENT = 4
ENTRY_FIELD_POS = 5

Field = tuple[int, int, Union[int, bytes]]

DEFAULT_DB_PATH = Path(
    "~/Library/Application Support/Google/JapaneseInput/user_dictionary.db"
).expanduser()
DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_POS = 1
DEFAULT_RELOAD_WAIT_SECONDS = 0.4

TOOL_APP_NAMES = ("DictionaryTool.app", "GoogleJapaneseInputTool.app")

DEFAULT_TOOL_ROOTS = [
    Path("/Library/Input Methods/GoogleJapaneseInput.app"),
    Path.home() / "Library/Input Methods/GoogleJapaneseInput.app",
    Path("/Applications/GoogleJapaneseInput.app"),
]

DEFAULT_TOOL_PATHS = [
    root / "Contents/Resources" / name
    for root in DEFAULT_TOOL_ROOTS
    for name in TOOL_APP_NAMES
]

WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


@dataclass(frozen=True)
class DayConfig:
    key: str
    offset_days: int


@dataclass
class Config:
    dictionary_name: str
    db_path: Path
    pos: int
    days: list[DayConfig]
    formats: list[str]


@dataclass
class DictionaryData:
    dict_id: int
    name: str
    entries_raw: list[bytes]
    unknown_fields: list[Field]


class ProtoDecodeError(ValueError):
    pass


def decode_varint(data: bytes, index: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        if index >= len(data):
            raise ProtoDecodeError("Unexpected end of data while decoding varint")
        b = data[index]
        index += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, index
        shift += 7
        if shift > 70:
            raise ProtoDecodeError("Varint is too long")


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("Varint cannot be negative")
    out = bytearray()
    while True:
        to_write = value & 0x7F
        value >>= 7
        if value:
            out.append(to_write | 0x80)
        else:
            out.append(to_write)
            return bytes(out)


def encode_tag(field_number: int, wire_type: int) -> bytes:
    return encode_varint((field_number << 3) | wire_type)


def encode_raw_field(field_number: int, wire_type: int, value: int | bytes) -> bytes:
    if wire_type == WIRE_VARINT:
        if not isinstance(value, int):
            raise ValueError("Varint field requires int value")
        return encode_tag(field_number, wire_type) + encode_varint(value)
    if wire_type == WIRE_64BIT:
        if not isinstance(value, (bytes, bytearray)) or len(value) != 8:
            raise ValueError("64-bit field requires 8 bytes")
        return encode_tag(field_number, wire_type) + bytes(value)
    if wire_type == WIRE_LENGTH_DELIMITED:
        if not isinstance(value, (bytes, bytearray)):
            raise ValueError("Length-delimited field requires bytes")
        return encode_tag(field_number, wire_type) + encode_varint(len(value)) + bytes(value)
    if wire_type == WIRE_32BIT:
        if not isinstance(value, (bytes, bytearray)) or len(value) != 4:
            raise ValueError("32-bit field requires 4 bytes")
        return encode_tag(field_number, wire_type) + bytes(value)
    raise ValueError(f"Unsupported wire type: {wire_type}")


def parse_fields(data: bytes) -> list[Field]:
    index = 0
    fields = []
    while index < len(data):
        tag, index = decode_varint(data, index)
        field_number = tag >> 3
        wire_type = tag & 0x7
        if wire_type == WIRE_VARINT:
            value, index = decode_varint(data, index)
            fields.append((field_number, wire_type, value))
        elif wire_type == WIRE_64BIT:
            if index + 8 > len(data):
                raise ProtoDecodeError("Unexpected end of data while decoding 64-bit field")
            value = data[index : index + 8]
            index += 8
            fields.append((field_number, wire_type, value))
        elif wire_type == WIRE_LENGTH_DELIMITED:
            length, index = decode_varint(data, index)
            if index + length > len(data):
                raise ProtoDecodeError("Unexpected end of data while decoding bytes field")
            value = data[index : index + length]
            index += length
            fields.append((field_number, wire_type, value))
        elif wire_type == WIRE_32BIT:
            if index + 4 > len(data):
                raise ProtoDecodeError("Unexpected end of data while decoding 32-bit field")
            value = data[index : index + 4]
            index += 4
            fields.append((field_number, wire_type, value))
        else:
            raise ProtoDecodeError(f"Unsupported wire type: {wire_type}")
    return fields


def parse_storage(raw: bytes) -> tuple[list[DictionaryData], list[Field]]:
    dictionaries: list[DictionaryData] = []
    unknown_fields: list[Field] = []

    for field_number, wire_type, value in parse_fields(raw):
        if field_number == STORAGE_FIELD_DICTIONARY and wire_type == WIRE_LENGTH_DELIMITED:
            dictionaries.append(parse_dictionary(value))
        else:
            unknown_fields.append((field_number, wire_type, value))

    return dictionaries, unknown_fields


def parse_dictionary(raw: bytes) -> DictionaryData:
    dict_id = None
    name = None
    entries: list[bytes] = []
    unknown: list[Field] = []

    for field_number, wire_type, value in parse_fields(raw):
        if field_number == DICT_FIELD_ID and wire_type == WIRE_VARINT:
            dict_id = int(value)
        elif field_number == DICT_FIELD_NAME and wire_type == WIRE_LENGTH_DELIMITED:
            name = value.decode("utf-8")
        elif field_number == DICT_FIELD_ENTRY and wire_type == WIRE_LENGTH_DELIMITED:
            if isinstance(value, (bytes, bytearray)):
                entries.append(bytes(value))
        else:
            unknown.append((field_number, wire_type, value))

    if dict_id is None or name is None:
        raise ProtoDecodeError("Dictionary record missing required fields")

    return DictionaryData(dict_id=dict_id, name=name, entries_raw=entries, unknown_fields=unknown)


def parse_entry_key(raw: bytes) -> str | None:
    for field_number, wire_type, value in parse_fields(raw):
        if field_number == ENTRY_FIELD_KEY and wire_type == WIRE_LENGTH_DELIMITED:
            try:
                return value.decode("utf-8")  # type: ignore[union-attr]
            except UnicodeDecodeError:
                return None
    return None


def build_entry(key: str, value: str, comment: str, pos: int) -> bytes:
    parts = [
        encode_raw_field(ENTRY_FIELD_KEY, WIRE_LENGTH_DELIMITED, key.encode("utf-8")),
        encode_raw_field(ENTRY_FIELD_VALUE, WIRE_LENGTH_DELIMITED, value.encode("utf-8")),
        encode_raw_field(ENTRY_FIELD_COMMENT, WIRE_LENGTH_DELIMITED, comment.encode("utf-8")),
        encode_raw_field(ENTRY_FIELD_POS, WIRE_VARINT, pos),
    ]
    return b"".join(parts)


def build_dictionary(data: DictionaryData) -> bytes:
    parts = [
        encode_raw_field(DICT_FIELD_ID, WIRE_VARINT, data.dict_id),
        encode_raw_field(DICT_FIELD_NAME, WIRE_LENGTH_DELIMITED, data.name.encode("utf-8")),
    ]
    for entry in data.entries_raw:
        parts.append(encode_raw_field(DICT_FIELD_ENTRY, WIRE_LENGTH_DELIMITED, entry))
    for field_number, wire_type, value in data.unknown_fields:
        parts.append(encode_raw_field(field_number, wire_type, value))
    return b"".join(parts)


def build_storage(
    dictionaries: Iterable[DictionaryData],
    unknown_fields: Iterable[Field],
) -> bytes:
    parts = []
    for dictionary in dictionaries:
        parts.append(
            encode_raw_field(STORAGE_FIELD_DICTIONARY, WIRE_LENGTH_DELIMITED, build_dictionary(dictionary))
        )
    for field_number, wire_type, value in unknown_fields:
        parts.append(encode_raw_field(field_number, wire_type, value))
    return b"".join(parts)


def generate_dictionary_id(existing: set[int]) -> int:
    while True:
        candidate = secrets.randbits(64)
        if candidate != 0 and candidate not in existing:
            return candidate


def parse_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))

    dictionary_name = raw.get("dictionary_name", "dict-weekday")
    db_path = Path(raw.get("db_path", str(DEFAULT_DB_PATH))).expanduser()
    pos = int(raw.get("pos", DEFAULT_POS))

    days = []
    for item in raw.get("days", []):
        if not isinstance(item, dict):
            raise ValueError("Each entry in 'days' must be an object")
        key = item.get("key")
        offset_days = item.get("offset_days")
        if key is None or offset_days is None:
            raise ValueError("Each day entry requires 'key' and 'offset_days'")
        days.append(DayConfig(key=str(key), offset_days=int(offset_days)))

    formats = [str(value) for value in raw.get("formats", [])]

    if not days:
        raise ValueError("Config requires at least one day entry")
    if not formats:
        raise ValueError("Config requires at least one format")

    return Config(
        dictionary_name=dictionary_name,
        db_path=db_path,
        pos=pos,
        days=days,
        formats=formats,
    )


def format_date(date: dt.date, fmt: str) -> str:
    month = date.month
    day = date.day

    iso_like = fmt.startswith("yyyy-") and "mm" in fmt and "dd" in fmt

    tokens = {
        "yyyy": f"{date.year:04d}",
        "MM": f"{month:02d}",
        "DD": f"{day:02d}",
        "mm": f"{month:02d}" if iso_like else str(month),
        "dd": f"{day:02d}" if iso_like else str(day),
        "m": str(month),
        "d": str(day),
    }

    ordered_tokens = ["yyyy", "MM", "DD", "mm", "dd", "m", "d"]
    result = []
    index = 0
    while index < len(fmt):
        matched = False
        for token in ordered_tokens:
            if fmt.startswith(token, index):
                result.append(tokens[token])
                index += len(token)
                matched = True
                break
        if not matched:
            result.append(fmt[index])
            index += 1

    rendered = "".join(result)
    if "(w)" in rendered:
        weekday = WEEKDAY_JA[date.weekday()]
        rendered = rendered.replace("(w)", f"({weekday})")

    return rendered


def unique_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def build_values_by_key(config: Config, today: dt.date) -> tuple[list[str], dict[str, list[str]]]:
    keys_in_order = [item.key for item in config.days]
    values_by_key: dict[str, list[str]] = {}

    for item in config.days:
        date = today + dt.timedelta(days=item.offset_days)
        values = unique_preserve(format_date(date, fmt) for fmt in config.formats)
        values_by_key[item.key] = values

    return keys_in_order, values_by_key


def build_entries_for_keys(
    keys_in_order: Iterable[str],
    values_by_key: dict[str, list[str]],
    pos: int,
    comment: str = "",
) -> list[bytes]:
    entries: list[bytes] = []
    for key in keys_in_order:
        for value in values_by_key[key]:
            entries.append(build_entry(key, value, comment, pos))
    return entries


def filter_entries_by_key(entries: Iterable[bytes], keys_to_remove: set[str]) -> list[bytes]:
    kept: list[bytes] = []
    for entry in entries:
        key = parse_entry_key(entry)
        if key is None or key not in keys_to_remove:
            kept.append(entry)
    return kept


def update_dictionary(
    raw: bytes,
    config: Config,
    today: dt.date,
) -> tuple[bytes, dict[str, int | str | bool]]:
    dictionaries, unknown_fields = parse_storage(raw)

    keys_in_order, values_by_key = build_values_by_key(config, today)
    target_keys = set(keys_in_order)
    new_entries = build_entries_for_keys(keys_in_order, values_by_key, config.pos)

    target_indices = [i for i, d in enumerate(dictionaries) if d.name == config.dictionary_name]
    created = False
    if not target_indices:
        existing_ids = {d.dict_id for d in dictionaries}
        new_id = generate_dictionary_id(existing_ids)
        dictionaries.append(
            DictionaryData(
                dict_id=new_id,
                name=config.dictionary_name,
                entries_raw=new_entries,
                unknown_fields=[],
            )
        )
        created = True
        updated_index = len(dictionaries) - 1
    else:
        updated_index = target_indices[0]
        if len(target_indices) > 1:
            print(
                f"Warning: multiple dictionaries named '{config.dictionary_name}' found; updating the first one.",
                file=sys.stderr,
            )

        target_dict = dictionaries[updated_index]
        kept_entries = filter_entries_by_key(target_dict.entries_raw, target_keys)
        target_dict.entries_raw = kept_entries + new_entries
        dictionaries[updated_index] = target_dict

    new_raw = build_storage(dictionaries, unknown_fields)

    stats = {
        "dictionary_name": config.dictionary_name,
        "dictionary_id": dictionaries[updated_index].dict_id,
        "created": created,
        "keys": len(keys_in_order),
        "entries": sum(len(values_by_key[key]) for key in keys_in_order),
    }

    return new_raw, stats


def write_atomic(path: Path, data: bytes, make_backup: bool = True) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if make_backup and path.exists():
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_suffix(path.suffix + f".bak.{timestamp}")
        shutil.copy2(path, backup_path)

    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_bytes(data)

    try:
        if path.exists():
            mode = path.stat().st_mode & 0o777
            os.chmod(temp_path, mode)
    except OSError:
        pass

    os.replace(temp_path, path)
    return backup_path


def find_tool_in_resources(resources_dir: Path) -> Path | None:
    for name in TOOL_APP_NAMES:
        candidate = resources_dir / name
        if candidate.exists():
            return candidate
    return None


def find_dictionary_tool_app(explicit_path: Path | None) -> Path | None:
    if explicit_path is not None:
        expanded = explicit_path.expanduser()
        if not expanded.exists():
            return None
        if expanded.is_dir() and expanded.suffix == ".app":
            if expanded.name in TOOL_APP_NAMES:
                return expanded
            if expanded.name == "GoogleJapaneseInput.app":
                candidate = find_tool_in_resources(expanded / "Contents/Resources")
                return candidate or expanded
            return expanded
        if expanded.is_dir():
            candidate = find_tool_in_resources(expanded)
            if candidate:
                return candidate
        return None
    for candidate in DEFAULT_TOOL_PATHS:
        if candidate.exists():
            return candidate
    return None


def run_osascript(lines: list[str]) -> subprocess.CompletedProcess[str]:
    args = ["osascript"]
    for line in lines:
        args.extend(["-e", line])
    return subprocess.run(args, check=False, capture_output=True, text=True)


def reload_google_japanese_input(tool_path: Path | None) -> tuple[bool, str | None]:
    if sys.platform != "darwin":
        return False, "Reload is only supported on macOS."

    app_path = find_dictionary_tool_app(tool_path)
    if app_path is None:
        return (
            False,
            "Dictionary Tool app not found (DictionaryTool.app or GoogleJapaneseInputTool.app). "
            "Use --tool-path to set it.",
        )

    open_result = subprocess.run(
        ["open", "-g", str(app_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if open_result.returncode != 0:
        message = open_result.stderr.strip() or "Failed to launch the Dictionary Tool."
        return False, message

    script_lines = [
        f'tell application "{app_path.stem}" to launch',
        f"delay {DEFAULT_RELOAD_WAIT_SECONDS}",
        f'tell application "{app_path.stem}" to quit',
    ]
    osa_result = run_osascript(script_lines)
    if osa_result.returncode != 0:
        message = osa_result.stderr.strip() or "Dictionary Tool did not quit cleanly."
        return True, message

    return True, None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update Google Japanese Input dictionary entries with weekday-aware date formats."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Override the dictionary DB path from config.",
    )
    parser.add_argument(
        "--today",
        type=str,
        default=None,
        help="Override today's date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the updates without writing the dictionary file.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create a timestamped backup of the dictionary file.",
    )
    parser.add_argument(
        "--reload",
        dest="reload",
        action="store_true",
        help="Reload Google Japanese Input by launching the Dictionary Tool (macOS only).",
    )
    parser.add_argument(
        "--no-reload",
        dest="reload",
        action="store_false",
        help="Skip reloading Google Japanese Input after updating.",
    )
    parser.add_argument(
        "--tool-path",
        type=Path,
        default=None,
        help="Path to DictionaryTool.app or GoogleJapaneseInputTool.app (macOS only).",
    )
    parser.set_defaults(reload=sys.platform == "darwin")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.config.exists():
        print(f"Config file not found: {args.config}", file=sys.stderr)
        return 1

    try:
        config = parse_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to load config: {exc}", file=sys.stderr)
        return 1

    if args.db_path is not None:
        config.db_path = args.db_path.expanduser()

    if args.today:
        try:
            today = dt.date.fromisoformat(args.today)
        except ValueError:
            print("--today must be in YYYY-MM-DD format", file=sys.stderr)
            return 1
    else:
        today = dt.date.today()

    raw = config.db_path.read_bytes() if config.db_path.exists() else b""

    try:
        new_raw, stats = update_dictionary(raw, config, today)
    except ProtoDecodeError as exc:
        print(f"Failed to parse dictionary file: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("Dry run only; no files will be written.")
        keys_in_order, values_by_key = build_values_by_key(config, today)
        for key in keys_in_order:
            values = ", ".join(values_by_key[key])
            print(f"{key}: {values}")
        return 0

    if new_raw == raw:
        print("No changes needed.")
        return 0

    backup = write_atomic(config.db_path, new_raw, make_backup=not args.no_backup)

    print(
        "Updated dictionary '{dictionary_name}' (id={dictionary_id}) with {entries} entries for {keys} keys.".format(
            **stats
        )
    )
    print(f"Dictionary path: {config.db_path}")
    if backup:
        print(f"Backup written to: {backup}")

    if args.reload:
        reloaded, message = reload_google_japanese_input(args.tool_path)
        if reloaded:
            if message:
                print(f"Reloaded Google Japanese Input (note: {message})", file=sys.stderr)
            else:
                print("Reloaded Google Japanese Input.")
        else:
            print(f"Reload skipped: {message}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
