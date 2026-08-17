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
  3. Copies (or moves) each video into a Plex-style show layout, using
     ffmpeg to remux (not re-encode) it and embed metadata + thumbnail
     directly into the file, instead of writing sidecar .nfo/.jpg files:

        <dest>/<Show Name>/Season 01/<Show Name> - S01E03 - Title.mp4
        <dest>/<Show Name>/tvshow.nfo

     Title, description, publish date, and post ID are written as container
     metadata tags. The post's thumbnail (if any) is embedded as attached
     cover art for mp4/m4v/mov files, or as an mkv attachment for .mkv
     files; other containers get metadata tags only (a warning is printed
     if a thumbnail had to be skipped for that reason).

     tvshow.nfo is the one file that's still written to disk as a sidecar,
     since it describes the show as a whole rather than any single episode
     and so has nowhere to be embedded.

  This requires ffmpeg to be installed and on PATH. Embedding uses stream
  copy (-c copy), so the original video/audio aren't re-encoded -- it's a
  fast remux, not a transcode.

  NOTE: Plex's own "Local Media Assets" agent primarily reads sidecar .nfo
  files for TV episodes, not tags embedded in the video container itself.
  If accurate metadata in Plex's UI matters more to you than having the
  metadata travel with the file, sidecar .nfo files (the previous behavior
  of this script) will likely work better with Plex specifically. Embedded
  tags are still useful for portability, other players/tools that do read
  them (e.g. Kodi, mpv, ffprobe, mediainfo), and for players like Jellyfin
  that vary in how much embedded metadata they honor.

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
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Optional

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".ts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Containers where a thumbnail can be embedded as attached cover art
# (an extra video stream with disposition=attached_pic).
COVER_ART_CONTAINERS = {".mp4", ".m4v", ".mov"}
# Containers where a thumbnail is embedded as a generic file attachment.
ATTACH_CONTAINERS = {".mkv"}

MIME_BY_IMAGE_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
}

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

# When a post has a video under one of these folders (e.g. an embedded
# YouTube video patreon-dl downloaded) AND a video under a regular media
# folder, the "embed" one is preferred and the other is skipped -- see
# --prefer-embed / --no-prefer-embed.
EMBED_DIR_NAMES = {"embed", "embeds", "embedded"}

# Thumbnails found under these folders are ignored when picking an episode
# thumb -- ".thumbnails" holds patreon-dl's auto-generated video thumbnails,
# and images sitting alongside the video in the "video" folder are usually
# just video-player poster frames, neither of which tend to look good as
# Plex episode art. Add more names here if your version of patreon-dl uses
# different folder names.
THUMB_EXCLUDE_DIR_NAMES = {".thumbnails", "video", "videos"}

# Video files sitting under a folder with one of these names (anywhere
# under a post) are skipped entirely -- e.g. "video_preview" holds
# low-quality preview clips patreon-dl saves alongside the real video,
# which we don't want treated as an episode. Add more names here if your
# version of patreon-dl uses different folder names.
EXCLUDED_VIDEO_DIR_NAMES = {"video_preview"}

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
        stripper.close()
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
    post_dir: Optional[Path] = None


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


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def find_thumb(post_dir: Path) -> Optional[Path]:
    """Pick a candidate thumbnail image for the post, skipping anything
    under THUMB_EXCLUDE_DIR_NAMES. Prefers images found directly in an
    'images' folder, falling back to any other non-excluded image."""
    all_imgs = [
        p for p in post_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    ]

    def rel_parts(p: Path) -> tuple:
        try:
            return p.relative_to(post_dir).parts[:-1]
        except ValueError:
            return p.parts[:-1]

    def is_excluded(p: Path) -> bool:
        return any(part.lower() in THUMB_EXCLUDE_DIR_NAMES for part in rel_parts(p))

    candidates = [p for p in all_imgs if not is_excluded(p)]
    candidates = [p for p in candidates if _safe_size(p) > 0]
    if not candidates:
        return None

    def sort_key(p: Path):
        in_images_folder = any(part.lower() == "images" for part in rel_parts(p))
        return (0 if in_images_folder else 1, len(rel_parts(p)), str(p))

    candidates.sort(key=sort_key)
    return candidates[0]


def extract_meta(video_path: Path, source_root: Path) -> PostMeta:
    post_dir, json_path = find_post_dir_and_json(video_path, source_root)
    fallback_title = video_path.stem
    if json_path is None:
        return PostMeta(title=fallback_title, thumb_path=find_thumb(post_dir), post_dir=post_dir)

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [warn] Could not parse {json_path}: {e}", file=sys.stderr)
        return PostMeta(title=fallback_title, json_path=json_path, thumb_path=find_thumb(post_dir), post_dir=post_dir)

    raw_title = find_first(data, TITLE_KEYS)
    raw_desc = find_first(data, DESCRIPTION_KEYS)
    raw_date = find_first(data, DATE_KEYS)
    raw_id = find_first(data, ID_KEYS)

    stripped_title = strip_html(str(raw_title)) if raw_title else ""
    title_lines = stripped_title.splitlines()
    title = title_lines[0].strip() if title_lines and title_lines[0].strip() else fallback_title
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
        post_dir=post_dir,
    )


