#!/usr/bin/env python3
"""
mkv_strip_pp.py - Hardened SABnzbd post-processing hook.

Strips unwanted audio/subtitle tracks from a freshly downloaded movie BEFORE
Radarr imports it. The cleaned file keeps its original name via an atomic 
in-place swap on the same filesystem.

Features:
- Atomic file replacement
- Free space pre-flight checks
- POSIX permission, ownership, timestamp and ACL healing
- Default track enforcement
- mkvpropedit fast-path for header-only fixes (default-audio flag, global title)
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple

# === configuration ===
AUDIO_LANGS = ["eng", "jpn", "und"]
SUB_LANGS = ["eng", "und"]
LOG_FILE = "/config/mkv_strip_pp.log"
DRY_RUN = False
MKVMERGE = "mkvmerge"
MKVPROPEDIT = "mkvpropedit"

# Extra cleanup passes. Title/tags/language fill and junk-name clearing default
# on; attachment removal is opt-in (embedded fonts can matter for styled subs).
STRIP_TITLE = True
STRIP_TAGS = True
INFER_LANGUAGE = True
CLEAR_JUNK_TRACK_NAMES = True
STRIP_ATTACHMENTS = False

log = logging.getLogger("mkvclean")

# Track-name substrings that mark junk audio (commentary / descriptive / DVS).
JUNK_AUDIO_NAME_PATTERNS = ("commentary", "description", "director", "dvs")
# Same set plus SDH, used by the cosmetic track-name cleanup pass.
JUNK_NAME_PATTERNS = JUNK_AUDIO_NAME_PATTERNS + ("sdh",)

# Language words that may appear in a track name -> ISO 639-2 code, used to fill
# an undefined ('und') track language so the language filter can act on it.
LANG_NAME_MAP = {
    "english": "eng",
    "japanese": "jpn",
    "spanish": "spa", "espanol": "spa", "español": "spa",
    "french": "fre", "francais": "fre", "français": "fre",
    "german": "ger", "deutsch": "ger",
    "italian": "ita", "italiano": "ita",
    "portuguese": "por", "portugues": "por", "português": "por",
    "russian": "rus",
    "chinese": "chi", "mandarin": "chi", "cantonese": "chi",
    "korean": "kor",
    "dutch": "dut", "nederlands": "dut",
    "hindi": "hin",
    "arabic": "ara",
}

Cleanup = namedtuple("Cleanup", "title tags infer_lang track_names attachments")
DEFAULT_CLEANUP = Cleanup(title=True, tags=True, infer_lang=True, track_names=True, attachments=False)
CLEANUP = Cleanup(
    title=STRIP_TITLE,
    tags=STRIP_TAGS,
    infer_lang=INFER_LANGUAGE,
    track_names=CLEAR_JUNK_TRACK_NAMES,
    attachments=STRIP_ATTACHMENTS,
)

def probe(path, mkvmerge="mkvmerge"):
    out = subprocess.run(
        [mkvmerge, "-J", path], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)

def select_tracks(info, audio_langs, sub_langs):
    tracks = info.get("tracks", [])
    audio = [t for t in tracks if t.get("type") == "audio"]
    subs = [t for t in tracks if t.get("type") == "subtitles"]
    all_audio = [t["id"] for t in audio]
    all_subs = [t["id"] for t in subs]

    if len(audio) <= 1:
        keep_audio = list(all_audio)
    else:
        has_eng_audio = any(t.get("properties", {}).get("language") == "eng" for t in audio)
        target_audio_langs = list(audio_langs)
        if has_eng_audio and "und" in target_audio_langs:
            target_audio_langs.remove("und")

        keep_audio = []
        for t in audio:
            props = t.get("properties", {})
            lang = props.get("language")
            track_name = str(props.get("track_name", "")).lower()

            # Junk audio: prefer the explicit Matroska flags (these catch untitled
            # or non-English commentary / audio-description tracks that the name
            # match misses); fall back to the track-name substring check.
            is_junk = (
                props.get("flag_commentary")
                or props.get("flag_visual_impaired")
                or any(x in track_name for x in JUNK_AUDIO_NAME_PATTERNS)
            )

            if lang in target_audio_langs and not is_junk:
                keep_audio.append(t["id"])

        if not keep_audio:
            keep_audio = list(all_audio)

    keep_subs = []
    for t in subs:
        props = t.get("properties", {})
        lang = props.get("language")
        track_name = str(props.get("track_name", "")).lower()
        is_forced = props.get("forced_track") or "forced" in track_name
        is_sdh = props.get("flag_hearing_impaired") or "sdh" in track_name

        # Forced subs are always kept. Otherwise keep preferred languages but drop
        # SDH / hearing-impaired tracks (flag preferred, "sdh" name as fallback).
        if is_forced:
            keep_subs.append(t["id"])
        elif lang in sub_langs and not is_sdh:
            keep_subs.append(t["id"])

    nothing = (set(keep_audio) == set(all_audio) and set(keep_subs) == set(all_subs))
    return keep_audio, keep_subs, all_audio, all_subs, nothing

def infer_language(track_name):
    """Return the ISO 639-2 code named in a track title (whole-word match), or None."""
    name = str(track_name or "").lower()
    if not name:
        return None
    for word, code in LANG_NAME_MAP.items():
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", name):
            return code
    return None

def _is_junk_name(track_name):
    """True if a track name looks like commentary / descriptive / SDH junk."""
    name = str(track_name or "").lower()
    return any(p in name for p in JUNK_NAME_PATTERNS)

def apply_language_inference(info):
    """Fill an undefined ('und'/missing) track language from the language named in
    its track name, mutating `info` so select_tracks() sees the corrected value.
    Returns a {track_id: code} map of the tracks that were changed."""
    fixes = {}
    for t in info.get("tracks", []):
        props = t.setdefault("properties", {})
        lang = props.get("language")
        if lang and lang != "und":
            continue
        code = infer_language(props.get("track_name", ""))
        if code:
            props["language"] = code
            fixes[t["id"]] = code
    return fixes

def _track_selectors(tracks):
    """Map each track id to its mkvpropedit per-type selector (v1/a1/s2/...)."""
    code = {"video": "v", "audio": "a", "subtitles": "s"}
    counts, sel = {}, {}
    for t in tracks:
        c = code.get(t.get("type"))
        if not c:
            continue
        counts[c] = counts.get(c, 0) + 1
        sel[t["id"]] = f"{c}{counts[c]}"
    return sel

def plan_metadata_fixes(info, keep_audio, lang_fixes, cleanup):
    """Header-only edits a kept-as-is file still needs, as mkvpropedit argument
    groups (empty if the header is already clean). Enforces exactly one
    default-audio flag (first kept track), fills inferred languages, and runs the
    cleanup passes: strip the global title, wipe tags, clear junk track names and
    drop attachments. Used only when no tracks are stripped, so the per-type track
    ordinals (track:aN/sN) line up with the file's track order."""
    edits = []
    tracks = info.get("tracks", [])
    audio = [t for t in tracks if t.get("type") == "audio"]
    sel = _track_selectors(tracks)

    for n, t in enumerate(audio, start=1):
        is_default = bool(t.get("properties", {}).get("default_track"))
        want_default = bool(keep_audio) and t["id"] == keep_audio[0]
        if is_default != want_default:
            edits.append(["--edit", f"track:a{n}",
                          "--set", f"flag-default={1 if want_default else 0}"])

    # Persist languages inferred from the track name onto 'und' tracks.
    for tid, code in lang_fixes.items():
        edits.append(["--edit", f"track:{sel[tid]}", "--set", f"language={code}"])

    # Clear junk track names (commentary / SDH / etc) on the kept tracks.
    if cleanup.track_names:
        for t in tracks:
            if _is_junk_name(t.get("properties", {}).get("track_name", "")):
                edits.append(["--edit", f"track:{sel[t['id']]}", "--delete", "name"])

    # Strip the global container title (usually the release-group filename).
    if cleanup.title and info.get("container", {}).get("properties", {}).get("title"):
        edits.append(["--edit", "info", "--delete", "title"])

    # Wipe all tags (global + per-track) in one shot, only if any exist.
    if cleanup.tags and (info.get("global_tags") or info.get("track_tags")):
        edits.append(["--tags", "all:"])

    # Drop attachments (cover art / fonts), addressed by UID so deletes don't
    # depend on positional re-indexing within the single mkvpropedit call.
    if cleanup.attachments:
        for att in info.get("attachments", []):
            uid = att.get("properties", {}).get("uid")
            if uid is not None:
                edits.append(["--delete-attachment", f"={uid}"])

    return edits

