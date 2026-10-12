#!/usr/bin/env python3
"""Syncs audio file rating tags into Navidrome user annotations.

This script scans audio files registered in Navidrome's database, extracts
rating tags (supporting FLAC/Vorbis comments, ID3 POPM, and TXXX frames),
and synchronizes them into the Navidrome annotation table for a specified user.
"""

import argparse
from datetime import datetime, timezone
import os
import sqlite3
import sys
from typing import Optional

try:
    import mutagen
except ImportError:
    print(
        "Error: mutagen library is required. Run: .venv/bin/pip install mutagen",
        file=sys.stderr,
    )
    sys.exit(1)


def extract_rating(file_path: str) -> int:
    """Extracts a 1-5 star rating from an audio file's metadata.

    Handles FLAC/Ogg Vorbis comments, ID3v2 POPM frames (used by Traktor, WMP,
    MediaMonkey, MusicBee), and custom TXXX:RATING frames.

    Args:
        file_path: The absolute path to the audio file.

    Returns:
        An integer rating between 1 and 5, or 0 if unrated or error occurs.
    """
    try:
        audio = mutagen.File(file_path)
    except Exception:
        return 0

    if audio is None:
        return 0

    # 1. Vorbis comments (FLAC, OGG, Opus)
    for tag in ("rating", "RATING", "Rating"):
        if tag in audio:
            raw_val = audio[tag]
            if isinstance(raw_val, (list, tuple)) and raw_val:
                raw_val = raw_val[0]
            try:
                numeric_rating = float(raw_val)
                # Normalize 0-100 scale down to 1-5 stars if applicable
                if numeric_rating > 5:
                    numeric_rating = numeric_rating / 20.0
                return max(1, min(5, round(numeric_rating)))
            except (ValueError, TypeError):
                pass

    # 2. ID3 tags (MP3, AIFF, WAV)
    for key, frame in audio.items():
        # Standard ID3 POPM (Popularimeter) rating scale: 1-255
        if key.startswith("POPM"):
            popm_rating = getattr(frame, "rating", 0)
            if popm_rating >= 242:
                return 5
            elif popm_rating >= 153:
                return 4
            elif popm_rating >= 96:
                return 3
            elif popm_rating >= 32:
                return 2
            elif popm_rating >= 1:
                return 1

        # ID3 User-defined text frame: TXXX:RATING
        if key.upper().startswith("TXXX:RATING"):
            text_values = getattr(frame, "text", ["0"])
            try:
                numeric_rating = float(text_values[0])
                if numeric_rating > 5:
                    numeric_rating = numeric_rating / 20.0
                return max(1, min(5, round(numeric_rating)))
            except (ValueError, TypeError, IndexError):
                pass

    return 0


def get_user_id(cursor: sqlite3.Cursor, username: str) -> Optional[str]:
    """Retrieves the internal user ID for the given Navidrome username.

    Args:
        cursor: SQLite cursor for navidrome.db.
        username: Navidrome account username.

    Returns:
        The string user ID if found, otherwise None.
    """
    cursor.execute("SELECT id FROM user WHERE user_name = ?", (username,))
    row = cursor.fetchone()
    return row[0] if row else None


def sync_ratings(
    db_path: str = "/var/lib/navidrome/navidrome.db",
    music_folder: str = "/mnt/norman_2tb/Backups/psych",
    username: str = "x1",
    force_all: bool = False,
) -> None:
    """Synchronizes file ratings to Navidrome database for a user.

    Args:
        db_path: Path to Navidrome SQLite database.
        music_folder: Root directory where music files are stored.
        username: Target Navidrome username to update ratings for.
        force_all: If True, re-checks all files even if already rated.
    """
    if not os.path.exists(db_path):
        print(f"Error: Database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    user_id = get_user_id(cur, username)
    if not user_id:
        print(f"Error: User '{username}' not found in database.", file=sys.stderr)
        sys.exit(1)

    print(f"Found user '{username}' (ID: {user_id})")

    # Select media files. By default, check files that don't have a positive rating yet.
    if force_all:
        query = "SELECT id, path FROM media_file"
        cur.execute(query)
    else:
        query = """
            SELECT m.id, m.path
            FROM media_file m
            LEFT JOIN annotation a ON a.item_id = m.id
                AND a.user_id = ?
                AND a.item_type = 'media_file'
            WHERE a.rating IS NULL OR a.rating = 0
        """
        cur.execute(query, (user_id,))

    tracks = cur.fetchall()
    total = len(tracks)
    print(f"Scanning {total} tracks for ratings...")

    now_iso = datetime.now(timezone.utc).isoformat()
    updated_count = 0

    upsert_stmt = """
        INSERT INTO annotation (
            user_id, item_id, item_type, rating, rated_at
        ) VALUES (?, ?, 'media_file', ?, ?)
        ON CONFLICT(user_id, item_id, item_type)
        DO UPDATE SET
            rating = excluded.rating,
            rated_at = excluded.rated_at
    """

    batch_updates = []
    for idx, (media_id, rel_path) in enumerate(tracks, start=1):
        full_path = os.path.join(music_folder, rel_path)
        if not os.path.exists(full_path):
            continue

        rating = extract_rating(full_path)
        if rating > 0:
            batch_updates.append((user_id, media_id, rating, now_iso))
            updated_count += 1

        # Commit in batches of 500 for high throughput
        if len(batch_updates) >= 500:
            cur.executemany(upsert_stmt, batch_updates)
            conn.commit()
            batch_updates.clear()

        if idx % 1000 == 0 or idx == total:
            print(f"Progress: {idx}/{total} tracks inspected ({updated_count} rated so far)")

    if batch_updates:
        cur.executemany(upsert_stmt, batch_updates)
        conn.commit()

    conn.close()
    print(f"\nDone! Successfully updated {updated_count} track ratings for user '{username}'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sync audio file rating tags into Navidrome user account."
    )
    parser.add_argument(
        "--db",
        default="/var/lib/navidrome/navidrome.db",
        help="Path to navidrome.db",
    )
    parser.add_argument(
        "--music",
        default="/mnt/norman_2tb/Backups/psych",
        help="Path to music library",
    )
    parser.add_argument(
        "--user",
        default="x1",
        help="Navidrome username to update ratings for",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force rescan and overwrite of existing ratings",
    )

    args = parser.parse_args()
    sync_ratings(
        db_path=args.db,
        music_folder=args.music,
        username=args.user,
        force_all=args.force,
    )
