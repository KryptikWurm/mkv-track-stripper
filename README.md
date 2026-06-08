# MKV Track Stripper & Library Cleaner

A collection of ultra-resilient, self-contained Python scripts designed to automate the stripping of unwanted audio and subtitle tracks from MKV files. These tools interact seamlessly with SABnzbd and Radarr to optimize disk space and enforce language preferences without interrupting your automated media pipeline.

## Features
- Language-Based Filtering: Keeps only your preferred languages (e.g., English and Japanese) and strips everything else.
- Dynamic Undetermined (und) Track Handling: Retains undetermined tracks only if your primary language (English) is missing or if it is the sole audio track in the file (preventing silent movies).
- Automated Auxiliary Preservation: Automatically protects forced subtitle tracks — detected via the `forced_track` flag or a "forced" marker in the track name — regardless of their language tag.
- Junk Track Discrimination: Automatically detects and strips commentary, director's notes, descriptive audio, and DVS tracks (both scripts).
- Default Audio Enforcement: Sets the first kept audio track as the sole default and explicitly clears the default flag on every other kept audio track, so a stale flag carried over from the source can't leave two defaults.
- Extra Cleanup Passes: Normalizes cosmetic/structural metadata alongside the track strip — removes the global container title (usually a release-group filename), wipes global and per-track tags, clears junk track names (commentary/SDH/etc) on kept tracks, and fills an undefined (`und`) track language from a language word in its name (e.g. "English" → `eng`) *before* the language filter runs so the fix actually feeds selection. Optionally drops attachments (cover art / embedded fonts), off by default since fonts can matter for styled subtitles. Title/tags/language-fill/junk-name passes are on by default; each is individually toggleable (CLI flags on `mkvclean`, config constants in `mkv_strip_pp.py`). These run on both the full-remux and the in-place `mkvpropedit` fast-path.
- Metadata-Only Fast-Path: When a file has no tracks to strip but its header is still wrong — a missing/duplicate default-audio flag or a junk global title — it is fixed in place with `mkvpropedit` instead of a full remux. The edit touches only the header on the existing file, so there's no temp file, atomic swap, or disk-space check, and ownership/permissions/ACLs are preserved inherently.
- Metadata Preservation: After the atomic swap, restores the original ownership, permissions, timestamps, extended attributes, and POSIX ACLs onto the cleaned file. If ownership can't be restored (e.g. running as a non-root user) it logs a warning and continues rather than failing.
- Verified Output Before Swap: Before the cleaned file replaces the original, it is re-probed with `mkvmerge -J` to confirm it still carries a video track and exactly the audio/subtitle track counts that were requested. If the re-probe fails or the counts don't match, the original is kept and the error is logged — guarding against a truncated-but-nonzero remux silently overwriting a good source.
- Atomic Swapping & Zero-Copy Safety: Executes remuxing directly inside the file's current directory using mkvmerge. The final file is atomically swapped into place, eliminating cross-device link errors (EXDEV) across UnionFS/MergerFS/ZFS pools.
- Non-Blocking Execution Philosophy: If an error occurs or mkvmerge fails, the original file is left completely intact, the error is logged, and the script exits gracefully (0) so the automation pipeline never stalls.

## 1. Automated Hook: `mkv_strip_pp.py`
   This script integrates directly into SABnzbd as a post-processing script. It cleans files immediately after downloading and before Radarr attempts to import them.
   
### SABnzbd Setup
Place `mkv_strip_pp.py` into your SABnzbd scripts directory.
Make the script executable:
```Bash
chmod +x mkv_strip_pp.py
```

In the SABnzbd Web UI, assign the script to your specific movie or TV categories.

### Docker (LinuxServer.io SABnzbd)

The stock `lscr.io/linuxserver/sabnzbd` image does not include `mkvmerge`. The included `Dockerfile` extends it and bakes in the required tooling:

```dockerfile
FROM lscr.io/linuxserver/sabnzbd:latest

# Add mkvmerge (from MKVToolNix) so the post-processing script can run.
# The LinuxServer.io image is Alpine-based, so use apk.
RUN apk add --no-cache mkvtoolnix acl
```

Build the image (run from the repo root, where the `Dockerfile` lives):

```Bash
docker build -t sabnzbd-mkvstrip .
```

Run it in place of the upstream image, mounting `mkv_strip_pp.py` into the container's scripts directory (`/config/scripts`):