def plan_remux_cleanup_args(info, keep_ids, lang_fixes, cleanup):
    """Extra mkvmerge args applying the cleanup passes during a full remux: strip
    the global title, drop tags/attachments, clear junk track names and persist
    inferred languages. Only touches tracks in keep_ids (those that survive)."""
    args = []
    if cleanup.title:
        args += ["--title", ""]
    if cleanup.tags:
        args += ["--no-global-tags", "--no-track-tags"]
    if cleanup.attachments:
        args += ["--no-attachments"]
    if cleanup.track_names:
        for t in info.get("tracks", []):
            if t["id"] in keep_ids and _is_junk_name(t.get("properties", {}).get("track_name", "")):
                args += ["--track-name", f"{t['id']}:"]
    for tid, code in lang_fixes.items():
        if tid in keep_ids:
            args += ["--language", f"{tid}:{code}"]
    return args

def _copy_acl(src, dst):
    """Best-effort copy of POSIX ACLs (requires the 'acl' package: getfacl/setfacl)."""
    getfacl = shutil.which("getfacl")
    setfacl = shutil.which("setfacl")
    if not (getfacl and setfacl):
        return
    try:
        dump = subprocess.run([getfacl, "-c", "--", src], capture_output=True, text=True)
        if dump.returncode != 0 or not dump.stdout.strip():
            return
        subprocess.run([setfacl, "--set-file=-", "--", dst], input=dump.stdout,
                       capture_output=True, text=True)
    except OSError as e:
        log.debug("  ACL copy skipped on %s: %s", os.path.basename(dst), e)

