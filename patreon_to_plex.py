#!/usr/bin/env python3
"""
patreon_to_plex.py

Turn a patreon-dl download folder into a Plex "TV Show".

patreon-dl saves content like this (see project README):

    out-dir/
      <campaign>/
        posts/
          <post 1>/
            post_info/          <- JSON metadata for the post lives somewhere in here
            images/
            some-video.mp4
          <post 2>/
            ...

This script:
  1. Walks the source folder recursively for video files.
  2. For each video, looks for the nearest post-metadata JSON file (walking
     up from the video's folder) and pulls out title / description / date /
     id using a flexible, schema-tolerant search (patreon-dl's exact JSON
     field names can vary between versions, so we search for a list of
     likely key names rather than hard-coding one path).
  3. Copies (or moves/links) each video into a Plex-style show layout:

        <dest>/<Show Name>/Season 01/<Show Name> - S01E03 - Title.mp4
        <dest>/<Show Name>/Season 01/<Show Name> - S01E03 - Title.nfo
        <dest>/<Show Name>/Season 01/<Show Name> - S01E03 - Title.jpg   (if a thumb was found)
        <dest>/<Show Name>/tvshow.nfo

  The .nfo files use the Kodi-style <episodedetails> / <tvshow> XML schema,
  which Plex's "Local Media Assets" agent reads natively (Settings > your
  library > Advanced > "Local Media Assets" agent, with "Prefer local
  metadata" turned on).

IMPORTANT: patreon-dl's exact JSON layout isn't guaranteed to match what
this script guesses. Run with --inspect first to see what was found for the
first few posts, and adjust EXTRA_TITLE_KEYS / EXTRA_DATE_KEYS / etc. below
if titles/dates come back empty.

Usage:
    python3 patreon_to_plex.py \\
        --source "/path/to/patreon-dl/out-dir" \\
        --dest   "/path/to/Plex/TV Shows" \\
        --show-name "My Creator" \\
        --season-mode year \\
        --mode copy

    # Preview what would happen without touching any files:
    python3 patreon_to_plex.py --source ... --dest ... --show-name ... --dry-run

    # See what metadata gets extracted from the first few posts:
    python3 patreon_to_plex.py --source ... --inspect
"""

import argparse
import html
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".ts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Candidate JSON key names to try, in priority order. patreon-dl / the
# Patreon API sometimes nests things under "attributes" (JSON:API style),
# which the recursive search below handles automatically.
TITLE_KEYS = ["title", "name", "content_name", "postTitle", "post_title"]
DESCRIPTION_KEYS = ["content", "summary", "description", "teaser", "teaserText", "body"]
DATE_KEYS = [
    "publishDate", "published_at", "publishedAt", "postedAt",
    "createdAt", "created_at", "insertedAt", "date"
]
ID_KEYS = ["id", "postId", "post_id", "contentId", "content_id"]

# Filename characters that are illegal (or awkward) on Windows/macOS/Linux.
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, max_len: int = 120) -> str:
    name = _ILLEGAL_CHARS.sub("", name).strip().rstrip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "Untitled"
    return name[:max_len].strip()


