# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **mkvclean**: startup compaction of the checkpoint file (`compact_checkpoint()`). The checkpoint is append-only, so re-processing a file (e.g. a Radarr upgrade changing mtime/size) accumulates duplicate lines and entries for since-deleted files are never shed. On startup mkvclean now atomically rewrites it to one last-wins line per path (collapsing duplicates) and prunes entries whose file no longer exists. Pruning is scoped to paths *under the scanned root* — which is already verified to be a live directory — so an unmounted or unrelated root can never wipe good entries; out-of-root paths are left untouched. The rewrite is skipped when nothing changed (no duplicates, nothing pruned). Pruning is on by default with a `--no-prune-checkpoint` opt-out (duplicate collapsing always runs). The rewrite is atomic and durable (`tempfile.mkstemp` in the same dir + `fsync` + `os.replace`, temp cleaned up on any failure); compaction also runs under `--dry-run` since it's checkpoint maintenance, not a media modification.
- Extra cleanup passes (both scripts): title, tags, attachments and language normalization, controlled by a new `Cleanup` namedtuple. **Strip global title** — now removed on the remux path too (previously only on the no-remux fast-path). **Wipe tags** — `--no-global-tags`/`--no-track-tags` on remux, `--tags all:` via `mkvpropedit` on the fast-path (only when tags actually exist). **Clear junk track names** — track names matching the commentary/description/director/DVS/SDH patterns are cleared on kept tracks (`--track-name TID:` / `--delete name`); `"forced"` is deliberately excluded so a name-only forced marker is never lost. **Fill undefined language from name** — `apply_language_inference()` maps a language word in a track's name (e.g. "English") to an ISO 639-2 code and sets it on `und`/missing-language tracks; it runs *before* `select_tracks()` so the corrected language feeds the existing filter. **Drop attachments** — opt-in removal of cover art/embedded fonts (`--no-attachments` on remux, `--delete-attachment =<uid>` on the fast-path). The fast-path planner (`plan_metadata_fixes()`) and a new `plan_remux_cleanup_args()` apply these on the no-remux and remux paths respectively. Configurable on mkvclean via `--strip-attachments` (opt-in) and `--keep-title`/`--keep-tags`/`--keep-track-names`/`--no-infer-lang` opt-outs; on `mkv_strip_pp.py` via the `STRIP_TITLE`/`STRIP_TAGS`/`INFER_LANGUAGE`/`CLEAR_JUNK_TRACK_NAMES`/`STRIP_ATTACHMENTS` constants. Title/tags/language-fill/junk-name passes default on; attachment removal defaults off (fonts can matter for styled subtitles).
- mkvpropedit fast-path for metadata-only fixes (both scripts): when `select_tracks()` finds no tracks to strip, a new `plan_metadata_fixes()` helper inspects the header and `fix_metadata_in_place()` applies any needed edits with `mkvpropedit` instead of a full remux. It enforces exactly one default-audio flag (set on the first kept audio track, cleared on the rest) and deletes a stray global container title. The edit is done in place on the existing inode, so there is no temp file, atomic swap, or disk-space check, and ownership/mode/ACLs are preserved inherently. This produces a new `"fixed"` result that is checkpointed and counted like a strip. The binary is resolved alongside `mkvmerge` (`--mkvpropedit` on mkvclean, `MKVPROPEDIT` constant in `mkv_strip_pp.py`); the remux path is unchanged.
- Remux verification before the atomic swap (both scripts): a new `verify_remux()` helper re-probes the temp output with `mkvmerge -J` and confirms it still has a video track and exactly the audio/subtitle track counts that were requested before `os.replace()` overwrites the original. If the re-probe fails or the counts don't match, the original is kept and an error is logged. Guards against a truncated-but-nonzero output silently replacing a good source.
- `Dockerfile` extending the LinuxServer.io SABnzbd image (`lscr.io/linuxserver/sabnzbd`) with `mkvtoolnix` (for `mkvmerge`) and `acl`, so `mkv_strip_pp.py` can run as a post-processor with all dependencies baked in.