```Bash
docker run -d \
  --name sabnzbd \
  -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC \
  -p 8080:8080 \
  -v /path/to/appdata/sabnzbd:/config \
  -v "$(pwd)/mkv_strip_pp.py":/config/scripts/mkv_strip_pp.py \
  -v /path/to/downloads:/downloads \
  --restart unless-stopped \
  sabnzbd-mkvstrip
```

Or with Docker Compose:

```yaml
services:
  sabnzbd:
    build: .                 # builds the Dockerfile in this repo
    # image: sabnzbd-mkvstrip  # (use instead of build: if you built it manually)
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

```Bash
docker compose up -d --build
```

After the container is up, mark the script executable and assign it to your categories in the SABnzbd Web UI as above:

```Bash
docker exec sabnzbd chmod +x /config/scripts/mkv_strip_pp.py
```

The default `LOG_FILE = "/config/mkv_strip_pp.log"` already points at the persistent `/config` volume.

### Manual Configuration
Open the script and adjust the top configuration block as needed:
```Python
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

## 2. Library Sweep Engine: `mkvclean`
   A heavy-duty, self-contained library management script meant to run on a schedule (via cron) or on-demand to clean up existing media folders managed by Radarr.
   
   Key Operational Enhancements
   - High-Speed Scan Engine: Uses os.scandir to traverse directories quickly, fetching stat metadata directly to reduce disk I/O strain on massive arrays.
   - JSON-Lines Checkpoint File: Tracks processed files by path, size, and modification time (mtime). This allows the script to skip already-cleaned files instantly and resume seamlessly after an interruption. If Radarr upgrades a file later, the script detects the new size/mtime and re-processes it.
   - Single-Instance Locking: Uses kernel-level file locking (fcntl) to prevent cron jobs and manual runs from overlapping and thrashing your disk array.  
   - Surrogateescape Path Resilience: Uses robust string error handling to prevent script crashes on files containing complex accents, foreign characters, or broken UTF-8 symbols.

### Usage Examples
Manual Run (Default Batch Size: 50 files):
```Bash
./mkvclean.py /media/Storage/Movies
```
Process the Entire Library with No Limits:
```Bash
./mkvclean.py /media/Storage --batch 0
```
Dry Run (See what would be stripped without making changes):
```Bash
./mkvclean.py /media/Storage --dry-run
```

### Scheduled Catch-All Cron Configuration
Add this to your crontab to run a sweep every night at 4:00 AM. The script uses a custom log path and the built-in lock file to ensure it runs safely in the background:
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
- Python 3.6+
- MKVToolNix Suite (`mkvmerge`): 
  Ensure the `mkvmerge` binary is installed and accessible via your system's PATH.
  - Ubuntu/Debian: `sudo apt install mkvtoolnix`
  - Alpine (Docker containers): `apk add mkvtoolnix`
- Optional — `acl` (`getfacl`/`setfacl`): only needed if you want POSIX ACLs copied onto cleaned files. Without it, ACL preservation is skipped silently; all other metadata (ownership, mode, timestamps, xattrs) is still preserved.
  - Ubuntu/Debian: `sudo apt install acl`
  - Alpine: `apk add acl`

> **Docker users:** the included `Dockerfile` installs both `mkvtoolnix` and `acl` on top of the LinuxServer.io SABnzbd image, so no manual dependency setup is needed — see [Docker (LinuxServer.io SABnzbd)](#docker-linuxserverio-sabnzbd).

## Troubleshooting

### `Could not preserve ownership (uid=… gid=…) … Operation not permitted`

This is a **non-fatal warning, not a failure.** The track stripping still completed: `mkvmerge` remuxed the file and it was atomically swapped into place. The only side effect is that the cleaned file is now owned by the user that ran the script instead of the original owner.

It happens because changing a file's owner to an *arbitrary* uid/gid requires `CAP_CHOWN` (effectively root). When the script runs as a non-root user that doesn't already own the files, the kernel denies the `chown` with `EPERM`, and the script logs the warning and continues by design.

To preserve the original ownership, run with enough privilege to perform the `chown`:

- **Bare-metal / cron:** run as root, e.g. `sudo ./mkvclean.py /media/Storage/Movies` (or schedule the cron job under root's crontab).
- **Docker (LinuxServer.io):** set `PUID`/`PGID` to match the media files' owner so the process *is* the owner. Note that matching the uid alone still won't let it set a different gid unless the process is a member of that group.

If you don't care about the ownership ending up as the runner, the warning is safe to ignore.

## License
This project is open-source and available under the MIT License. See [LICENSE](LICENSE) for details.