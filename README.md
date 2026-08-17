Turn a patreon-dl download folder into a Plex TV Show.

## DISCLAIMER:

This is 100% written by Anthropic's Claude. I haven't tested every possible permutation of arguments, but everything is working in my test cases.

I wanted a solution for Patreon videos similar to [ytdl-sub](https://github.com/jmbannon/ytdl-sub), and I have already been using [patreon-dl](https://github.com/jmbannon/ytdl-sub) and [patreon-dl-gui](https://github.com/patrickkfkan/patreon-dl-gui) to download my Patreon subs. This script would be simple enough to write, but menial, so it seemed like a good use case for AI. It took a little bit of time getting everything just right, so I figured I'd share it in case anyone else can use this.

## Usage:

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

## How it works

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
 
        <dest>/<Show Name>/Season 2023/<Show Name> - S2023E050103 - Title.mp4
        <dest>/<Show Name>/tvshow.nfo
 
     By default, episode numbers are date-based rather than a plain
     incrementing counter, so filenames sort correctly by upload date even
     if posts are processed out of order later:
       - --season-mode=year:   mmddxx     (e.g. 050103 = May 1, 3rd upload that day)
       - --season-mode=single: yyyymmddxx (e.g. 2023050103)
     Posts with no discoverable publish date fall back to 9999-12-31 so
     they still get a validly-formatted code and sort after everything
     dated. Pass --sequential-episodes to use the old plain E001, E002...
     numbering instead.
 
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
