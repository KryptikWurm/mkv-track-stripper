# MKV Track Stripper & Library Cleaner

Every movie you grab shows up bloated: five audio tracks you'll never play, a commentary track, a "descriptive audio" track, and subtitles in nine languages you don't speak. One file, whatever. Multiply it across a whole library and you're burning real disk space on tracks nobody in your house will ever touch.

These two self-contained Python scripts fix that. They strip MKV files down to the audio and subtitle languages you actually want, then hand the clean file back to your pipeline like nothing happened. One runs as a SABnzbd post-processing hook, so files get cleaned the moment they land and before Radarr or Sonarr imports them. The other sweeps your existing library on a schedule and cleans up everything you already have.

The whole thing is built to never break your automation. If anything goes wrong, the original file is left untouched and the script exits cleanly. A failed strip is a no-op, not a stalled queue.

**Requirements:** Python 3.10+ and MKVToolNix (`mkvmerge`). See [Prerequisites](#prerequisites).

## Contents

- [MKV Track Stripper \& Library Cleaner](#mkv-track-stripper--library-cleaner)
  - [Contents](#contents)
  - [Features](#features)
    - [Extra cleanup passes](#extra-cleanup-passes)
  - [1. Automated Hook: `mkv_strip_pp.py`](#1-automated-hook-mkv_strip_pppy)
    - [SABnzbd setup](#sabnzbd-setup)
    - [Docker (LinuxServer.io SABnzbd)](#docker-linuxserverio-sabnzbd)
    - [Manual configuration](#manual-configuration)
  - [2. Library Sweep Engine: `mkvclean.py`](#2-library-sweep-engine-mkvcleanpy)
    - [How it stays out of its own way](#how-it-stays-out-of-its-own-way)
    - [Usage examples](#usage-examples)
    - [Scheduled catch-all cron configuration](#scheduled-catch-all-cron-configuration)
  - [CLI Arguments](#cli-arguments)
  - [Prerequisites](#prerequisites)
  - [Troubleshooting](#troubleshooting)
    - [`Could not preserve ownership (uid=… gid=…) … Operation not permitted`](#could-not-preserve-ownership-uid-gid--operation-not-permitted)
  - [License](#license)

## Features

- **Language-based filtering:** keeps only the languages you ask for (say, English and Japanese) and drops the rest.
- **Undetermined (`und`) track handling:** `und` tracks are a coin flip, so the script only keeps them when it has to: when your primary language is missing, or when it's the only audio track in the file. No accidental silent movies.
- **Forced-subtitle preservation:** forced subs survive no matter what language they're tagged as, detected through the `forced_track` flag or a "forced" marker in the track name. Those are the subs that translate the one line of Elvish, and you do not want to lose them.
- **Junk-track discrimination:** commentary, director's notes, descriptive audio, and DVS tracks get stripped automatically (both scripts).
- **Default-audio enforcement:** the first kept audio track becomes the sole default, and the default flag is explicitly cleared on every other kept audio track. A stale flag carried over from the source can't leave you with two defaults fighting each other.
- **Metadata-only fast-path:** sometimes a file has nothing to strip but its header is still wrong (missing or duplicate default-audio flag, junk global title). Instead of remuxing the whole thing, it gets patched in place with `mkvpropedit`. That means no temp file, no atomic swap, no disk-space check, and ownership/permissions/ACLs are preserved for free because the file never moves.
- **Verified output before swap:** before a cleaned file replaces the original, it's re-probed with `mkvmerge -J` to confirm it still has a video track and exactly the audio/subtitle counts that were requested. If the re-probe fails or the numbers don't add up, the original stays put and the error is logged. This is the guard against a truncated-but-nonzero remux quietly overwriting a perfectly good source.
- **Metadata preservation:** after the swap, the original ownership, permissions, timestamps, extended attributes, and POSIX ACLs are copied back onto the cleaned file. If ownership can't be restored (you're not root, you don't own the files) it logs a warning and keeps going instead of falling over.
- **Atomic swapping and zero-copy safety:** remuxing happens right inside the file's own directory, then the result is atomically swapped into place. No cross-device link errors when your storage is a UnionFS, MergerFS, or ZFS pool.
- **Non-blocking by design:** if `mkvmerge` fails or anything throws an error, the original file is left completely intact, the error goes to the log, and the script exits 0. Your pipeline never stalls waiting on a file that wouldn't cooperate.

### Extra cleanup passes

While it's in there stripping tracks, the script also tidies up the cosmetic and structural metadata that release groups leave lying around:

- Removes the global container title (usually the release-group filename).
- Wipes global and per-track tags.
- Clears junk track names (commentary, SDH, etc.) on the tracks it keeps.
- Fills in an undefined (`und`) track language from a language word in its name ("English" becomes `eng`). This one runs **before** the language filter on purpose, otherwise the fix would happen too late to actually affect which tracks get kept.
- Optionally drops attachments (cover art and embedded fonts). This is off by default, because those fonts can matter for styled subtitles and ripping them out can wreck how ASS/SSA subs render.

The title, tags, language-fill, and junk-name passes are all on by default and each one is individually toggleable (CLI flags on `mkvclean.py`, config constants in `mkv_strip_pp.py`). They run on both the full remux and the in-place `mkvpropedit` fast-path.

## 1. Automated Hook: `mkv_strip_pp.py`

This is the one that runs inside SABnzbd as a post-processing script. It cleans files the moment they finish downloading, before Radarr ever sees them, so the file that lands in your library is already clean.

### SABnzbd setup

Drop `mkv_strip_pp.py` into your SABnzbd scripts directory and make it executable:

```bash
chmod +x mkv_strip_pp.py
```

Then head into the SABnzbd web UI and assign the script to your movie and/or TV categories. Done.

### Docker (LinuxServer.io SABnzbd)

Heads up: the stock `lscr.io/linuxserver/sabnzbd` image doesn't ship `mkvmerge`, so the script has nothing to call. The included `Dockerfile` extends the image and bakes the tooling in:

```dockerfile
FROM lscr.io/linuxserver/sabnzbd:latest

# Add mkvmerge (from MKVToolNix) and the optional ACL so the post-processing script can run.
# The LinuxServer.io image is Alpine-based, so use apk.
RUN apk add --no-cache mkvtoolnix acl
```

Build it from the repo root, where the `Dockerfile` lives:

```bash
docker build -t sabnzbd-mkvstrip .
```

Run it in place of the upstream image, mounting `mkv_strip_pp.py` into the container's scripts directory (`/config/scripts`):

If you live in Compose like the rest of us:

```yaml
services:
  sabnzbd:
    build: .                 # builds the Dockerfile in this repo
    container_name: sabnzbd
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
    ports:
      - "8080:8080"
    volumes:
      - /path/to/appdata/sabnzbd:/config
      - ./mkv_strip_pp.py:/config/scripts/mkv_strip_pp.py
      - /path/to/downloads:/downloads
    restart: unless-stopped
```

```bash
docker compose up -d --build
```

Once the container is up, mark the script executable and assign it to your categories in the web UI, same as the bare-metal setup above:

```bash
docker exec sabnzbd chmod +x /config/scripts/mkv_strip_pp.py
```

The default `LOG_FILE = "/config/mkv_strip_pp.log"` already points at the persistent `/config` volume, so your logs survive a container rebuild.

### Manual configuration

Open the script and tweak the config block at the top to match what you actually want kept:

```python
AUDIO_LANGS = ["eng", "jpn", "und"]
SUB_LANGS = ["eng", "und"]
LOG_FILE = "/config/mkv_strip_pp.log"   # Change to a persistent path
DRY_RUN = False

# Extra cleanup passes (attachment removal is opt-in; fonts can matter for subs)
STRIP_TITLE = True
STRIP_TAGS = True
INFER_LANGUAGE = True
CLEAR_JUNK_TRACK_NAMES = True
STRIP_ATTACHMENTS = False
```

## 2. Library Sweep Engine: `mkvclean.py`

The hook above only catches new downloads. For the pile of stuff you already have, this is the script that goes back and cleans it. Run it by hand or throw it on a cron job and let it chew through the backlog while you sleep.

First, drop it somewhere on your `PATH` (the scheduled examples below assume this) and make it executable:

```bash
sudo cp mkvclean.py /usr/local/bin/
sudo chmod +x /usr/local/bin/mkvclean.py
```

### How it stays out of its own way

Running a strip across a massive array is exactly where naive scripts thrash your disks or trip over each other. A few things keep that from happening:

- **High-speed scan engine:** uses `os.scandir` to walk directories fast and pull `stat` metadata directly, which keeps disk I/O off the floor on big arrays.
- **JSON-lines checkpoint file:** tracks every processed file by path, size, and mtime, so already-cleaned files get skipped instantly and an interrupted run resumes right where it stopped. If Radarr upgrades a file later, the new size/mtime gives it away and the script re-cleans it.
- **Single-instance locking:** kernel-level file locking (`fcntl`) means a manual run and a cron job can't fire at the same time and grind your array into paste.
- **Surrogateescape path resilience:** the script handles filenames full of accents, foreign characters, or busted UTF-8 without crashing.

### Usage examples

Manual run (default batch size: 50 files):

```bash
./mkvclean.py /media/Storage/Movies
```

Process the entire library, no batch limit:

```bash
./mkvclean.py /media/Storage --batch 0
```

Dry run (see exactly what it would strip without touching a single file):

```bash
./mkvclean.py /media/Storage --dry-run
```

Always do a dry run first on a new library. Look at the log, confirm it's keeping what you expect, then set it loose.

### Scheduled catch-all cron configuration

Drop this in your crontab to sweep every night at 4 AM. The custom log path and the built-in lock file keep it safe to run unattended:

```bash
0 4 * * * /usr/local/bin/mkvclean.py /media/Storage/Movies --batch 0 --log ~/mkvclean-cron.log
```

## CLI Arguments

```
positional arguments:
  root                  Library root to scan recursively (default: /media/Storage)

options:
  -h, --help            show this help message and exit
  --batch BATCH         Process this many not-yet-handled files, then stop (0 = no limit)
  --audio AUDIO         Preferred audio languages, comma separated (default: eng,jpn,und)
  --subs SUBS           Preferred subtitle languages, comma separated (default: eng,und)
  --prefer-audio-channels N
                        If multiple English audio tracks exist, keep only one: prefer this
                        channel count (e.g. 6 = 5.1), else the most channels. Omit to keep
                        all English tracks; 0 = just keep the most channels (default: omitted)
  --checkpoint CHECKPOINT
                        Path to checkpoint file (default: ~/.mkvclean_checkpoint)
  --log LOG             Path to runtime log file (default: ~/mkvclean.log)
  --lock LOCK           Path to process lock file (default: /tmp/mkvclean.lock)
  --mkvmerge MKVMERGE   Path to mkvmerge binary (default: mkvmerge)
  --mkvpropedit MKVPROPEDIT
                        Path to mkvpropedit binary (default: mkvpropedit)
  --dry-run             Report what would change, modify nothing
  --strip-attachments   Drop attachments (cover art / fonts) during cleanup (default: keep)
  --keep-title          Keep the global container title (default: strip it)
  --keep-tags           Keep global/track tags (default: wipe them)
  --keep-track-names    Keep junk track names (default: clear commentary/SDH/etc names)
  --no-infer-lang       Don't fill undefined track languages from the track name
```

## Prerequisites

- **Python 3.10+** (built and tested on 3.13.5)
- **MKVToolNix (`mkvmerge`):** the binary needs to be installed and on your `PATH`.
  - Ubuntu/Debian: `sudo apt install mkvtoolnix`
  - Alpine (Docker containers): `apk add mkvtoolnix`
- **Optional, `acl` (`getfacl`/`setfacl`):** only matters if you want POSIX ACLs copied onto cleaned files. Skip it and ACL preservation is silently skipped too; everything else (ownership, mode, timestamps, xattrs) is still preserved.
  - Ubuntu/Debian: `sudo apt install acl`
  - Alpine: `apk add acl`

> **Docker users:** the included `Dockerfile` installs both `mkvtoolnix` and `acl` on top of the LinuxServer.io SABnzbd image, so there's nothing to set up by hand. See [Docker (LinuxServer.io SABnzbd)](#docker-linuxserverio-sabnzbd).

## Troubleshooting

### `Could not preserve ownership (uid=… gid=…) … Operation not permitted`

Relax, this one's a **warning, not a failure.** The strip already finished: `mkvmerge` remuxed the file and it was atomically swapped into place. The only catch is that the cleaned file now belongs to whoever ran the script instead of the original owner.

Here's why it happens. Changing a file's owner to some *arbitrary* uid/gid needs `CAP_CHOWN`, which in practice means root. When the script runs as a non-root user that doesn't already own the files, the kernel slaps the `chown` with `EPERM`, so the script logs it and keeps moving by design rather than dying on the spot.

If you want the original ownership preserved, give it enough privilege to do the `chown`:

- **Bare-metal / cron:** run it as root, e.g. `sudo ./mkvclean.py /media/Storage/Movies`, or put the cron job in root's crontab.
- **Docker (LinuxServer.io):** set `PUID`/`PGID` to match the media files' owner so the process *is* the owner. Matching the uid alone still won't let it set a different gid unless the process is in that group, so watch out for that.

And if you don't care who ends up owning the files, ignore the warning entirely. It changes nothing about the actual cleanup.

## License

Open-source under the MIT License. See [LICENSE](LICENSE) for details.
