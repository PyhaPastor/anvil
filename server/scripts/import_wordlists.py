#!/usr/bin/env python3
"""
import_wordlists.py — register wordlist files in the Anvil database.

Usage (from server/ directory):
    python3 scripts/import_wordlists.py <file_or_dir> [options]

Examples:
    python3 scripts/import_wordlists.py /opt/anvil/wordlists/rockyou.txt
    python3 scripts/import_wordlists.py /opt/anvil/wordlists/rockyou.txt --name "RockYou 2021" --category "Common"
    python3 scripts/import_wordlists.py /opt/anvil/wordlists/           --category "Common"
    python3 scripts/import_wordlists.py /opt/anvil/wordlists/rockyou.txt --dry-run
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SUPPORTED_EXTENSIONS = {".txt", ".lst", ".wordlist", ".dic", ".dict"}


def count_lines(path: Path) -> int:
    """Count newlines fast in binary mode — no encoding issues."""
    total = path.stat().st_size
    done = 0
    count = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            count += chunk.count(b"\n")
            done += len(chunk)
            pct = done / total * 100 if total else 0
            done_gb = done / 1024 ** 3
            total_gb = total / 1024 ** 3
            print(f"\r    {done_gb:.1f} / {total_gb:.1f} GB  ({pct:.1f}%)", end="", flush=True)
    print()  # newline after progress
    return count


def find_db(start: Path) -> Path:
    """Walk up from start and from the script's own location looking for anvil.db."""
    script_dir = Path(__file__).resolve().parent
    candidates = [start, start.parent, start.parent.parent,
                  script_dir, script_dir.parent, script_dir.parent.parent]
    for candidate in candidates:
        db = candidate / "anvil.db"
        if db.exists():
            return db
    raise FileNotFoundError(
        "Could not find anvil.db. Pass it explicitly with --db /opt/anvil/server/anvil.db"
    )


def register(db_path: Path, file_path: Path, name: str, category: str | None,
             description: str | None, dry_run: bool) -> bool:
    abs_path = str(file_path.resolve())
    size = file_path.stat().st_size

    print(f"  Counting lines in {file_path.name} ...")
    lines = count_lines(file_path)
    print(f"  {lines:,} lines")

    if dry_run:
        print(f"  [dry-run] Would insert: name={name!r}  path={abs_path}  "
              f"lines={lines:,}  size={size:,}  category={category!r}")
        return True

    con = sqlite3.connect(str(db_path))
    try:
        # Check if already registered by file path
        existing = con.execute(
            "SELECT id, name FROM wordlists WHERE file_path = ?", (abs_path,)
        ).fetchone()
        if existing:
            print(f"  Already registered (id={existing[0]}, name={existing[1]!r}) — skipping.")
            return False

        con.execute(
            "INSERT INTO wordlists (name, description, file_path, file_size_bytes, "
            "line_count, category, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, description, abs_path, size, lines, category,
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        )
        con.commit()
        wl_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        print(f"  Registered as id={wl_id}  name={name!r}")
        return True
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register wordlist files in the Anvil database."
    )
    parser.add_argument("path", help="File or directory to import")
    parser.add_argument("--name", help="Display name (single file only; default: filename)")
    parser.add_argument("--category", default=None, help="Category label (e.g. 'Common', 'Targeted')")
    parser.add_argument("--description", default=None, help="Optional description")
    parser.add_argument("--db", default=None, help="Explicit path to anvil.db")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without writing")
    args = parser.parse_args()

    target = Path(args.path)
    if not target.exists():
        print(f"Error: {target} does not exist.", file=sys.stderr)
        sys.exit(1)

    db_path = Path(args.db) if args.db else find_db(Path.cwd())
    print(f"Using database: {db_path}\n")

    if target.is_file():
        files = [target]
    else:
        files = sorted(
            p for p in target.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        if not files:
            print(f"No wordlist files found in {target} "
                  f"(looked for {', '.join(SUPPORTED_EXTENSIONS)}).")
            sys.exit(0)
        print(f"Found {len(files)} file(s) in {target}\n")

    added = skipped = 0
    for f in files:
        display_name = args.name if (args.name and len(files) == 1) else f.stem.replace("_", " ").replace("-", " ").title()
        print(f"→ {f.name}")
        ok = register(db_path, f, display_name, args.category, args.description, args.dry_run)
        if ok:
            added += 1
        else:
            skipped += 1

    print(f"\nDone. Added: {added}  Skipped (already registered): {skipped}")


if __name__ == "__main__":
    main()