class _HTMLStripper(HTMLParser):
    """Minimal HTML -> plain text converter (stdlib only, no dependencies)."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "div", "li"):
            self.parts.append("\n")

    def get_text(self) -> str:
        return html.unescape("".join(self.parts)).strip()


def strip_html(text: str) -> str:
    if not text:
        return ""
    stripper = _HTMLStripper()
    try:
        stripper.feed(text)
        return re.sub(r"\n{3,}", "\n\n", stripper.get_text())
    except Exception:
        # Fall back to a crude tag stripper if something malformed shows up.
        return re.sub(r"<[^>]+>", "", text).strip()


def find_first(data: Any, keys: list[str], _depth: int = 0) -> Optional[Any]:
    """
    Recursively search a JSON-decoded structure (dicts/lists) for the first
    value whose key matches (case-insensitively) any name in `keys`.
    This is intentionally schema-tolerant since patreon-dl's exact JSON
    shape can vary by version / content type.
    """
    if _depth > 6:
        return None
    lowered = {k.lower() for k in keys}
    if isinstance(data, dict):
        # Direct match first
        for k, v in data.items():
            if isinstance(k, str) and k.lower() in lowered:
                if isinstance(v, (str, int, float)) and str(v).strip() != "":
                    return v
        # Then recurse
        for v in data.values():
            found = find_first(v, keys, _depth + 1)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_first(item, keys, _depth + 1)
            if found is not None:
                return found
    return None


def parse_date(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value)
        except (ValueError, OSError):
            return None
    s = str(value).strip()
    # Try ISO 8601 first (handles "...Z" too), then a few common fallbacks.
    candidates = [s, s.replace("Z", "+00:00")]
    for cand in candidates:
        try:
            return datetime.fromisoformat(cand)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


@dataclass
class PostMeta:
    title: str
    description: str = ""
    published: Optional[datetime] = None
    post_id: Optional[str] = None
    json_path: Optional[Path] = None
    thumb_path: Optional[Path] = None


def load_json_files(dir_path: Path) -> list[Path]:
    return sorted(p for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() == ".json")


def find_post_dir_and_json(video_path: Path, source_root: Path) -> tuple[Path, Optional[Path]]:
    """
    Walk upward from the video's folder (stopping at source_root) looking
    for the post's directory and a JSON metadata file. patreon-dl nests
    JSON info under a 'post_info' folder or as a loose *.json file inside
    the post directory, so we check both patterns at each level.
    """
    current = video_path.parent
    while True:
        # Direct JSON files in this folder
        jsons = load_json_files(current)
        # A subfolder literally called post_info / content_info / info
        for sub_name in ("post_info", "content_info", "info"):
            sub = current / sub_name
            if sub.is_dir():
                jsons = load_json_files(sub) + jsons
        if jsons:
            # Prefer a file with "post" or "info" in its name if several exist
            jsons.sort(key=lambda p: ("info" not in p.stem.lower(), p.name))
            return current, jsons[0]
        if current == source_root or current.parent == current:
            return current, None
        current = current.parent


def find_thumb(post_dir: Path) -> Optional[Path]:
    search_dirs = [post_dir, post_dir / "images"]
    for d in search_dirs:
        if d.is_dir():
            imgs = sorted(
                p for p in d.rglob("*")
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
            )
            if imgs:
                return imgs[0]
    return None


def extract_meta(video_path: Path, source_root: Path) -> PostMeta:
    post_dir, json_path = find_post_dir_and_json(video_path, source_root)
    fallback_title = video_path.stem
    if json_path is None:
        return PostMeta(title=fallback_title, thumb_path=find_thumb(post_dir))

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [warn] Could not parse {json_path}: {e}", file=sys.stderr)
        return PostMeta(title=fallback_title, json_path=json_path, thumb_path=find_thumb(post_dir))

    raw_title = find_first(data, TITLE_KEYS)
    raw_desc = find_first(data, DESCRIPTION_KEYS)
    raw_date = find_first(data, DATE_KEYS)
    raw_id = find_first(data, ID_KEYS)

    title = strip_html(str(raw_title)).splitlines()[0].strip() if raw_title else fallback_title
    description = strip_html(str(raw_desc)) if raw_desc else ""
    published = parse_date(raw_date)
    post_id = str(raw_id) if raw_id is not None else None

    return PostMeta(
        title=title or fallback_title,
        description=description,
        published=published,
        post_id=post_id,
        json_path=json_path,
        thumb_path=find_thumb(post_dir),
    )


def find_videos(source_root: Path) -> list[Path]:
    return sorted(
        p for p in source_root.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )


def write_episode_nfo(nfo_path: Path, show_name: str, meta: PostMeta,
                       season: int, episode: int, dry_run: bool):
    aired = meta.published.strftime("%Y-%m-%d") if meta.published else ""
    plot = meta.description or ""
    uniqueid = meta.post_id or ""

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;"))

    xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<episodedetails>
  <title>{esc(meta.title)}</title>
  <showtitle>{esc(show_name)}</showtitle>
  <season>{season}</season>
  <episode>{episode}</episode>
  <aired>{aired}</aired>
  <premiered>{aired}</premiered>
  <plot>{esc(plot)}</plot>
  <uniqueid type="patreon" default="true">{esc(uniqueid)}</uniqueid>
</episodedetails>
"""
    if dry_run:
        print(f"  [dry-run] would write {nfo_path}")
        return
    nfo_path.write_text(xml, encoding="utf-8")


def write_tvshow_nfo(show_dir: Path, show_name: str, dry_run: bool):
    xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<tvshow>
  <title>{show_name}</title>