### Changed
- Opt-in English audio de-duplication in `select_tracks()` (**mkvclean.py only**), via a new `--prefer-audio-channels N` flag. When the flag is set and more than one English audio track survives the language/junk filter, only the single best one is kept and the rest are stripped on the remux path: a track whose channel count equals `N` wins outright, otherwise the track with the most channels (`audio_channels`) wins, ties broken by file order (`_audio_priority()` supplies the key). Passing `0` keeps the most channels with no exact-count preference. Omitting the flag (the default) keeps all English tracks — nothing is de-duplicated. Only English is ever collapsed; other languages keep all matching tracks and single-audio-track files are never touched. The SABnzbd post-processor `mkv_strip_pp.py` does **not** expose this and always keeps every English track.
- Renamed the library-sweep script `mkvclean` → `mkvclean.py` for consistency with `mkv_strip_pp.py` and clearer file-type association. Invocations in the docs are updated accordingly (`./mkvclean.py …`); the conceptual tool name, logger (`mkvcleaner`), and default artifact paths (`~/.mkvclean_checkpoint`, `~/mkvclean.log`, `/tmp/mkvclean.lock`) are unchanged.
- Flag-based junk detection in `select_tracks()` (both scripts): junk audio is now detected via the explicit Matroska flags `flag_commentary` and `flag_visual_impaired` first, with the existing commentary/description/director/DVS track-name substring match kept as a fallback. This catches untitled or non-English commentary and audio-description tracks that the name match alone missed. Subtitle selection now also drops SDH / hearing-impaired tracks via `flag_hearing_impaired` (with an `"sdh"` track-name fallback); forced subtitles are still always kept regardless of language or SDH status.
- Default audio track enforcement (both scripts): the first kept audio track is set as the sole default and the default flag is now explicitly cleared on every other kept audio track, so a stale default flag carried over from the source can no longer leave two default audio tracks.
- Harden metadata preservation on the atomic swap (both scripts): a new `preserve_metadata()` helper now also preserves timestamps, extended attributes (`shutil.copystat`), and POSIX ACLs (`getfacl`/`setfacl`, best-effort), in addition to mode and ownership. When ownership can't be restored (e.g. running as a non-root user), it now logs a WARNING and continues instead of failing silently.

### Fixed
- **mkv_strip_pp.py**: `select_tracks()` now strips junk audio (commentary/description/director/DVS by track name), matching `mkvclean`. Previously the SABnzbd post-processor kept those tracks if their language matched.
- Forced-subtitle detection (both scripts): replaced the bogus `visual_impermanence` property — which `mkvmerge -J` never emits, so it was always `None` and did nothing — with a `"forced"` track-name substring check alongside the `forced_track` flag. Forced subtitles named "Forced" without the flag set are now kept. Corrected the term in `CLAUDE.md` rule 4.

## [1.0.0] - 2026-06-08

### Added
- **mkv_strip_pp.py**: SABnzbd post-processing script for cleaning MKVs before Radarr import
  - Reads job directory from `SAB_COMPLETE_DIR` or command line argument
  - Skips processing on failed downloads via `SAB_PP_STATUS` check
  - Always exits 0 to avoid blocking automation pipeline
- **mkvclean**: Library sweep engine for batch processing existing media
  - JSON-lines checkpoint file for tracking processed files by path/size/mtime
  - Kernel-level file locking (`fcntl`) to prevent overlapping runs
  - `os.scandir()` for fast directory traversal on large arrays
  - Configurable batch size with `--batch` flag
  - Signal handling (SIGTERM) for graceful interruption
- Language-based audio filtering with configurable preferences (default: eng, jpn, und)
- Language-based subtitle filtering (default: eng, und)
- Dynamic undetermined track handling: drops `und` audio when English audio exists
- Automatic preservation of forced subtitle tracks
- Junk track detection: strips commentary, director's notes, descriptive audio, DVS tracks
- Atomic file replacement using `os.replace()` to prevent corruption
- Pre-flight disk space checks (requires 105% of original file size)
- POSIX permission and ownership preservation via `os.chmod()`/`os.chown()`
- Default track flag enforcement on first kept audio track
- Surrogateescape path handling for files with broken UTF-8 characters
- Dry-run mode for both scripts