def is_embed_video(video_path: Path, post_dir: Path) -> bool:
    """True if `video_path` sits under a folder named like 'embed' between
    the post directory and the file itself (patreon-dl saves downloaded
    embedded videos, e.g. from YouTube, under such a folder)."""
    try:
        rel_parts = video_path.relative_to(post_dir).parts[:-1]
    except ValueError:
        rel_parts = video_path.parts[:-1]
    return any(part.lower() in EMBED_DIR_NAMES for part in rel_parts)


def drop_non_embed_duplicates(metas: list[tuple[Path, "PostMeta"]]) -> list[tuple[Path, "PostMeta"]]:
    """When a post has both an embedded video and a regular video, keep
    only the embedded one(s)."""
    groups: dict[Optional[Path], list[int]] = {}
    for i, (_, m) in enumerate(metas):
        groups.setdefault(m.post_dir, []).append(i)

    to_drop = set()
    for post_dir, idxs in groups.items():
        if post_dir is None or len(idxs) < 2:
            continue
        embed_idxs = [i for i in idxs if is_embed_video(metas[i][0], post_dir)]
        non_embed_idxs = [i for i in idxs if i not in embed_idxs]
        if embed_idxs and non_embed_idxs:
            for i in non_embed_idxs:
                print(f"  [prefer-embed] skipping {metas[i][0]} "
                      f"(embedded video found for the same post)")
            to_drop.update(non_embed_idxs)

    return [item for i, item in enumerate(metas) if i not in to_drop]


def is_in_excluded_dir(path: Path, excluded_names: set) -> bool:
    return any(part.lower() in excluded_names for part in path.parts[:-1])


def find_videos(source_root: Path) -> list[Path]:
    return sorted(
        p for p in source_root.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        and not is_in_excluded_dir(p, EXCLUDED_VIDEO_DIR_NAMES)
    )


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


def mimetype_for_image(path: Path) -> str:
    return MIME_BY_IMAGE_EXT.get(path.suffix.lower(), "application/octet-stream")


def build_metadata_args(meta: PostMeta, show_name: str, season: int, episode: int) -> list[str]:
    args = [
        "-metadata", f"title={meta.title}",
        "-metadata", f"show={show_name}",
        "-metadata", f"season_number={season}",
        "-metadata", f"episode_sort={episode}",
    ]
    if meta.description:
        args += ["-metadata", f"comment={meta.description}",
                  "-metadata", f"synopsis={meta.description}"]
    if meta.published:
        args += ["-metadata", f"date={meta.published.strftime('%Y-%m-%d')}"]
    if meta.post_id:
        args += ["-metadata", f"episode_id={meta.post_id}"]
    return args