def preserve_metadata(src, dst, src_stat=None):
    """Heal ownership, mode, timestamps, xattrs and ACLs from src onto dst.
    Best-effort: logs a warning if ownership can't be preserved (non-root)."""
    st = src_stat or os.stat(src)
    # Ownership first: chown can clear setuid/setgid bits, so do it before mode.
    try:
        os.chown(dst, st.st_uid, st.st_gid)
    except OSError as e:
        log.warning("  Could not preserve ownership (uid=%s gid=%s) on %s: %s",
                    st.st_uid, st.st_gid, os.path.basename(dst), e)
    # Mode + timestamps (+ flags/xattrs where supported).
    try:
        shutil.copystat(src, dst)
    except OSError as e:
        log.warning("  Could not fully preserve mode/timestamps on %s: %s",
                    os.path.basename(dst), e)
    # POSIX ACLs (after copystat, since setfacl rewrites the base mode entries).
    _copy_acl(src, dst)

def verify_remux(tmp, keep_audio, keep_subs, mkvmerge="mkvmerge"):
    """Re-probe the remuxed temp file and confirm it carries the tracks we asked
    mkvmerge to keep. mkvmerge renumbers output track IDs, so we compare counts
    by type rather than the source IDs. Guards against a truncated-but-nonzero
    output silently overwriting the original."""
    try:
        info = probe(tmp, mkvmerge)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as e:
        log.error("  Output failed verification (could not re-probe): %s", e)
        return False

    tracks = info.get("tracks", [])
    n_video = sum(1 for t in tracks if t.get("type") == "video")
    n_audio = sum(1 for t in tracks if t.get("type") == "audio")
    n_subs = sum(1 for t in tracks if t.get("type") == "subtitles")

    if n_video < 1:
        log.error("  Output failed verification: no video track present.")
        return False
    if n_audio != len(keep_audio):
        log.error("  Output failed verification: expected %d audio track(s), output has %d.",
                  len(keep_audio), n_audio)
        return False
    if n_subs != len(keep_subs):
        log.error("  Output failed verification: expected %d subtitle track(s), output has %d.",
                  len(keep_subs), n_subs)
        return False
    return True