</tvshow>
"""
    path = show_dir / "tvshow.nfo"
    if path.exists():
        return
    if dry_run:
        print(f"  [dry-run] would write {path}")
        return
    show_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")


def place_file(src: Path, dest: Path, mode: str, dry_run: bool):
    if dry_run:
        print(f"  [dry-run] {mode}: {src}  ->  {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"  [skip] already exists: {dest}")
        return
    if mode == "copy":
        shutil.copy2(src, dest)
    elif mode == "move":
        shutil.move(str(src), str(dest))
    elif mode == "link":
        import os
        os.link(src, dest)
    elif mode == "symlink":
        dest.symlink_to(src.resolve())
    else:
        raise ValueError(f"Unknown mode: {mode}")


def season_for(meta: PostMeta, season_mode: str, fixed_season: int) -> int:
    if season_mode == "single":
        return fixed_season
    if season_mode == "year" and meta.published:
        return meta.published.year
    # Fallback when there's no date to key off of
    return fixed_season


def run(args):
    source_root = Path(args.source).expanduser().resolve()
    if not source_root.is_dir():
        print(f"Source folder not found: {source_root}", file=sys.stderr)
        sys.exit(1)

    videos = find_videos(source_root)
    if not videos:
        print("No video files found under the source folder.")
        return
    print(f"Found {len(videos)} video file(s) under {source_root}")

    metas = []
    for v in videos:
        m = extract_meta(v, source_root)
        metas.append((v, m))

    if args.inspect:
        print("\n--- Metadata inspection (first 10) ---")
        for v, m in metas[:10]:
            print(f"\nVideo:       {v}")
            print(f"  JSON used: {m.json_path}")
            print(f"  Title:     {m.title!r}")
            print(f"  Published: {m.published}")
            print(f"  Post ID:   {m.post_id}")
            print(f"  Thumb:     {m.thumb_path}")
            print(f"  Desc:      {(m.description[:120] + '...') if len(m.description) > 120 else m.description!r}")
        print("\nIf Title/Published look wrong or empty, edit TITLE_KEYS / "
              "DATE_KEYS near the top of this script to match your JSON, "
              "then re-run --inspect.")
        return

    # Sort chronologically (undated items go last, in original discovery order)
    def sort_key(item):
        _, m = item
        return (m.published is None, m.published or datetime.max)
    metas.sort(key=sort_key)

    dest_root = Path(args.dest).expanduser().resolve()
    show_name = args.show_name
    show_dir = dest_root / sanitize(show_name)
    write_tvshow_nfo(show_dir, show_name, args.dry_run)

    episode_counters: dict[int, int] = {}
    seen_ids = set()
    placed = 0

    for video_path, meta in metas:
        # Skip exact duplicate posts (e.g. multiple videos already handled
        # individually is fine -- this only dedupes literal same-file entries)
        season = season_for(meta, args.season_mode, args.season)
        episode_counters[season] = episode_counters.get(season, 0) + 1
        episode = episode_counters[season]

        season_dir = show_dir / f"Season {season:02d}"
        base_name = f"{show_name} - S{season:02d}E{episode:03d} - {sanitize(meta.title)}"

        video_dest = season_dir / f"{base_name}{video_path.suffix.lower()}"
        nfo_dest = season_dir / f"{base_name}.nfo"

        print(f"\n[{season:02d}x{episode:03d}] {meta.title}")
        place_file(video_path, video_dest, args.mode, args.dry_run)
        write_episode_nfo(nfo_dest, show_name, meta, season, episode, args.dry_run)

        if meta.thumb_path and meta.thumb_path.exists():
            thumb_dest = season_dir / f"{base_name}{meta.thumb_path.suffix.lower()}"
            place_file(meta.thumb_path, thumb_dest, "copy", args.dry_run)

        placed += 1

    print(f"\nDone. Processed {placed} episode(s) into: {show_dir}")
    print("In Plex: create/edit the library, set its agent to "
          "'Local Media Assets (TV)', and enable 'Prefer local metadata' "
          "under the library's Advanced settings so it reads the .nfo files.")


def main():
    p = argparse.ArgumentParser(description="Copy patreon-dl videos into a Plex TV show layout with NFO metadata.")
    p.add_argument("--source", required=True, help="patreon-dl output folder (searched recursively)")
    p.add_argument("--dest", help="Destination library folder (the show folder is created inside it). "
                                   "Not required when using --inspect.")
    p.add_argument("--show-name", help="Name of the Plex show, e.g. the creator's name. "
                                        "Not required when using --inspect.")
    p.add_argument("--season-mode", choices=["year", "single"], default="year",
                   help="'year': one season per calendar year published (default). "
                        "'single': everything goes into one season (see --season).")
    p.add_argument("--season", type=int, default=1,
                   help="Season number to use for --season-mode=single, or as a fallback "
                        "when a post has no discoverable publish date. Default: 1")
    p.add_argument("--mode", choices=["copy", "move", "link", "symlink"], default="copy",
                   help="How to place video files at the destination. Default: copy")
    p.add_argument("--dry-run", action="store_true", help="Print what would happen without writing/copying anything")
    p.add_argument("--inspect", action="store_true",
                   help="Just print the metadata extracted for the first 10 videos and exit "
                        "(use this first to sanity-check field extraction)")
    args = p.parse_args()
    if not args.inspect and (not args.dest or not args.show_name):
        p.error("--dest and --show-name are required unless you're using --inspect")
    run(args)


if __name__ == "__main__":
    main()