def embed_and_place_video(ffmpeg: Optional[str], src: Path, dest: Path, meta: PostMeta,
                           show_name: str, season: int, episode: int,
                           mode: str, dry_run: bool):
    if dest.exists():
        print(f"  [skip] already exists: {dest}")
        return

    ext = dest.suffix.lower()
    have_thumb = bool(meta.thumb_path and meta.thumb_path.exists())

    if ffmpeg is None:
        print("  [warn] ffmpeg not found on PATH -- copying without embedding metadata/thumbnail")
        if dry_run:
            print(f"  [dry-run] plain {mode}: {src}  ->  {dest}")
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        if mode == "move":
            shutil.move(str(src), str(dest))
        else:
            shutil.copy2(src, dest)
        return

    metadata_args = build_metadata_args(meta, show_name, season, episode)

    if have_thumb and ext in COVER_ART_CONTAINERS:
        cmd = [ffmpeg, "-y", "-i", str(src), "-i", str(meta.thumb_path),
               "-map", "0", "-map", "1",
               "-c", "copy", "-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic"]
        cmd += metadata_args + [str(dest)]
    elif have_thumb and ext in ATTACH_CONTAINERS:
        cmd = [ffmpeg, "-y", "-i", str(src), "-map", "0", "-c", "copy"]
        cmd += metadata_args
        cmd += ["-attach", str(meta.thumb_path),
                "-metadata:s:t:0", f"mimetype={mimetype_for_image(meta.thumb_path)}"]
        cmd += [str(dest)]
    else:
        if have_thumb:
            print(f"  [warn] {ext} doesn't support an embedded cover image -- "
                  "embedding metadata tags only, thumbnail skipped")
        cmd = [ffmpeg, "-y", "-i", str(src), "-map", "0", "-c", "copy"]
        cmd += metadata_args + [str(dest)]

    if dry_run:
        print(f"  [dry-run] would run: {' '.join(cmd)}")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [warn] ffmpeg failed to embed metadata (exit {result.returncode}); "
              "falling back to a plain copy for this file.")
        print("    " + result.stderr.strip()[-800:].replace("\n", "\n    "))
        if dest.exists():
            dest.unlink(missing_ok=True)
        shutil.copy2(src, dest)

    if mode == "move":
        try:
            src.unlink()
        except OSError as e:
            print(f"  [warn] could not remove source file after move: {e}")


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

    if args.include_title:
        include_terms = [t.lower() for t in args.include_title]
        filtered = []
        skipped = 0
        for v, m in metas:
            title_lower = m.title.lower()
            if any(t in title_lower for t in include_terms):
                filtered.append((v, m))
            else:
                skipped += 1
                print(f"  [include-title] skipping '{m.title}' (no match): {v}")
        metas = filtered
        if skipped:
            print(f"\n[include-title] skipped {skipped} video(s) not matching any included title text")

    if args.exclude_title:
        exclude_terms = [t.lower() for t in args.exclude_title]
        filtered = []
        skipped = 0
        for v, m in metas:
            title_lower = m.title.lower()
            hit = next((t for t in exclude_terms if t in title_lower), None)
            if hit:
                skipped += 1
                print(f"  [exclude-title] skipping '{m.title}' (matched {hit!r}): {v}")
                continue
            filtered.append((v, m))
        metas = filtered
        if skipped:
            print(f"\n[exclude-title] skipped {skipped} video(s) with excluded title text")

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

    if args.prefer_embed:
        before = len(metas)
        metas = drop_non_embed_duplicates(metas)
        skipped = before - len(metas)
        if skipped:
            print(f"\n[prefer-embed] skipped {skipped} non-embedded duplicate video(s)")

    # Sort chronologically (undated items go last, in original discovery order)
    def sort_key(item):
        _, m = item
        return (m.published is None, m.published or datetime.max)
    metas.sort(key=sort_key)

    dest_root = Path(args.dest).expanduser().resolve()
    show_name = args.show_name
    show_dir = dest_root / sanitize(show_name)
    write_tvshow_nfo(show_dir, show_name, args.dry_run)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("\n[warn] ffmpeg was not found on PATH. Metadata and thumbnails "
              "will NOT be embedded -- videos will just be plain-copied. "
              "Install ffmpeg and make sure it's on PATH to enable embedding.")

    episode_counters: dict[int, int] = {}
    placed = 0

    for video_path, meta in metas:
        season = season_for(meta, args.season_mode, args.season)
        episode_counters[season] = episode_counters.get(season, 0) + 1
        episode = episode_counters[season]

        season_dir = show_dir / f"Season {season:02d}"
        base_name = f"{show_name} - S{season:02d}E{episode:03d} - {sanitize(meta.title)}"
        video_dest = season_dir / f"{base_name}{video_path.suffix.lower()}"

        print(f"\n[{season:02d}x{episode:03d}] {meta.title}")
        embed_and_place_video(ffmpeg, video_path, video_dest, meta, show_name,
                               season, episode, args.mode, args.dry_run)

        placed += 1

    print(f"\nDone. Processed {placed} episode(s) into: {show_dir}")
    print("In Plex: create/edit the library, set its agent to "
          "'Local Media Assets (TV)', and enable 'Prefer local metadata' "
          "under the library's Advanced settings so it reads tvshow.nfo. "
          "Note that Plex's episode metadata generally comes from sidecar "
          ".nfo files rather than tags embedded in the video itself, so "
          "per-episode titles/descriptions in Plex's UI may still show as "
          "auto-generated from the filename.")


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
    p.add_argument("--mode", choices=["copy", "move"], default="copy",
                   help="'copy' (default): leave the original video in place, write the "
                        "embedded-metadata version to the destination. 'move': same, but "
                        "delete the original after the destination file is written "
                        "successfully. (Embedding always writes a new file, so link/symlink "
                        "modes aren't available.)")
    p.add_argument("--prefer-embed", dest="prefer_embed",
                   action=argparse.BooleanOptionalAction, default=True,
                   help="When a post has a video under both an 'embed' folder and a regular "
                        "video/media folder, keep only the embedded one. Default: on "
                        "(use --no-prefer-embed to keep both).")
    p.add_argument("--include-title", action="append", default=[], metavar="TEXT",
                   help="Only keep posts whose title contains this text (case-insensitive). "
                        "Repeat the flag to include multiple strings (a post matching ANY "
                        "of them is kept), e.g. --include-title 'Episode' --include-title 'Special'. "
                        "Applied before --exclude-title.")
    p.add_argument("--exclude-title", action="append", default=[], metavar="TEXT",
                   help="Skip any post whose title contains this text (case-insensitive). "
                        "Repeat the flag to exclude multiple strings, e.g. "
                        "--exclude-title 'Q&A' --exclude-title 'Behind the Scenes'")
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