def strip_in_place(path, keep_audio, keep_subs, mkvmerge="mkvmerge", extra_args=None):
    folder = os.path.dirname(path)
    if not os.access(folder, os.W_OK):
        log.error("  Directory not writeable: %s", folder)
        return "error"

    orig_size = os.path.getsize(path)
    free_space = shutil.disk_usage(folder).free
    if free_space < (orig_size * 1.05):
        log.error("  Insufficient disk space. Requires %d bytes, has %d.", int(orig_size * 1.05), free_space)
        return "error"

    orig_stat = os.stat(path)
    fd, tmp = tempfile.mkstemp(suffix=".mkv", prefix=".mkvclean_", dir=folder)
    os.close(fd)
    
    try:
        cmd = [mkvmerge, "-o", tmp]
        
        if keep_audio:
            cmd += ["--audio-tracks", ",".join(map(str, keep_audio))]
            # Exactly one default audio track: the first kept track; clear the rest
            # so a stale default flag on another kept track can't win.
            for i, tid in enumerate(keep_audio):
                cmd += ["--default-track-flag", f"{tid}:{1 if i == 0 else 0}"]
        else:
            cmd += ["--no-audio"]
            
        if keep_subs:
            cmd += ["--subtitle-tracks", ",".join(map(str, keep_subs))]
            cmd += ["--default-track-flag", f"{keep_subs[0]}:0"]
        else:
            cmd += ["--no-subtitles"]

        # Cleanup passes (title/tags/attachments/track-names/language). These are
        # global or source-track options, so they must precede the input file.
        if extra_args:
            cmd += extra_args

        cmd += [path]

        result = subprocess.run(cmd, capture_output=True, text=True)
        ok = (result.returncode in (0, 1) and os.path.exists(tmp) and os.path.getsize(tmp) > 0)

        if not ok:
            last = (result.stderr.strip().splitlines() or [""])[-1]
            log.error("  mkvmerge exit %s; original kept. %s", result.returncode, last)
            return "error"

        # Verify the remux before clobbering the original; a bad output keeps the source.
        if not verify_remux(tmp, keep_audio, keep_subs, mkvmerge):
            log.error("  Original kept: %s", os.path.basename(path))
            return "error"

        preserve_metadata(path, tmp, orig_stat)
        os.replace(tmp, path)
        return "stripped"
        
    except Exception as e:
        log.error("  Failed during remux processing: %s", e)
        return "error"
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

def fix_metadata_in_place(path, edits, mkvpropedit="mkvpropedit"):
    """Apply header-only edits with mkvpropedit. It rewrites the existing file in
    place (same inode), so ownership, mode and ACLs are preserved and there is no
    temp file or disk-space check. `edits` is a list of mkvpropedit argument
    groups as returned by plan_metadata_fixes()."""
    if not os.access(path, os.W_OK):
        log.error("  File not writeable: %s", path)
        return False

    cmd = [mkvpropedit, path]
    for group in edits:
        cmd += group

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        log.error("  mkvpropedit failed to run: %s", e)
        return False

    # mkvpropedit exit codes: 0 success, 1 warnings (edits still applied), 2 error.
    if result.returncode >= 2:
        last = (result.stderr.strip().splitlines() or [""])[-1]
        log.error("  mkvpropedit exit %s; file unchanged. %s", result.returncode, last)
        return False
    return True

def process_file(path, audio_langs, sub_langs, mkvmerge="mkvmerge", mkvpropedit="mkvpropedit", dry_run=False, cleanup=DEFAULT_CLEANUP):
    try:
        info = probe(path, mkvmerge)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as e:
        log.error("  Could not probe %s: %s", os.path.basename(path), e)
        return "error"

    # Fill undefined languages from the track name first, so selection sees them.
    lang_fixes = apply_language_inference(info) if cleanup.infer_lang else {}

    keep_audio, keep_subs, all_audio, all_subs, nothing = select_tracks(info, audio_langs, sub_langs)
    if nothing:
        # No tracks to strip, but the header may still need work: a wrong
        # default-audio flag, a stray title/tags, junk track names, inferred
        # languages or attachments to drop. Fix those in place with mkvpropedit
        # (no remux, no temp file, no disk-space check) rather than rewriting it.
        edits = plan_metadata_fixes(info, keep_audio, lang_fixes, cleanup)
        if not edits:
            return "nothing"
        log.info("  Metadata-only fix, %d edit(s) without remux: %s",
                 len(edits), os.path.basename(path))
        if dry_run:
            log.info("    DRY RUN: no change made.")
            return "nothing"
        return "fixed" if fix_metadata_in_place(path, edits, mkvpropedit) else "error"

    removed_a = len(all_audio) - len(keep_audio)
    removed_s = len(all_subs) - len(keep_subs)
    log.info("  Stripping %d audio / %d subtitle track(s): %s", removed_a, removed_s, os.path.basename(path))

    if dry_run:
        log.info("    DRY RUN: no change made.")
        return "nothing"

    video_ids = [t["id"] for t in info.get("tracks", []) if t.get("type") == "video"]
    keep_ids = set(keep_audio) | set(keep_subs) | set(video_ids)
    extra_args = plan_remux_cleanup_args(info, keep_ids, lang_fixes, cleanup)
    return strip_in_place(path, keep_audio, keep_subs, mkvmerge, extra_args)

def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)

def get_job_dir():
    d = os.environ.get("SAB_COMPLETE_DIR")
    if not d and len(sys.argv) > 1 and sys.argv[1]:
        d = sys.argv[1]
    return d

def download_failed():
    status = os.environ.get("SAB_PP_STATUS")
    if status is None and len(sys.argv) > 7:
        status = sys.argv[7]
    if status is None:
        return False
    try:
        return int(status) != 0
    except ValueError:
        return False

def find_mkvs(root):
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith(".mkv"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)

def main():
    setup_logging()
    log.info("=" * 40)
    log.info("mkv_strip_pp starting")

    resolved_mkvmerge = shutil.which(MKVMERGE) or (MKVMERGE if os.path.isfile(MKVMERGE) else None)
    if not resolved_mkvmerge:
        log.error("mkvmerge binary path not found. Ensure MKVToolNix is installed.")
        return 1

    resolved_mkvpropedit = shutil.which(MKVPROPEDIT) or (MKVPROPEDIT if os.path.isfile(MKVPROPEDIT) else None)
    if not resolved_mkvpropedit:
        log.error("mkvpropedit binary path not found. Ensure MKVToolNix is installed.")
        return 1

    job_dir = get_job_dir()
    if not job_dir or not os.path.isdir(job_dir):
        log.error("Job directory not found or not provided: %r", job_dir)
        return 1

    if download_failed():
        log.info("Download marked failed by SABnzbd; skipping. Exiting 0.")
        return 0

    log.info("Job folder: %s", job_dir)
    mkvs = find_mkvs(job_dir)
    if not mkvs:
        log.info("No .mkv files found. Nothing to do.")
        return 0

    log.info("Found %d MKV file(s).", len(mkvs))
    for path in mkvs:
        log.info("Inspecting: %s", os.path.basename(path))
        try:
            result = process_file(path, AUDIO_LANGS, SUB_LANGS, resolved_mkvmerge, resolved_mkvpropedit, DRY_RUN, CLEANUP)
            if result == "nothing":
                log.info("  Nothing to strip, leaving file as-is.")
            elif result == "stripped":
                log.info("  Done.")
            elif result == "fixed":
                log.info("  Metadata fixed in place (no remux).")
        except Exception as e:
            log.error("Unexpected error on %s: %s. Original kept.", os.path.basename(path), e)

    log.info("Finished. Exiting 0 so import proceeds.")
    return 0

if __name__ == "__main__":
    sys.exit(main())