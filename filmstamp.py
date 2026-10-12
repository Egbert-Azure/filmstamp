#!/usr/bin/env python3
"""filmstamp — pre-import metadata stamper for afilm roll exports."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader
from timezonefinder import TimezoneFinder

NAS_BLOCK_MARKER = "/Photography/RAW Files/"
DEFAULT_EXTENSIONS = (".tif", ".tiff")
EXIT_ARCHIVE_FAILED = 3  # stamping verified; only the archive copy of the export failed
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


class FilmstampError(Exception):
    """A hard-stop condition: the tool refuses to guess."""


def _require(d: dict, key: str, context: str) -> object:
    """Same hard-stop discipline as every other malformed-field case: never a bare KeyError."""
    try:
        return d[key]
    except KeyError:
        raise FilmstampError(f"Malformed export: missing required field {key!r} {context}.") from None


# ---------------------------------------------------------------------------
# Normalisation (H3/H4)
# ---------------------------------------------------------------------------

def normalize_gear(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip()).casefold()


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def load_profile(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def resolve_camera(raw_name: str, profile: dict) -> tuple[str, dict]:
    key = normalize_gear(raw_name)
    for cam_key, cam in profile.get("cameras", {}).items():
        if normalize_gear(cam_key) == key:
            return cam_key, cam
    raise FilmstampError(
        f"Unresolved camera {raw_name!r} (normalised {key!r}). "
        f'Add [cameras."{key}"] to the profile.'
    )


def resolve_lens(raw_name: str, camera: dict, profile: dict) -> tuple[str, dict, bool]:
    """Returns (lens_key, lens_dict, used_fallback)."""
    lenses = profile.get("lenses", {})
    normalized = normalize_gear(raw_name)

    if normalized == "":
        lookup_key = camera.get("default_lens")
        if not lookup_key or lookup_key not in lenses:
            raise FilmstampError(
                "Empty lensName and camera has no valid default_lens in profile."
            )
        # A fixed-lens body (Ikonta) always logs an empty lensName: the default is
        # the lens, not a fallback, so it must not raise LENS-FALLBACK.
        used_fallback = not camera.get("fixed_lens", False)
    else:
        used_fallback = False
        lookup_key = next(
            (k for k in lenses if normalize_gear(k) == normalized), None
        )
        if lookup_key is None:
            raise FilmstampError(
                f"Unresolved lens {raw_name!r} (normalised {normalized!r}). "
                f'Add [lenses."{normalized}"] to the profile — never guessed.'
            )

    entry = lenses[lookup_key]
    seen = {lookup_key}
    while "alias_of" in entry:
        target = entry["alias_of"]
        if target in seen or target not in lenses:
            raise FilmstampError(
                f"Broken lens alias chain starting at {lookup_key!r} -> {target!r}."
            )
        seen.add(target)
        lookup_key = target
        entry = lenses[target]

    return lookup_key, entry, used_fallback


def concrete_lenses(profile: dict) -> dict[str, dict]:
    """Lens entries that are real lenses (not aliases), key -> entry."""
    return {k: v for k, v in profile.get("lenses", {}).items() if "alias_of" not in v}


def parse_lens_specs(
    specs: list[str], sources: list, camera: dict, profile: dict
) -> dict[int, str]:
    """--lens values -> {frame number: lens key}. Only frames whose afilm lens name is
    empty can be given a lens; naming a frame that already has one is an error."""
    if not specs:
        return {}
    blank = sorted(s.number for s in sources if normalize_gear(s.lens_name) == "")
    chosen: dict[int, str] = {}
    for spec in specs:
        frames_part, sep, key = spec.rpartition("=")
        key = key.strip()
        try:
            resolved_key, _, _ = resolve_lens(key, camera, profile)
        except FilmstampError:
            raise FilmstampError(
                f"--lens {key!r} is not a lens in the profile. Known: "
                f"{', '.join(concrete_lenses(profile))}."
            ) from None
        if not sep:
            numbers = blank
        else:
            try:
                numbers = [int(n) for n in frames_part.split(",") if n.strip()]
            except ValueError:
                raise FilmstampError(f"--lens {spec!r}: frames must be numbers like 5,6,9.") from None
            wrong = [n for n in numbers if n not in blank]
            if wrong:
                raise FilmstampError(
                    f"--lens {spec!r}: frame(s) {wrong} already have a lens in the afilm log "
                    "(or are not logged); only frames with no recorded lens can be given one."
                )
        for n in numbers:
            chosen[n] = resolved_key
    return chosen


def resolve_film_stock(roll_name: str, profile: dict) -> tuple[dict | None, bool]:
    """Longest case-insensitive alias match against the roll name. Returns (stock, warned)."""
    haystack = normalize_gear(roll_name)
    candidates = sorted(
        profile.get("film_stocks", {}).items(), key=lambda kv: len(kv[0]), reverse=True
    )
    for alias, stock in candidates:
        if alias.casefold() in haystack:
            return stock, False
    return None, True


def parse_roll_number(name: str) -> str | None:
    match = re.match(r"^R(\d+)", name.strip())
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Field parsing
# ---------------------------------------------------------------------------

def parse_aperture(raw: str) -> float:
    if not raw.startswith("f/"):
        raise FilmstampError(f"Malformed aperture {raw!r}: expected 'f/<number>'.")
    try:
        return float(raw[2:])
    except ValueError as exc:
        raise FilmstampError(f"Malformed aperture {raw!r}: expected 'f/<number>'.") from exc


_FRACTION_RE = re.compile(r"^\d+/\d+$")
_BARE_INT_RE = re.compile(r"^\d+$")


def parse_shutter(raw: str) -> str:
    if _FRACTION_RE.match(raw):
        return raw
    if "." in raw:
        try:
            float(raw)
            return raw
        except ValueError:
            pass
    if _BARE_INT_RE.match(raw):
        # See O6: afilm's long-exposure format is unconfirmed, so a bare integer
        # is rejected rather than guessed as seconds.
        raise FilmstampError(
            f"Malformed shutterSpeed {raw!r}: bare integer, may be a seconds value — "
            "confirm the afilm format and extend this rule."
        )
    raise FilmstampError(
        f"Malformed shutterSpeed {raw!r}: expected 'N/M' or a decimal seconds value."
    )


def parse_iso(iso: int, pull_push: str) -> int:
    try:
        stops = int(pull_push)
    except ValueError as exc:
        raise FilmstampError(f"Malformed pullPush {pull_push!r}: expected an integer string.") from exc
    return round(iso * (2 ** stops))


def gps_tags(location: tuple[float, float] | None) -> dict | None:
    if location is None:
        return None
    lat, lon = location
    return {
        "lat": lat,
        "lat_ref": "N" if lat >= 0 else "S",
        "lon": lon,
        "lon_ref": "E" if lon >= 0 else "W",
    }


# ---------------------------------------------------------------------------
# Frame source data
# ---------------------------------------------------------------------------

@dataclass
class FrameSource:
    number: int
    lens_name: str
    aperture: str
    shutter_speed: str
    date_local: datetime
    note: str
    location: tuple[float, float] | None


def load_frames(roll: dict) -> list[FrameSource]:
    sources = []
    for key, f in roll.get("frames", {}).items():
        loc = f.get("location")
        location = (loc["latitude"], loc["longitude"]) if loc else None
        raw_date = _require(f, "date", f"on frame {key}")
        try:
            date_local = datetime.fromisoformat(raw_date)
        except ValueError as exc:
            raise FilmstampError(f"Malformed date {raw_date!r} on frame {key}.") from exc
        sources.append(
            FrameSource(
                number=int(key),
                lens_name=f.get("lensName", ""),
                aperture=_require(f, "aperture", f"on frame {key}"),
                shutter_speed=_require(f, "shutterSpeed", f"on frame {key}"),
                date_local=date_local,
                note=f.get("note", ""),
                location=location,
            )
        )
    sources.sort(key=lambda fr: fr.number)
    return sources


# ---------------------------------------------------------------------------
# Timezone resolution
# ---------------------------------------------------------------------------

METHOD_LABELS = {
    "tz-override": "--tz override",
    "derived": "derived from GPS",
    "profile-default": "profile default",
}


@dataclass
class DayZone:
    zone: ZoneInfo
    zone_name: str
    derived: bool
    mismatch: bool
    method: str  # one of METHOD_LABELS


_timezone_finder: TimezoneFinder | None = None


def _tf() -> TimezoneFinder:
    global _timezone_finder
    if _timezone_finder is None:
        _timezone_finder = TimezoneFinder()
    return _timezone_finder


def resolve_day_zones(
    frames: list[FrameSource], profile: dict, tz_override: str | None
) -> tuple[dict[date, DayZone], list[str]]:
    warnings: list[str] = []
    fallback_name = profile["time"]["timezone"]

    if tz_override:
        zone = ZoneInfo(tz_override)
        result = DayZone(
            zone=zone, zone_name=tz_override, derived=False, mismatch=False, method="tz-override"
        )
        return {fr.date_local.date(): result for fr in frames}, warnings

    derive_enabled = profile["time"].get("derive_timezone_from_gps", True)

    by_day: dict[date, list[FrameSource]] = {}
    for fr in frames:
        by_day.setdefault(fr.date_local.date(), []).append(fr)

    results: dict[date, DayZone] = {}
    for day, day_frames in sorted(by_day.items()):
        zone_name = fallback_name
        derived = False
        if derive_enabled:
            for fr in sorted(day_frames, key=lambda f: f.date_local):
                if fr.location is not None:
                    found = _tf().timezone_at(lat=fr.location[0], lng=fr.location[1])
                    if found:
                        zone_name = found
                        derived = True
                    break
        mismatch = derived and zone_name != fallback_name
        if mismatch:
            warnings.append(
                f"{day.isoformat()}: timezone derived as {zone_name} "
                f"(profile default is {fallback_name})"
            )
        method = "derived" if derived else "profile-default"
        results[day] = DayZone(
            zone=ZoneInfo(zone_name), zone_name=zone_name, derived=derived, mismatch=mismatch, method=method
        )
    return results, warnings


def format_capture_time(dt_local: datetime, zone: ZoneInfo) -> tuple[str, str, str]:
    aware = dt_local.replace(tzinfo=zone)
    offset_minutes = int(aware.utcoffset().total_seconds() // 60)
    sign = "+" if offset_minutes >= 0 else "-"
    offset_minutes = abs(offset_minutes)
    offset_str = f"{sign}{offset_minutes // 60:02d}:{offset_minutes % 60:02d}"
    stamp = dt_local.strftime("%Y:%m:%d %H:%M:%S")
    subsec = f"{dt_local.microsecond // 1000:03d}"
    return stamp, offset_str, subsec


# ---------------------------------------------------------------------------
# Frame <-> file mapping
# ---------------------------------------------------------------------------

_FRAME_NUM_RE = re.compile(r"(\d+)(?!.*\d)")


def parse_frame_number(filename: str) -> int | None:
    match = _FRAME_NUM_RE.search(Path(filename).stem)
    return int(match.group(1)) if match else None


def load_map_file(path: Path) -> dict[str, int]:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return {k: int(v) for k, v in data.items()}


def build_mapping(
    scan_dir: Path,
    frame_numbers: list[int],
    offset: int,
    map_path: Path | None,
    extensions: tuple[str, ...],
    include_glob: str | None = None,
) -> tuple[dict[int, list[Path]], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    all_files = sorted(
        p for p in scan_dir.iterdir() if p.is_file() and p.suffix.lower() in extensions
    )
    if include_glob:
        included = [p for p in all_files if fnmatch.fnmatch(p.name, include_glob)]
        if included:
            files = included
        else:
            files = all_files
            if all_files:
                warnings.append(
                    f"--include {include_glob!r} matched no files; falling back to all "
                    f"{len(all_files)} file(s) matching --ext."
                )
    else:
        files = all_files
    overrides = load_map_file(map_path) if map_path else {}

    # A file named in --map explains away the gap at its *natural* (un-overridden)
    # frame number — e.g. overriding "…004-positive.tif" to frame 5 explains why
    # frame 4 has no file of its own. A --map used for something unrelated must not
    # suppress a genuine gap elsewhere in the roll.
    explained_gaps: set[int] = set()
    for filename, dest_frame in overrides.items():
        natural = parse_frame_number(filename)
        if natural is not None and natural + offset != dest_frame:
            explained_gaps.add(natural + offset)

    # A frame maps to every file whose parsed frame number matches, not to one — the
    # source scan and the converted positive are the same exposure and both get
    # identical metadata. Multiple files per frame is normal, not an error.
    mapping: dict[int, list[Path]] = {}
    for f in files:
        if f.name in overrides:
            frame_no = overrides[f.name]
        else:
            parsed = parse_frame_number(f.name)
            if parsed is None:
                errors.append(f"Could not parse a frame number from filename {f.name!r}.")
                continue
            frame_no = parsed + offset
        mapping.setdefault(frame_no, []).append(f)

    # Counts compare frames to frames, never files to frames.
    frame_set = set(frame_numbers)
    mapped_set = set(mapping)
    max_frame = max(frame_set) if frame_set else 0

    missing = sorted(frame_set - mapped_set - explained_gaps)

    # Trailing-excess case: files map to frames 1..n but the roll only
    # logs 1..m (m < n) — an under-logged roll, not a data error, PROVIDED every
    # logged frame 1..m already has a file. If any logged frame is still missing
    # (mid-roll gap or otherwise), an unexplained excess above m is left as a hard
    # error rather than guessing which problem to report.
    unknown = sorted(mapped_set - frame_set)
    # Files with no log entry are accepted anywhere in the roll — end or middle — as
    # long as every logged frame has a file. Then the numbering is demonstrably in
    # step and the extras are frames that simply were not entered in the app. If a
    # logged frame is ALSO missing, the numbering is suspect and it stays a hard error.
    unlogged_files: list[int] = []
    if unknown and not missing:
        unlogged_files = unknown
        unknown = []

    if unknown:
        errors.append(
            f"File(s) map to frame number(s) not present in the roll: {unknown}. "
            "Resolve with --offset or --map."
        )

    if unlogged_files:
        warnings.append(
            f"Not logged: frame(s) {unlogged_files} — date + camera data copied from neighbours."
        )
    if missing:
        sorted_frames = sorted(frame_set)
        is_trailing_shortfall = missing == sorted_frames[-len(missing):]
        if is_trailing_shortfall:
            warnings.append(
                f"Partially scanned roll: frame(s) {missing} have no file yet. "
                "Stamping the frames that are present."
            )
        else:
            errors.append(
                f"Frame(s) {missing} have no matching file, and the gap is not a "
                "trailing shortfall (a scan may be missing from the middle of the "
                "roll). Resolve with --offset or --map."
            )

    return mapping, errors, warnings


# ---------------------------------------------------------------------------
# Frame records
# ---------------------------------------------------------------------------

@dataclass
class FrameRecord:
    frame_number: int
    scan_paths: list[Path]
    snap_path: Path | None
    date_time_original: str
    offset_time_original: str
    subsec_time_original: str
    lens_key: str
    lens: dict
    fnumber: float
    exposure_time: str
    iso: int
    gps: dict | None
    caption: str | None
    flags: list[str]
    # A scan the afilm log has no entry for: date and camera data (lens, aperture,
    # shutter, ISO) are copied from the neighbouring logged frame; no GPS, no caption.
    inferred: bool = False


def build_frame_records(
    roll: dict,
    profile: dict,
    camera: dict,
    mapping: dict[int, list[Path]],
    snaps_dir: Path | None,
    tz_override: str | None,
    lens_choices: dict[int, str] | None = None,
) -> tuple[list[FrameRecord], list[str], dict[date, DayZone]]:
    warnings: list[str] = []
    lens_choices = lens_choices or {}

    def lens_for(src: FrameSource) -> tuple[str, dict, bool]:
        """The afilm lens name when there is one; else the lens picked with --lens; else
        the camera's default_lens, provisionally (flagged, and a write refuses it)."""
        if normalize_gear(src.lens_name) == "" and src.number in lens_choices:
            key, lens, _ = resolve_lens(lens_choices[src.number], camera, profile)
            return key, lens, False
        return resolve_lens(src.lens_name, camera, profile)
    sources = load_frames(roll)

    expected_frames = camera.get("expected_frames")
    if expected_frames is not None and len(sources) != expected_frames:
        warnings.append(
            f"Roll logs {len(sources)} frame(s), expected {expected_frames} for "
            f"{camera.get('model', 'this camera')}. Confirm manually."
        )

    day_zones, tz_warnings = resolve_day_zones(sources, profile, tz_override)
    warnings.extend(tz_warnings)

    threshold = profile["time"].get("burst_threshold_seconds", 15)
    iso = parse_iso(_require(roll, "iso", "on the roll"), _require(roll, "pullPush", "on the roll"))

    records: list[FrameRecord] = []
    burst_count = 0
    prev: FrameSource | None = None
    for src in sources:
        flags: list[str] = []

        is_burst = prev is not None and abs((src.date_local - prev.date_local).total_seconds()) < threshold
        if is_burst:
            flags.append("BURST")
        prev = src

        scan_paths = sorted(mapping.get(src.number, []))
        if not scan_paths:
            warnings.append(f"Frame {src.number}: no scan file mapped; skipped.")
            continue

        # Only count frames that actually appear (and are flagged) in the output —
        # a frame skipped here for lack of a scan never becomes a visible BURST row.
        if is_burst:
            burst_count += 1

        lens_key, lens, used_fallback = lens_for(src)
        if used_fallback:
            flags.append("LENS-FALLBACK")
            warnings.append(
                f"Frame {src.number}: no lens recorded; shown as default_lens ({lens_key}) "
                "until you pick one (--lens)."
            )
        elif src.number in lens_choices and normalize_gear(src.lens_name) == "":
            flags.append("LENS-CHOSEN")

        fnumber = parse_aperture(src.aperture)
        exposure_time = parse_shutter(src.shutter_speed)

        day_zone = day_zones[src.date_local.date()]
        stamp, offset_str, subsec = format_capture_time(src.date_local, day_zone.zone)
        if day_zone.mismatch:
            flags.append("TZ-DERIVED")

        gps = gps_tags(src.location)
        if gps is None:
            flags.append("NO-GPS")

        caption = src.note.strip() or None

        snap_path = None
        if snaps_dir is not None:
            candidate = snaps_dir / f"{src.number}.jpg"
            if candidate.exists():
                snap_path = candidate

        records.append(
            FrameRecord(
                frame_number=src.number,
                scan_paths=scan_paths,
                snap_path=snap_path,
                date_time_original=stamp,
                offset_time_original=offset_str,
                subsec_time_original=subsec,
                lens_key=lens_key,
                lens=lens,
                fnumber=fnumber,
                exposure_time=exposure_time,
                iso=iso,
                gps=gps,
                caption=caption,
                flags=flags,
            )
        )

    if burst_count:
        warnings.append(f"{burst_count} frame(s) flagged BURST (gap under {threshold}s).")

    # Scans with no log entry get a date and the camera data of a neighbouring logged
    # frame. Between two logged frames the date is interpolated by frame position; before
    # the first or after the last it is the nearest logged frame's date. Camera data
    # (lens, aperture, shutter) always comes from the earlier neighbour, else the later.
    # Scans are numbered in shooting order, so position is the best evidence there is.
    # No GPS or caption is copied. Flagged, never mistaken for a log entry.
    logged_numbers = {s.number for s in sources}
    for number in sorted(set(mapping) - logged_numbers):
        before = [s for s in sources if s.number < number]
        after = [s for s in sources if s.number > number]
        prev_src = before[-1] if before else None
        next_src = after[0] if after else None
        if prev_src is None and next_src is None:
            warnings.append(f"Frame {number}: not logged and no logged frame to take a date from; skipped.")
            continue
        if prev_src is not None and next_src is not None:
            span = next_src.number - prev_src.number
            frac = (number - prev_src.number) / span
            dt = prev_src.date_local + (next_src.date_local - prev_src.date_local) * frac
            dt = dt.replace(microsecond=0)
            flag = f"DATE-INTERPOLATED-{prev_src.number}-{next_src.number}"
        else:
            dt = (prev_src or next_src).date_local
            flag = f"DATE-FROM-{(prev_src or next_src).number}"
        zone_src = prev_src or next_src
        lens_key, lens, _ = lens_for(zone_src)
        day_zone = day_zones.get(zone_src.date_local.date())
        zone = day_zone.zone if day_zone else ZoneInfo(profile["time"]["timezone"])
        stamp, offset_str, subsec = format_capture_time(dt, zone)
        records.append(
            FrameRecord(
                frame_number=number,
                scan_paths=sorted(mapping[number]),
                snap_path=None,
                date_time_original=stamp,
                offset_time_original=offset_str,
                subsec_time_original=subsec,
                lens_key=lens_key,
                lens=lens,
                fnumber=parse_aperture(zone_src.aperture),
                exposure_time=parse_shutter(zone_src.shutter_speed),
                iso=iso,
                gps=None,
                caption=None,
                flags=["UNLOGGED", flag],
                inferred=True,
            )
        )

    records.sort(key=lambda r: r.frame_number)
    return records, warnings, day_zones


def records_fingerprint(records: list[FrameRecord]) -> str:
    """A short hash of everything the run would stamp. Saved in R##.html so a later check
    can tell the record no longer matches what the tool would write today."""
    payload = [
        [
            r.frame_number, [p.name for p in r.scan_paths], r.date_time_original,
            r.offset_time_original, r.lens_key, r.lens.get("model"), r.lens.get("lens_serial"),
            r.fnumber, r.exposure_time, r.iso, r.gps, r.caption,
        ]
        for r in records
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def build_keywords(
    profile: dict, camera: dict, stock: dict | None, filter_name: str
) -> list[str]:
    kw_cfg = profile.get("keywords", {})
    keywords: list[str] = []
    if kw_cfg.get("emit_film_stock", True) and stock:
        keywords.append(stock["name"])
    if kw_cfg.get("emit_film_format", True) and camera.get("film_format"):
        keywords.append(camera["film_format"])
    if kw_cfg.get("emit_camera", True) and camera.get("model"):
        keywords.append(camera["model"])
    if kw_cfg.get("emit_filter", True) and filter_name:
        keywords.append(filter_name)
    keywords.extend(kw_cfg.get("extra", []))
    return keywords


# ---------------------------------------------------------------------------
# Roll-shorthand resolution ("R49" -> export zip + scan folder)
# ---------------------------------------------------------------------------

ROLL_SHORTHAND_RE = re.compile(r"^R\d+$", re.IGNORECASE)


def open_in_viewer(path: Path) -> None:
    """Show a finished file (review page, contact sheet) in its default macOS app."""
    subprocess.run(["open", str(path)], check=False)


def default_profile_path() -> Path:
    """./roll-profile.toml when there is one, otherwise the profile that lives with the
    tool — so the command works from any folder without --profile."""
    here = Path("roll-profile.toml")
    return here if here.exists() else Path(__file__).resolve().parent / "roll-profile.toml"


def resolve_export_shorthand(shorthand: str, downloads_dir: Path) -> Path:
    # The separator class matters: "R4[ _.]*" must not match "R49 ...".
    pattern = f"{shorthand}[ _.]*.zip"
    candidates = sorted(downloads_dir.glob(pattern))
    if len(candidates) != 1:
        raise FilmstampError(
            f"Roll shorthand {shorthand!r}: expected exactly one export zip matching "
            f"{pattern!r} in {downloads_dir}, found {len(candidates)}: "
            f"{[p.name for p in candidates]}"
        )
    return candidates[0]


def resolve_scan_folder_shorthand(shorthand: str, safety_root: Path) -> Path:
    # The lab-number suffix after "R49-" is not predictable, hence the wildcard;
    # searched across every camera subfolder under the safety root.
    pattern = f"*/{shorthand}-*"
    candidates = sorted(p for p in safety_root.glob(pattern) if p.is_dir())
    if len(candidates) != 1:
        raise FilmstampError(
            f"Roll shorthand {shorthand!r}: expected exactly one scan folder matching "
            f"{pattern!r} under {safety_root}, found {len(candidates)}: "
            f"{[str(p) for p in candidates]}"
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# afilm export loading
# ---------------------------------------------------------------------------

def load_export(export_path: Path) -> tuple[dict, Path | None, tempfile.TemporaryDirectory | None]:
    if export_path.suffix.lower() == ".zip":
        tmpdir = tempfile.TemporaryDirectory(prefix="filmstamp-")
        with zipfile.ZipFile(export_path) as zf:
            zf.extractall(tmpdir.name)
        roll = json.loads((Path(tmpdir.name) / "data.json").read_text(encoding="utf-8"))
        return roll, Path(tmpdir.name), tmpdir
    roll = json.loads(export_path.read_text(encoding="utf-8"))
    return roll, export_path.parent, None


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def validate_scan_path(scan_folder: Path, safety_root: Path) -> None:
    if ".." in scan_folder.parts:
        raise FilmstampError(f"Refusing a path containing '..': {scan_folder}")
    # Photos is symlinked and Synology-backed: compare resolved (canonical,
    # symlink-following) paths, never raw strings — a string check can be defeated
    # in either direction by the CloudStorage indirection.
    resolved = scan_folder.resolve()
    if NAS_BLOCK_MARKER in str(resolved):
        raise FilmstampError(f"Refusing to run under a NAS RAW-files path: {resolved}")
    safe_root = safety_root.resolve()
    if resolved != safe_root and safe_root not in resolved.parents:
        raise FilmstampError(f"Refusing to run outside {safe_root}: {resolved}")


# ---------------------------------------------------------------------------
# Output — stdout table + R##.html
# ---------------------------------------------------------------------------

def print_reconciliation_table(
    roll: dict, roll_number: str | None, camera: dict, records: list[FrameRecord], warnings: list[str]
) -> None:
    title = roll.get("name", "").strip()
    header = f"{title} (roll {roll_number or '?'}) — {camera.get('model', '?')} — {len(records)} frame(s)"
    print(header)
    print("-" * len(header))
    row_fmt = "{:>4}  {:<32}  {:<24}  {:<24}  {:>5}  {:>7}  {:<20}  {:<20}  {:<20}"
    print(row_fmt.format("Fr#", "File", "Capture Time", "Lens", "f/", "Shutter", "GPS", "Note", "Flags"))
    for r in records:
        gps = f"{r.gps['lat']:.4f},{r.gps['lon']:.4f}" if r.gps else "-"
        print(
            row_fmt.format(
                r.frame_number,
                ", ".join(p.name for p in r.scan_paths),
                f"{r.date_time_original} {r.offset_time_original}",
                r.lens.get("model", "-"),
                _fmt_num(r.fnumber) if r.fnumber is not None else "-",
                r.exposure_time or "-",
                gps,
                r.caption or "-",
                ",".join(r.flags) or "-",
            )
        )
    print()
    if warnings:
        print(f"{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("0 warnings.")


def _b64_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def generate_scan_thumbnail(scan_path: Path, out_dir: Path, max_dim: int = 240) -> Path | None:
    """Downsized JPEG via sips — already on macOS, so no imaging dependency is added."""
    out_path = out_dir / f"{scan_path.stem}.jpg"
    try:
        subprocess.run(
            ["sips", "-Z", str(max_dim), "-s", "format", "jpeg", str(scan_path), "--out", str(out_path)],
            check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return out_path if out_path.exists() else None


def build_write_status(write_result: "WriteResult | None") -> dict | None:
    """None means the run never attempted a write (a real dry run). Otherwise the
    review page must show what actually landed on disk, not what was intended."""
    if write_result is None:
        return None
    failed = bool(write_result.written_failed or write_result.not_written)
    return {
        "mode": "failed" if failed else "written",
        "written_verified": len(write_result.written_verified),
        "written_failed": len(write_result.written_failed),
        "not_written": len(write_result.not_written),
        "skipped_existing": len(write_result.skipped_existing),
        "exiftool_error": write_result.exiftool_error,
    }


def _flag_chip(flag: str) -> dict:
    """Review-page label for a flag. The date-inference flags carry frame numbers, so
    they get a readable label and one shared colour class."""
    if flag == "UNLOGGED":
        return {"css": "UNLOGGED", "label": "NOT LOGGED"}
    m = re.fullmatch(r"DATE-INTERPOLATED-(\d+)-(\d+)", flag)
    if m:
        return {"css": "UNLOGGED", "label": f"DATE CALCULATED ({m.group(1)}–{m.group(2)}), CAMERA COPIED FROM {m.group(1)}"}
    m = re.fullmatch(r"DATE-FROM-(\d+)", flag)
    if m:
        return {"css": "UNLOGGED", "label": f"DATE + CAMERA COPIED FROM {m.group(1)}"}
    return {"css": flag, "label": flag}


def render_review_html(
    scan_folder: Path,
    export_path: Path,
    roll: dict,
    roll_number: str | None,
    camera: dict,
    stock: dict | None,
    keywords: list[str],
    records: list[FrameRecord],
    warnings: list[str],
    filter_name: str,
    developed_at: str,
    expected_frames: int | None,
    actual_frame_count: int,
    day_zone_rows: list[dict],
    lens_fallback_rows: list[dict],
    unscanned_frames: list[int],
    unlogged_frames: list[int],
    write_status: dict | None,
) -> Path | None:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("roll.html.j2")

    rows = []
    with tempfile.TemporaryDirectory(prefix="filmstamp-thumbs-") as thumb_dir_name:
        thumb_dir = Path(thumb_dir_name)
        for r in records:
            snap_uri = f"data:image/jpeg;base64,{_b64_image(r.snap_path)}" if r.snap_path else None

            scans = []
            for scan_path in r.scan_paths:
                scan_thumb = generate_scan_thumbnail(scan_path, thumb_dir)
                if scan_thumb is not None:
                    scan_uri = f"data:image/jpeg;base64,{_b64_image(scan_thumb)}"
                else:
                    scan_uri = None
                    print(
                        f"WARNING: Frame {r.frame_number}: sips could not thumbnail "
                        f"{scan_path.name}; showing a placeholder.",
                        file=sys.stderr,
                    )
                scans.append({"name": scan_path.name, "uri": scan_uri})

            rows.append(
                {
                    "frame_number": r.frame_number,
                    "scans": scans,
                    "snap_uri": snap_uri,
                    "capture_time": f"{r.date_time_original} {r.offset_time_original}",
                    "lens": r.lens.get("model", ""),
                    "aperture": f"f/{_fmt_num(r.fnumber)}" if r.fnumber is not None else "",
                    "shutter": r.exposure_time or "",
                    "iso": r.iso if r.iso is not None else "",
                    "gps": f"{r.gps['lat']:.6f}, {r.gps['lon']:.6f}" if r.gps else None,
                    "caption": r.caption,
                    "flags": r.flags,
                    "flag_chips": [_flag_chip(f) for f in r.flags],
                }
            )

    record_state = "dry-run" if write_status is None else write_status["mode"]
    html = template.render(
        resolved_export=str(export_path.resolve()),
        resolved_scan_folder=str(scan_folder.resolve()),
        roll_name=roll.get("name", "").strip(),
        roll_number=roll_number,
        camera=camera,
        stock=stock,
        keywords=keywords,
        filter_name=filter_name,
        developed_at=developed_at,
        frame_count=len(records),
        expected_frames=expected_frames,
        actual_frame_count=actual_frame_count,
        frame_count_mismatch=expected_frames is not None and actual_frame_count != expected_frames,
        day_zone_rows=day_zone_rows,
        lens_fallback_rows=lens_fallback_rows,
        unscanned_frames=unscanned_frames,
        unlogged_frames=unlogged_frames,
        warnings=warnings,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        rows=rows,
        write_status=write_status,
        record_state=record_state,
        fingerprint=records_fingerprint(records),
    )

    out_path = scan_folder / review_filename(roll_number, scan_folder)
    return write_review(out_path, html, record_state)


def _temp_sibling(path: Path) -> Path:
    """A unique temp file beside `path` — two filmstamp runs on one roll never share one."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    os.close(fd)
    # mkstemp creates 0600; the file replaces R##.html/.pdf/.zip, which have always been
    # created with the umask's mode, so give it that back.
    umask = os.umask(0)
    os.umask(umask)
    os.chmod(name, 0o666 & ~umask)
    return Path(name)


def review_filename(roll_number: str | None, scan_folder: Path) -> str:
    """R##.html, named for the roll and sitting beside R##.pdf. Without a parseable
    roll number it falls back to the roll's own scan folder name (one folder per roll),
    never to a constant shared between rolls."""
    if roll_number:
        return f"R{roll_number}.html"
    stem = re.sub(r"[^\w.-]+", "_", scan_folder.name).strip("._")
    return f"{stem or 'roll'}.html"


_RECORD_STATE_RE = re.compile(r'<meta name="filmstamp-record" content="([\w-]+)"')
_RECORD_FINGERPRINT_RE = re.compile(r'<meta name="filmstamp-fingerprint" content="(\w+)"')


def write_review(out_path: Path, html: str, new_state: str) -> Path | None:
    """R##.html is the roll's permanent data record, never a scratch file. Only a
    previous dry-run record is replaced freely. A record of a real write — or any file
    this tool didn't author — is never clobbered by a dry run (returns None), and is
    moved aside, never deleted, when a later write replaces it. The new page is fully
    written to a temp file first, so a failed write cannot leave the roll without a
    record."""
    existing_state = None
    if out_path.exists():
        match = _RECORD_STATE_RE.search(out_path.read_text(encoding="utf-8", errors="replace"))
        existing_state = match.group(1) if match else "unknown"
        if existing_state != "dry-run" and new_state == "dry-run":
            return None

    tmp_path = _temp_sibling(out_path)
    try:
        tmp_path.write_text(html, encoding="utf-8")
        if existing_state not in (None, "dry-run"):
            stamp = datetime.fromtimestamp(out_path.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
            archived = out_path.with_name(f"{out_path.stem}.{stamp}{out_path.suffix}")
            n = 0
            while archived.exists():
                n += 1
                archived = out_path.with_name(f"{out_path.stem}.{stamp}-{n}{out_path.suffix}")
            out_path.rename(archived)
            print(f"NOTE: previous {out_path.name} kept as {archived.name}.", file=sys.stderr)
        tmp_path.replace(out_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return out_path


# ---------------------------------------------------------------------------
# Export archive — the phone is not a backup
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_export(export_path: Path, scan_folder: Path, roll_number: str | None) -> tuple[Path, str]:
    """Copy the afilm export zip beside the scans as R##.afilm.zip and verify the copy
    by hash. Never overwrites: an identical archive is left alone, and a differing
    re-export is kept alongside as R##.afilm.<timestamp>.zip. Returns (path, status)
    with status one of 'archived', 'already-archived'."""
    stem = f"R{roll_number}" if roll_number else re.sub(r"[^\w.-]+", "_", scan_folder.name).strip("._") or "roll"
    source_hash = _sha256(export_path)

    existing = sorted(scan_folder.glob(f"{stem}.afilm*.zip"))
    for candidate in existing:
        if _sha256(candidate) == source_hash:
            return candidate, "already-archived"

    target = scan_folder / f"{stem}.afilm.zip"
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = scan_folder / f"{stem}.afilm.{stamp}.zip"
        n = 0
        while target.exists():
            n += 1
            target = scan_folder / f"{stem}.afilm.{stamp}-{n}.zip"

    tmp = _temp_sibling(target)
    try:
        shutil.copyfile(export_path, tmp)
        if _sha256(tmp) != source_hash:
            raise FilmstampError(f"Archive copy of {export_path.name} failed hash verification.")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return target, "archived"


# ---------------------------------------------------------------------------
# Writing — the argfile, the idempotency guard, and verification.
# XMP-dc:Title is never added to a tag set anywhere below.
# ---------------------------------------------------------------------------

def _fmt_num(x: float) -> str:
    return f"{x:g}"


def _fmt_gps(x: float) -> str:
    # afilm reports 7 decimal places (sub-meter); ":g" truncates to 6 significant
    # digits and silently degrades GPS precision, so coordinates get their own formatter.
    return f"{x:.7f}"


def build_frame_tags(
    record: FrameRecord, camera: dict, identity: dict, keywords: list[str]
) -> dict[str, object]:
    """Every tag of the tag contract is included on every run — as an empty string
    when the source field is absent — so a field cleared in a re-export clears the
    tag on the file too. XMP-dc:Title is never added, under any circumstance."""
    if record.gps is not None:
        lat, lat_ref = _fmt_gps(record.gps["lat"]), record.gps["lat_ref"]
        lon, lon_ref = _fmt_gps(record.gps["lon"]), record.gps["lon_ref"]
    else:
        lat = lat_ref = lon = lon_ref = ""

    focal_length_mm = record.lens.get("focal_length_mm")
    caption = record.caption or ""

    return {
        "EXIF:DateTimeOriginal": record.date_time_original,
        "EXIF:CreateDate": record.date_time_original,
        "EXIF:OffsetTimeOriginal": record.offset_time_original,
        "EXIF:SubSecTimeOriginal": record.subsec_time_original,
        "EXIF:Make": camera["make"],
        "EXIF:Model": camera["model"],
        "EXIF:SerialNumber": camera.get("body_serial") or "",
        "EXIF:LensMake": record.lens.get("make") or "",
        "EXIF:LensModel": record.lens.get("model") or "",
        "XMP-aux:Lens": record.lens.get("model") or "",
        "EXIF:LensSerialNumber": record.lens.get("lens_serial") or "",
        "EXIF:FocalLength": _fmt_num(focal_length_mm) if focal_length_mm is not None else "",
        "EXIF:FNumber": _fmt_num(record.fnumber),
        "EXIF:ExposureTime": record.exposure_time,
        "EXIF:ISO": str(record.iso),
        "EXIF:GPSLatitude": lat,
        "EXIF:GPSLatitudeRef": lat_ref,
        "EXIF:GPSLongitude": lon,
        "EXIF:GPSLongitudeRef": lon_ref,
        "IPTC:Caption-Abstract": caption,
        "XMP-dc:Description": caption,
        "XMP-dc:Subject": list(keywords),
        "EXIF:Artist": identity.get("artist") or "",
        "XMP-dc:Creator": identity.get("artist") or "",
        "EXIF:Copyright": identity.get("copyright") or "",
        "XMP-dc:Rights": identity.get("copyright") or "",
    }


def write_argfile(
    path: Path, frame_tags: list[tuple[Path, dict[str, object]]], values_dir: Path
) -> None:
    """Free-text values (captions, notes — anything not controlled by this tool) go
    through ExifTool's documented -TAG<=DATFILE convention, never inlined as
    "-TAG=value" text. An argfile is one argument per line with no shell quoting, so
    an inlined value containing a newline (or, split onto its own line, one that
    happens to start with '-') would be misparsed as a separate argument."""
    lines: list[str] = []
    value_index = 0

    def value_file(value: str) -> Path:
        nonlocal value_index
        value_index += 1
        vpath = values_dir / f"value-{value_index}.txt"
        vpath.write_text(value, encoding="utf-8")
        return vpath

    for scan_path, tags in frame_tags:
        list_tags = [tag for tag, value in tags.items() if isinstance(value, list)]
        if list_tags:
            # Clear in its own -execute block first: under --force a repeated tag like
            # XMP-dc:Subject already carries a prior run's values, and clearing plus
            # appending in the *same* block is a no-op — ExifTool only respects the
            # clear when it's applied and committed before the additions.
            for tag in list_tags:
                lines.append(f"-{tag}=")
            lines.append(str(scan_path))
            lines.append("-execute")
        for tag, value in tags.items():
            if isinstance(value, list):
                for v in value:
                    lines.append(f"-{tag}+<={value_file(str(v))}")
            elif value == "":
                # An explicit clear: "-TAG=" with nothing after deletes the tag.
                lines.append(f"-{tag}=")
            else:
                lines.append(f"-{tag}<={value_file(str(value))}")
        lines.append(str(scan_path))
        lines.append("-execute")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_SF_DATALESS = 0x40000000  # macOS: file is a dataless (not-yet-materialized) object


def is_cloud_placeholder(path: Path) -> bool:
    """A file Synology Drive (or iCloud/OneDrive/Dropbox, same mechanism) hasn't
    downloaded locally yet: on-demand sync leaves a placeholder that reports its full
    remote size but has no real data on disk."""
    st = path.stat()
    if getattr(st, "st_flags", 0) & _SF_DATALESS:
        return True
    if st.st_size > 0 and st.st_blocks == 0:
        return True
    return False


def find_already_stamped(paths: list[Path]) -> set[str]:
    """One combined query, not one subprocess per file."""
    if not paths:
        return set()
    proc = subprocess.run(
        ["exiftool", "-json", "-DateTimeOriginal", *[str(p) for p in paths]],
        capture_output=True, text=True, check=True,
    )
    records = json.loads(proc.stdout)
    return {Path(r["SourceFile"]).name for r in records if r.get("DateTimeOriginal")}


_VERIFY_READ_ARGS = [
    "-DateTimeOriginal", "-CreateDate", "-OffsetTimeOriginal", "-SubSecTimeOriginal",
    "-Make", "-Model", "-EXIF:SerialNumber", "-LensMake", "-LensModel", "-XMP-aux:Lens",
    "-EXIF:LensSerialNumber",
    "-FocalLength", "-FNumber", "-ExposureTime", "-ISO",
    "-Composite:GPSLatitude", "-Composite:GPSLongitude", "-GPSLatitudeRef", "-GPSLongitudeRef",
    "-IPTC:Caption-Abstract", "-XMP-dc:Description", "-XMP-dc:Subject",
    "-Artist", "-XMP-dc:Creator", "-Copyright", "-XMP-dc:Rights",
    "-XMP-dc:Title",
]


def read_tags_for_verification(paths: list[Path]) -> dict[str, dict]:
    proc = subprocess.run(
        ["exiftool", "-charset", "iptc=UTF8", "-json", "-n", *_VERIFY_READ_ARGS, *[str(p) for p in paths]],
        capture_output=True, text=True, check=True,
    )
    records = json.loads(proc.stdout)
    return {Path(r["SourceFile"]).name: r for r in records}


def _shutter_seconds(raw: str) -> float:
    if "/" in raw:
        num, den = raw.split("/")
        return float(num) / float(den)
    return float(raw)


def verify_frame_tags(
    record: FrameRecord, camera: dict, identity: dict, keywords: list[str], actual: dict
) -> list[str]:
    """Diff the file's real, re-read tags against intent. A write that isn't verified didn't happen."""
    mismatches: list[str] = []

    def check_str(key: str, expected: object) -> None:
        got = actual.get(key)
        if got is None or str(got) != str(expected):
            mismatches.append(f"{key}: expected {expected!r}, got {got!r}")

    def check_float(key: str, expected: float, tol: float = 1e-4) -> None:
        got = actual.get(key)
        try:
            ok = got is not None and abs(float(got) - float(expected)) <= tol
        except (TypeError, ValueError):
            ok = False
        if not ok:
            mismatches.append(f"{key}: expected {expected!r}, got {got!r}")

    def check_absent(key: str) -> None:
        got = actual.get(key)
        if got not in (None, ""):
            mismatches.append(f"{key}: expected cleared (absent), got {got!r}")

    check_str("DateTimeOriginal", record.date_time_original)
    check_str("CreateDate", record.date_time_original)
    check_str("OffsetTimeOriginal", record.offset_time_original)
    check_str("SubSecTimeOriginal", record.subsec_time_original)
    check_str("Make", camera["make"])
    check_str("Model", camera["model"])

    if camera.get("body_serial"):
        check_str("SerialNumber", camera["body_serial"])
    else:
        check_absent("SerialNumber")
    _verify_logged_fields(record, actual, check_str, check_float, check_absent, mismatches)

    got_subject = actual.get("Subject")
    if isinstance(got_subject, str):
        got_subject = [got_subject]
    if keywords:
        if got_subject is None or list(got_subject) != list(keywords):
            mismatches.append(f"Subject: expected {keywords!r}, got {got_subject!r}")
    elif got_subject not in (None, []):
        mismatches.append(f"Subject: expected cleared (absent), got {got_subject!r}")

    if identity.get("artist"):
        check_str("Artist", identity["artist"])
        check_str("Creator", identity["artist"])
    else:
        check_absent("Artist")
        check_absent("Creator")
    if identity.get("copyright"):
        check_str("Copyright", identity["copyright"])
        check_str("Rights", identity["copyright"])
    else:
        check_absent("Copyright")
        check_absent("Rights")

    if "Title" in actual:
        mismatches.append(f"Title: must never be written, but found {actual['Title']!r}")

    return mismatches


def _verify_logged_fields(record, actual, check_str, check_float, check_absent, mismatches) -> None:
    if record.lens.get("lens_serial"):
        check_str("LensSerialNumber", record.lens["lens_serial"])
    else:
        check_absent("LensSerialNumber")

    if record.lens.get("make"):
        check_str("LensMake", record.lens["make"])
    else:
        check_absent("LensMake")
    if record.lens.get("model"):
        check_str("LensModel", record.lens["model"])
        check_str("Lens", record.lens["model"])
    else:
        check_absent("LensModel")
        check_absent("Lens")
    if record.lens.get("focal_length_mm") is not None:
        check_float("FocalLength", record.lens["focal_length_mm"])
    else:
        check_absent("FocalLength")

    check_float("FNumber", record.fnumber)
    check_float("ExposureTime", _shutter_seconds(record.exposure_time))
    check_float("ISO", record.iso)

    if record.gps is not None:
        check_float("GPSLatitude", record.gps["lat"])
        check_float("GPSLongitude", record.gps["lon"])
        check_str("GPSLatitudeRef", record.gps["lat_ref"])
        check_str("GPSLongitudeRef", record.gps["lon_ref"])
    else:
        check_absent("GPSLatitude")
        check_absent("GPSLongitude")
        check_absent("GPSLatitudeRef")
        check_absent("GPSLongitudeRef")

    if record.caption:
        check_str("Caption-Abstract", record.caption)
        check_str("Description", record.caption)
    else:
        check_absent("Caption-Abstract")
        check_absent("Description")


@dataclass
class WriteResult:
    written_verified: list[Path]
    written_failed: dict[Path, list[str]]
    not_written: list[Path]
    skipped_existing: list[Path]
    exiftool_error: str | None


def perform_write(
    records: list[FrameRecord], camera: dict, profile: dict, keywords: list[str], force: bool
) -> WriteResult:
    identity = profile.get("identity", {})

    # A frame's files (source scan + converted positive) all get identical metadata.
    file_frame_pairs = [(path, r) for r in records for path in r.scan_paths]
    all_paths = [path for path, _ in file_frame_pairs]

    placeholders = sorted({p.name for p in all_paths if is_cloud_placeholder(p)})
    if placeholders:
        raise FilmstampError(
            "Refusing to write: the following file(s) are Synology Drive cloud "
            "placeholders, not downloaded locally yet. Materialize them first (Finder → "
            "right-click → Download Now) and re-run:\n  " + "\n  ".join(placeholders)
        )

    already = set() if force else find_already_stamped(all_paths)
    to_write = [(path, r) for path, r in file_frame_pairs if path.name not in already]
    skipped = [path for path, _ in file_frame_pairs if path.name in already]

    if not to_write:
        return WriteResult(
            written_verified=[], written_failed={}, not_written=[],
            skipped_existing=skipped, exiftool_error=None,
        )

    frame_tags = {r.frame_number: build_frame_tags(r, camera, identity, keywords) for _, r in to_write}

    with tempfile.TemporaryDirectory(prefix="filmstamp-write-") as tmp:
        tmp_path = Path(tmp)
        argfile = tmp_path / "args.txt"
        write_argfile(
            argfile, [(path, frame_tags[r.frame_number]) for path, r in to_write], tmp_path
        )
        # -overwrite_original_in_place: edit each scan where it is, leave no *_original backup.
        proc = subprocess.run(
            ["exiftool", "-charset", "iptc=UTF8", "-@", str(argfile),
             # -common_args: the argfile is many -execute blocks, and a plain option
             # would only reach the first one.
             "-common_args", "-overwrite_original_in_place"],
            capture_output=True, text=True,
        )
        # Don't raise on a non-zero exit: ExifTool's -execute blocks before a failing
        # one are still committed to disk, so a raw abort here would leave those
        # files stamped but never checked. The invariant is that nothing is left
        # unverified, not that nothing is left unwritten — verify every candidate
        # file below regardless of the process exit code, and let the per-file
        # classification (and the caller's exit code) carry the failure.
        exiftool_error = (proc.stdout + proc.stderr).strip() if proc.returncode != 0 else None

    actual = read_tags_for_verification([path for path, _ in to_write])

    written_verified: list[Path] = []
    written_failed: dict[Path, list[str]] = {}
    not_written: list[Path] = []

    for path, r in to_write:
        file_actual = actual.get(path.name, {})
        touched = str(file_actual.get("DateTimeOriginal")) == str(r.date_time_original)
        if not touched:
            not_written.append(path)
            continue
        problems = verify_frame_tags(r, camera, identity, keywords, file_actual)
        if problems:
            written_failed[path] = problems
        else:
            written_verified.append(path)

    return WriteResult(
        written_verified=written_verified,
        written_failed=written_failed,
        not_written=not_written,
        skipped_existing=skipped,
        exiftool_error=exiftool_error,
    )


# ---------------------------------------------------------------------------
# Contact sheet — reads only; the one file it creates is R##.pdf.
# The PDF is written with the standard library: JPEG thumbnails from sips are embedded
# as DCT streams and the text uses PDF's built-in Helvetica, so no dependency is added.
# ---------------------------------------------------------------------------

SHEET_FRAME_RE = re.compile(r"R(\d+)[\s_-]+(\d{3})(?!\d)")
SHEET_FOLDER_RE = re.compile(r"^R(\d+)(?:-(.+))?$")
SHEET_EXTENSIONS = (".tif", ".tiff", ".jpg", ".jpeg")
STRIPS_PER_PAGE = 5  # a 13-frame roll cut 2,2,3,3,3 is five strips and must fit one page

# Frames the lab cuts per strip, by film format.
FRAMES_PER_STRIP = {"6x4.5": 3, "6x6": 3, "6x7": 2, "6x9": 2}

TIER_POSITIVE, TIER_JPG, TIER_TIF = 0, 1, 2


def parse_frame_ref(filename: str) -> tuple[str, int]:
    """(roll number, frame number) from a finished-file name; the frame number may sit
    anywhere in the name, after a title. Zero or several distinct matches is a hard
    error naming the file."""
    matches = {(int(r), int(f)) for r, f in SHEET_FRAME_RE.findall(Path(filename).stem)}
    if len(matches) != 1:
        raise FilmstampError(
            f"Cannot read a single 'R<roll> <frame>' reference from {filename!r} "
            f"({len(matches)} found)."
        )
    roll, frame = matches.pop()
    return str(roll), frame


def sheet_strips(frame_count: int, per_strip: int) -> list[int]:
    """Strips of at most `per_strip`; the shortfall is spread one frame at a time over
    the first strips, so short strips come first (13 -> 2,2,3,3,3)."""
    if frame_count < 1:
        raise FilmstampError("Roll has no frames to lay out.")
    k = -(-frame_count // per_strip)
    shortfall = per_strip * k - frame_count
    return [per_strip - 1 if i < shortfall else per_strip for i in range(k)]


def sheet_pages(strips: list[int]) -> list[list[list[int | None]]]:
    """pages -> rows -> cells; a cell is a frame number or None where a strip is short.
    A page holds up to five strips, one strip per row; a longer roll continues on the
    next page. Cell size is the same for every roll, so sheets look alike in the binder."""
    columns = max(strips)
    rows: list[list[int | None]] = []
    nxt = 1
    for size in strips:
        rows.append([nxt + i if i < size else None for i in range(columns)])
        nxt += size
    return [rows[i : i + STRIPS_PER_PAGE] for i in range(0, len(rows), STRIPS_PER_PAGE)]


@dataclass
class SheetFrame:
    number: int
    path: Path
    tier: int
    wrong_roll: str | None = None  # the roll number the file claims, when it is not this folder's
    no_exif: bool = False


def select_sheet_files(folder: Path, folder_roll: str) -> dict[int, SheetFrame]:
    """One image per frame: -positive.tif, else a titled .jpg in the folder root, else
    the plain .tif. Export/ and everything that is not an image are not looked at."""
    candidates: dict[int, list[tuple[int, Path, str]]] = {}
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name.startswith(".") or p.suffix.lower() not in SHEET_EXTENSIONS:
            continue
        roll, frame = parse_frame_ref(p.name)
        if frame == 0:
            raise FilmstampError(f"Frame 0 in {p.name!r}.")
        if p.suffix.lower() in (".jpg", ".jpeg"):
            tier = TIER_JPG
        elif p.stem.lower().endswith("-positive"):
            tier = TIER_POSITIVE
        else:
            tier = TIER_TIF
        candidates.setdefault(frame, []).append((tier, p, roll))

    chosen: dict[int, SheetFrame] = {}
    for frame, options in candidates.items():
        best = min(t for t, _, _ in options)
        top = [(p, roll) for t, p, roll in options if t == best]
        if len(top) > 1:
            raise FilmstampError(
                f"Frame {frame} is ambiguous: {[p.name for p, _ in top]} are equally good "
                "sources for it."
            )
        wrong = next((roll for _, _, roll in options if roll != folder_roll), None)
        chosen[frame] = SheetFrame(frame, top[0][0], best, wrong_roll=wrong)
    return chosen


def sheet_metadata(paths: list[Path]) -> dict[str, dict]:
    proc = subprocess.run(
        ["exiftool", "-json", "-charset", "iptc=UTF8", "-Model", "-DateTimeOriginal", "-Subject",
         *[str(p) for p in paths]],
        capture_output=True, text=True, check=True,
    )
    return {Path(r["SourceFile"]).name: r for r in json.loads(proc.stdout)}


def _most_common(values: list[str]) -> str | None:
    return Counter(values).most_common(1)[0][0] if values else None


def build_sheet_title(
    camera: str, roll_id: str, stock: str | None, date_text: str, lab: str | None
) -> str:
    """The cookbook's five fields in a fixed order. A missing lab number prints
    'Lab —', never a placeholder."""
    return " · ".join([camera, roll_id, stock or "—", date_text, f"Lab {lab or '—'}"])


# Helvetica / Helvetica-Bold advance widths (1/1000 em) for ASCII 32..126, plus the few
# extra characters the title uses. Needed only to centre text; anything else counts 556.
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
         1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
         333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_HELV_B = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
           556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
           975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
           667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
           333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
           611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]
_EXTRA_WIDTHS = {"·": 278, "—": 1000, "–": 556, "≠": 584}


def text_width(text: str, size: float, bold: bool = False) -> float:
    table = _HELV_B if bold else _HELV
    total = sum(
        table[ord(c) - 32] if 32 <= ord(c) <= 126 else _EXTRA_WIDTHS.get(c, 556) for c in text
    )
    return total * size / 1000


def _pdf_text(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def jpeg_size(data: bytes) -> tuple[int, int, int]:
    """(width, height, components) from the first SOF marker."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return (
                int.from_bytes(data[i + 7 : i + 9], "big"),
                int.from_bytes(data[i + 5 : i + 7], "big"),
                data[i + 9],
            )
        i += 2 + int.from_bytes(data[i + 2 : i + 4], "big")
    raise FilmstampError("Could not read the size of a thumbnail JPEG.")


class PdfBuilder:
    """Just enough PDF: pages of text, rectangles and JPEG images."""

    PAGE_W, PAGE_H = 612, 792  # US Letter, points

    def __init__(self, title: str):
        self.title = title
        self.objects: dict[int, bytes] = {}
        self.next_id = 5  # 1 catalog, 2 page tree, 3/4 fonts are fixed; the rest are allocated
        self.pages: list[int] = []
        self._ops: list[str] = []
        self._xobjects: dict[str, int] = {}

    def _alloc(self, body: bytes) -> int:
        oid = self.next_id
        self.next_id += 1
        self.objects[oid] = body
        return oid

    def new_page(self) -> None:
        self._flush()
        self._ops, self._xobjects = [], {}

    def text(self, x: float, y: float, text: str, size: float, bold: bool = False,
             red: bool = False, center: bool = False) -> None:
        if center:
            x -= text_width(text, size, bold) / 2
        colour = "0.8 0 0 rg" if red else "0 g"
        font = "F2" if bold else "F1"
        self._ops.append(
            f"BT {colour} /{font} {size:g} Tf {x:.2f} {y:.2f} Td ({_pdf_text(text).decode('latin-1')}) Tj ET"
        )

    def rect(self, x: float, y: float, w: float, h: float, red: bool = False, width: float = 0.5) -> None:
        colour = "0.8 0 0 RG" if red else "0.6 G"
        self._ops.append(f"{colour} {width:g} w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re S")

    def image(self, jpeg: bytes, x: float, y: float, w: float, h: float) -> None:
        px_w, px_h, comps = jpeg_size(jpeg)
        if comps not in (1, 3):
            raise FilmstampError(f"Unsupported thumbnail colour space ({comps} components).")
        space = "/DeviceGray" if comps == 1 else "/DeviceRGB"
        head = (
            f"<< /Type /XObject /Subtype /Image /Width {px_w} /Height {px_h} "
            f"/ColorSpace {space} /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n"
        ).encode("latin-1")
        oid = self._alloc(head + jpeg + b"\nendstream")
        name = f"Im{len(self._xobjects) + 1}"
        self._xobjects[name] = oid
        self._ops.append(f"q {w:.2f} 0 0 {h:.2f} {x:.2f} {y:.2f} cm /{name} Do Q")

    def _flush(self) -> None:
        if not self._ops and not self._xobjects:
            return
        stream = zlib.compress("\n".join(self._ops).encode("latin-1"))
        content = self._alloc(
            f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode() + stream + b"\nendstream"
        )
        xobj = " ".join(f"/{n} {i} 0 R" for n, i in self._xobjects.items())
        page = self._alloc(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.PAGE_W} {self.PAGE_H}] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> /XObject << {xobj} >> >> "
                f"/Contents {content} 0 R >>"
            ).encode()
        )
        self.pages.append(page)

    def to_bytes(self) -> bytes:
        self._flush()
        self._ops, self._xobjects = [], {}
        info = self._alloc(b"<< /Title (" + _pdf_text(self.title) + b") /Producer (filmstamp) >>")
        self.objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
        kids = " ".join(f"{p} 0 R" for p in self.pages)
        self.objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(self.pages)} >>".encode()
        self.objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
        self.objects[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for oid in sorted(self.objects):
            offsets[oid] = len(out)
            out += f"{oid} 0 obj\n".encode() + self.objects[oid] + b"\nendobj\n"
        xref = len(out)
        size = max(self.objects) + 1
        out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
        for oid in range(1, size):
            out += f"{offsets[oid]:010d} 00000 n \n".encode()
        out += f"trailer\n<< /Size {size} /Root 1 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)


def make_sheet_jpeg(src: Path, out_dir: Path, index: int, max_px: int = 800) -> bytes:
    out = out_dir / f"thumb-{index}.jpg"
    try:
        subprocess.run(
            ["sips", "-Z", str(max_px), "-s", "format", "jpeg", "-s", "formatOptions", "85",
             str(src), "--out", str(out)],
            check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        raise FilmstampError(f"sips could not make a thumbnail of {src.name}.") from exc
    return out.read_bytes()


def render_contact_sheet(
    frames: dict[int, SheetFrame],
    pages: list[list[list[int | None]]],
    title: str,
    subtitle: str | None,
    captions: str,
) -> bytes:
    pdf = PdfBuilder(title)
    W, H, M = PdfBuilder.PAGE_W, PdfBuilder.PAGE_H, 36
    columns = len(pages[0][0])
    cell_w = (W - 2 * M) / columns
    header = 22 + (14 if subtitle else 0)
    grid_top = H - M - header - 8
    grid_bottom = 40
    cell_h = (grid_top - grid_bottom) / STRIPS_PER_PAGE
    size = 12.0
    while size > 7 and text_width(title, size, True) > W - 2 * M:
        size -= 0.5

    with tempfile.TemporaryDirectory(prefix="filmstamp-sheet-") as tmp:
        tmp_dir = Path(tmp)
        index = 0
        for page_no, rows in enumerate(pages, start=1):
            pdf.new_page()
            pdf.text(W / 2, H - M - 10, title, size, bold=True, center=True)
            if subtitle:
                pdf.text(W / 2, H - M - 26, subtitle, 10, center=True)
            if len(pages) > 1:
                pdf.text(W / 2, 22, f"Page {page_no} of {len(pages)}", 8, center=True)
            for r, row in enumerate(rows):
                for c, number in enumerate(row):
                    if number is None or number not in frames:
                        continue
                    fr = frames[number]
                    x0 = M + c * cell_w
                    y0 = grid_top - (r + 1) * cell_h
                    box_x, box_y = x0 + 8, y0 + 20
                    box_w, box_h = cell_w - 16, cell_h - 28
                    index += 1
                    jpeg = make_sheet_jpeg(fr.path, tmp_dir, index)
                    px_w, px_h, _ = jpeg_size(jpeg)
                    scale = min(box_w / px_w, box_h / px_h)
                    w, h = px_w * scale, px_h * scale
                    ix, iy = box_x + (box_w - w) / 2, box_y + (box_h - h) / 2
                    pdf.image(jpeg, ix, iy, w, h)
                    if fr.wrong_roll:
                        pdf.rect(ix - 2, iy - 2, w + 4, h + 4, red=True, width=1.5)
                    label = fr.path.name if captions == "filename" else f"{number}"
                    notes = []
                    if fr.wrong_roll:
                        notes.append(f"file says R{fr.wrong_roll}")
                    if fr.tier == TIER_TIF:
                        notes.append("unconverted")
                    if fr.no_exif:
                        notes.append("no EXIF")
                    if captions == "filename":
                        label_size = 6.0
                        while label_size > 4 and text_width(label, label_size) > cell_w - 8:
                            label_size -= 0.5
                        pdf.text(x0 + cell_w / 2, y0 + 10, label, label_size, center=True)
                        if notes:
                            pdf.text(x0 + cell_w / 2, y0 + 3, " · ".join(notes), 6, red=True, center=True)
                    else:
                        pdf.text(x0 + cell_w / 2, y0 + 8, label, 10, bold=True, center=True)
                        if notes:
                            pdf.text(x0 + cell_w / 2, y0 + 1, " · ".join(notes), 6.5, red=True, center=True)
    return pdf.to_bytes()


def find_roll_export(folder: Path, folder_roll: str, profile: dict) -> Path | None:
    """The afilm export for a roll that was never stamped: the archived copy beside the
    scans first, then the landing zone. None when there is not exactly one."""
    archived = sorted(folder.glob(f"R{folder_roll}.afilm*.zip"))
    if archived:
        return archived[-1]
    downloads = profile.get("downloads_dir")
    if downloads:
        try:
            return resolve_export_shorthand(f"R{folder_roll}", Path(downloads).expanduser())
        except FilmstampError:
            return None
    return None


def read_export_roll(export_path: Path) -> dict:
    roll, _, tmpdir = load_export(export_path)
    if tmpdir is not None:
        tmpdir.cleanup()
    return roll


def parse_sheet_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="filmstamp sheet", description="Generate the R##.pdf contact sheet for a roll folder."
    )
    parser.add_argument(
        "folder", type=Path,
        help="roll shorthand like 'R49' (found under safety_root), or a roll folder path",
    )
    parser.add_argument("--profile", type=Path, default=default_profile_path())
    parser.add_argument("--date", default=None, help="date text for the title (default: year of the first frame)")
    parser.add_argument("--subtitle", default=None, help="optional free-text second line")
    parser.add_argument("--captions", choices=("frame", "filename"), default="frame")
    parser.add_argument("--strips", default=None, help="explicit strip cut, e.g. 2,2,3,3,3")
    parser.add_argument("--export", type=Path, default=None,
                        help="afilm export to fill in what unstamped files lack "
                             "(default: R##.afilm.zip in the folder, else the one in Downloads)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing R##.pdf")
    parser.add_argument("--no-open", dest="open_pdf", action="store_false", default=True,
                        help="do not open the PDF when it is done")
    return parser.parse_args(argv)


def run_sheet(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    safety_root_raw = profile.get("safety_root")
    if not safety_root_raw:
        raise FilmstampError("Profile is missing required top-level 'safety_root'.")
    safety_root = Path(safety_root_raw).expanduser()
    folder = args.folder
    if ROLL_SHORTHAND_RE.match(str(folder)) and not folder.exists():
        folder = resolve_scan_folder_shorthand(str(folder).upper(), safety_root)
    validate_scan_path(folder, safety_root)
    if not folder.is_dir():
        raise FilmstampError(f"Not a folder: {folder}")

    match = SHEET_FOLDER_RE.match(folder.resolve().name)
    if not match:
        raise FilmstampError(
            f"Folder name {folder.resolve().name!r} does not look like 'R<roll>' or 'R<roll>-<lab number>'."
        )
    folder_roll, lab = str(int(match.group(1))), match.group(2)

    out_path = folder / f"R{folder_roll}.pdf"
    if out_path.exists() and not args.force:
        raise FilmstampError(
            f"{out_path.name} already exists, and was left untouched. "
            "Add --force to replace it with a fresh one."
        )

    frames = select_sheet_files(folder, folder_roll)
    if not frames:
        raise FilmstampError(f"No frames found in {folder}.")
    placeholders = sorted(f.path.name for f in frames.values() if is_cloud_placeholder(f.path))
    if placeholders:
        raise FilmstampError(
            "These file(s) are Synology Drive cloud placeholders, not downloaded yet; "
            "materialize them and re-run:\n  " + "\n  ".join(placeholders)
        )

    meta = sheet_metadata([f.path for f in frames.values()])
    for fr in frames.values():
        fr.no_exif = not meta.get(fr.path.name, {}).get("DateTimeOriginal")

    model = _most_common([m["Model"] for m in meta.values() if m.get("Model")])
    camera = None
    if model:
        camera = next(
            (c for c in profile.get("cameras", {}).values()
             if normalize_gear(c.get("model", "")) == normalize_gear(model)),
            None,
        )

    stock_names = {s["name"] for s in profile.get("film_stocks", {}).values()}
    subjects: list[str] = []
    for m in meta.values():
        subject = m.get("Subject") or []
        subjects.extend([subject] if isinstance(subject, str) else subject)
    stock = _most_common([s for s in subjects if s in stock_names])
    taken = sorted(m["DateTimeOriginal"] for m in meta.values() if m.get("DateTimeOriginal"))
    year = taken[0][:4] if taken else None

    # Stamped files are the primary source. For a roll that was never stamped, the
    # afilm export supplies whatever the files lack: camera, film stock, year.
    filled: list[str] = []
    if camera is None or stock is None or year is None or args.export:
        export_path = args.export or find_roll_export(folder.resolve(), folder_roll, profile)
        if export_path is not None:
            roll = read_export_roll(export_path)
            roll_name = _require(roll, "name", "on the roll")
            export_roll = parse_roll_number(roll_name)
            if export_roll is None or str(int(export_roll)) != folder_roll:
                raise FilmstampError(
                    f"{export_path.name} is the export for roll {roll_name!r}, "
                    f"but this folder is R{folder_roll}. Refusing to mix rolls."
                )
            if camera is None:
                _, camera = resolve_camera(_require(roll, "cameraName", "on the roll"), profile)
                filled.append("camera")
            if stock is None:
                found, _ = resolve_film_stock(roll_name, profile)
                if found:
                    stock = found["name"]
                    filled.append("film stock")
            if year is None:
                dates = sorted(f["date"] for f in roll.get("frames", {}).values() if f.get("date"))
                if dates:
                    year = dates[0][:4]
                    filled.append("year")
                    for fr in frames.values():
                        fr.no_exif = False  # the data exists, in afilm; not an unmarked gap
            if filled:
                print(f"Filled in from {export_path.name}: {', '.join(filled)}.")

    top = max(frames)
    if args.strips:
        try:
            strips = [int(x) for x in args.strips.split(",")]
        except ValueError:
            raise FilmstampError(f"--strips must be comma-separated integers, got {args.strips!r}.") from None
        if min(strips) < 1 or sum(strips) < top:
            raise FilmstampError(f"--strips {args.strips} holds {sum(strips)} frame(s) but the folder has frame {top}.")
    else:
        if camera is None:
            raise FilmstampError(
                f"Camera model {model!r} is not in the profile, so the film format is unknown. "
                "Add the camera, give the cut with --strips, or provide the afilm export with --export."
            )
        per_strip = FRAMES_PER_STRIP.get(str(camera.get("film_format", "")).lower())
        if per_strip is None:
            raise FilmstampError(f"Unsupported film_format {camera.get('film_format')!r} for the strip layout.")
        strips = sheet_strips(top, per_strip)
    pages = sheet_pages(strips)

    date_text = args.date or year or "—"
    title = build_sheet_title(
        (camera or {}).get("model") or model or "—", f"R{folder_roll}", stock, date_text, lab
    )

    gaps = [n for n in range(1, top + 1) if n not in frames]
    wrong = {n: f.wrong_roll for n, f in sorted(frames.items()) if f.wrong_roll}
    print(title)
    print(f"{len(frames)} frame(s), strips {','.join(map(str, strips))}, {len(pages)} page(s).")
    if gaps:
        print(f"Gap in frame numbering, empty cell(s) for: {gaps}")
    for n, roll in wrong.items():
        print(
            f"WARNING: frame {n} ({frames[n].path.name}) carries roll R{roll}, but this folder is R{folder_roll}.",
            file=sys.stderr,
        )
    for n in sorted(n for n, f in frames.items() if f.tier == TIER_TIF):
        print(f"NOTE: frame {n} uses the plain .tif (no -positive.tif or JPG): shown unconverted.")
    for n in sorted(n for n, f in frames.items() if f.no_exif):
        print(f"NOTE: frame {n} has no EXIF capture time: marked on the sheet.")

    data = render_contact_sheet(frames, pages, title, args.subtitle, args.captions)
    tmp = _temp_sibling(out_path)
    try:
        tmp.write_bytes(data)
        tmp.replace(out_path)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"Wrote {out_path}")
    if args.open_pdf:
        open_in_viewer(out_path)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="filmstamp", description="Pre-import metadata stamper for afilm roll exports."
    )
    parser.add_argument(
        "export", type=Path,
        help="afilm roll export (.zip or data.json), or a roll shorthand like 'R49'",
    )
    parser.add_argument(
        "scan_folder", type=Path, nargs="?", default=None,
        help="folder of scanned TIFFs; optional when export is a roll shorthand",
    )
    parser.add_argument("--profile", type=Path, default=default_profile_path())
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--map", type=Path, default=None, dest="map_path")
    parser.add_argument("--tz", default=None, dest="tz_override")
    parser.add_argument("--filter", default=None, dest="filter_name")
    parser.add_argument("--developed-at", default=None, dest="developed_at")
    parser.add_argument("--review", dest="review", action="store_true", default=True)
    parser.add_argument("--no-review", dest="review", action="store_false")
    parser.add_argument("--no-open", dest="open_review", action="store_false", default=True,
                        help="do not open R##.html when it is written")
    parser.add_argument("--write", action="store_true", default=False)
    parser.add_argument("--force", action="store_true", default=False)
    parser.add_argument(
        "--lens", action="append", default=None, dest="lens_specs", metavar="[FRAMES=]LENS",
        help="lens for frames the afilm log has no lens for: '--lens biometar-80' (all of "
             "them) or '--lens 5,6,9=biometar-80'; repeatable. A write refuses to run "
             "while any such frame has none.",
    )
    parser.add_argument("--ext", action="append", default=None)
    parser.add_argument("--include", default=None, help="optional filename glob; no default")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if args.write and not args.review:
        raise FilmstampError(
            "--write refuses to run without --review: a review must be generated for "
            "these same inputs in this same run first. Drop --no-review."
        )
    profile = load_profile(args.profile)
    safety_root_raw = profile.get("safety_root")
    if not safety_root_raw:
        raise FilmstampError("Profile is missing required top-level 'safety_root'.")
    safety_root = Path(safety_root_raw).expanduser()

    export_arg = str(args.export)
    if ROLL_SHORTHAND_RE.match(export_arg):
        export_arg = export_arg.upper()
        downloads_dir_raw = profile.get("downloads_dir")
        if not downloads_dir_raw:
            raise FilmstampError(
                "Profile is missing required top-level 'downloads_dir', needed to "
                f"resolve roll shorthand {export_arg!r}."
            )
        args.export = resolve_export_shorthand(export_arg, Path(downloads_dir_raw).expanduser())
        # An explicitly given scan folder always wins over shorthand resolution.
        if args.scan_folder is None:
            args.scan_folder = resolve_scan_folder_shorthand(export_arg, safety_root)
    elif args.scan_folder is None:
        raise FilmstampError(
            "scan_folder is required unless the export argument is a roll shorthand like 'R49'."
        )

    validate_scan_path(args.scan_folder, safety_root)

    roll, snaps_dir, tmpdir = load_export(args.export)
    try:
        camera_name = _require(roll, "cameraName", "on the roll")
        _, camera = resolve_camera(camera_name, profile)

        frame_numbers = [int(k) for k in roll.get("frames", {})]
        extensions = tuple(
            (e if e.startswith(".") else f".{e}").lower()
            for e in (args.ext or list(DEFAULT_EXTENSIONS))
        )
        mapping, mapping_errors, mapping_warnings = build_mapping(
            args.scan_folder, frame_numbers, args.offset, args.map_path, extensions, args.include
        )
        if mapping_errors:
            for err in mapping_errors:
                print(f"ERROR: {err}", file=sys.stderr)
            return 1

        roll_name = _require(roll, "name", "on the roll")
        stock, stock_warned = resolve_film_stock(roll_name, profile)
        roll_number = parse_roll_number(roll_name)

        roll_defaults = profile.get("roll_defaults", {})
        filter_name = args.filter_name if args.filter_name is not None else roll_defaults.get("filter", "")
        developed_at = args.developed_at if args.developed_at is not None else roll_defaults.get("developed_at", "")

        lens_choices = parse_lens_specs(
            args.lens_specs or [], load_frames(roll), camera, profile
        )
        records, warnings, day_zones = build_frame_records(
            roll, profile, camera, mapping, snaps_dir, args.tz_override, lens_choices
        )
        warnings = mapping_warnings + warnings

        if stock_warned:
            warnings.append(f"Film stock not resolved from roll name {roll_name!r}; keyword omitted.")
        if roll_number is None:
            warnings.append(f"Roll number not found in name {roll_name!r} (expected 'R<digits> ...').")

        keywords = build_keywords(profile, camera, stock, filter_name)

        print_reconciliation_table(roll, roll_number, camera, records, warnings)

        # The write (if any) happens before the review is rendered — the review page
        # is the archival record and must report what actually landed on disk, never
        # a dry-run guess made before the write ran.
        if args.write:
            unpicked = sorted(r.frame_number for r in records if "LENS-FALLBACK" in r.flags)
            if unpicked:
                raise FilmstampError(
                    f"Nothing written: no lens is recorded for frame(s) {unpicked} and none was "
                    "picked. Choose one with --lens (e.g. --lens biometar-80, or "
                    "--lens 5,6=biometar-80). See `filmstamp lenses` for the choices."
                )
        write_result = perform_write(records, camera, profile, keywords, args.force) if args.write else None

        if args.review:
            day_zone_rows = [
                {
                    "day": day.isoformat(),
                    "zone": dz.zone_name,
                    "method": METHOD_LABELS[dz.method],
                    "mismatch": dz.mismatch,
                }
                for day, dz in sorted(day_zones.items())
            ]
            lens_fallback_rows = [
                {"frame_number": r.frame_number, "lens": r.lens["model"]}
                for r in records
                if "LENS-FALLBACK" in r.flags
            ]
            unscanned_frames = sorted(set(frame_numbers) - set(mapping))
            unlogged_frames = sorted(set(mapping) - set(frame_numbers))
            out_path = render_review_html(
                args.scan_folder, args.export, roll, roll_number, camera, stock, keywords,
                records, warnings, filter_name, developed_at,
                expected_frames=camera.get("expected_frames"),
                actual_frame_count=len(roll.get("frames", {})),
                day_zone_rows=day_zone_rows,
                lens_fallback_rows=lens_fallback_rows,
                unscanned_frames=unscanned_frames,
                unlogged_frames=unlogged_frames,
                write_status=build_write_status(write_result),
            )
            if out_path is not None:
                print(f"\nWrote {out_path}")
                if args.open_review:
                    open_in_viewer(out_path)
            else:
                existing = args.scan_folder / review_filename(roll_number, args.scan_folder)
                print(
                    f"\nNo review page produced: {existing.name} "
                    "already records a real write and a dry run never overwrites it."
                )
                saved = _RECORD_FINGERPRINT_RE.search(
                    existing.read_text(encoding="utf-8", errors="replace")
                )
                if saved and saved.group(1) != records_fingerprint(records):
                    print(
                        f"{existing.name} is OUT OF DATE: a fresh check would stamp different "
                        "values than it records (lens, time or camera data changed since the write). "
                        "Re-stamp the roll to bring the files and the record up to date."
                    )

        if write_result is not None:
            print()
            if write_result.skipped_existing:
                print(
                    f"Skipped {len(write_result.skipped_existing)} file(s) that already carry "
                    "DateTimeOriginal (use --force to re-stamp): "
                    f"{[p.name for p in write_result.skipped_existing]}"
                )

            failed = bool(write_result.written_failed or write_result.not_written)
            if failed:
                if write_result.not_written:
                    print(f"NOT WRITTEN ({len(write_result.not_written)} file(s)):", file=sys.stderr)
                    for path in write_result.not_written:
                        print(f"  {path.name}", file=sys.stderr)
                if write_result.written_failed:
                    print(
                        f"WRITTEN BUT FAILED VERIFICATION ({len(write_result.written_failed)} file(s)):",
                        file=sys.stderr,
                    )
                    for path, problems in sorted(write_result.written_failed.items()):
                        for problem in problems:
                            print(f"  {path.name}: {problem}", file=sys.stderr)
                if write_result.exiftool_error:
                    print(f"ExifTool reported an error:\n{write_result.exiftool_error}", file=sys.stderr)
                print(
                    f"Written and verified: {len(write_result.written_verified)} file(s).",
                    file=sys.stderr,
                )
                return 1

            print(
                f"Wrote and verified {len(write_result.written_verified)} file(s); "
                f"{len(write_result.skipped_existing)} already stamped, skipped."
            )

            # Only after a fully verified write, and only from a real export zip.
            if args.export.suffix.lower() == ".zip":
                try:
                    archive_path, status = archive_export(args.export, args.scan_folder, roll_number)
                except (FilmstampError, OSError) as exc:
                    # The write itself is done and verified; say so, with a distinct exit
                    # code, so nobody re-stamps correct files because of a failed copy.
                    print(
                        f"WARNING: all {len(write_result.written_verified)} file(s) were written and "
                        f"verified, but the export could not be archived: {exc}",
                        file=sys.stderr,
                    )
                    return EXIT_ARCHIVE_FAILED
                label = "Already archived" if status == "already-archived" else "Archived export"
                print(f"{label}: {archive_path.name}")
            else:
                print("Export is a bare data.json; nothing to archive (the zip carries the snaps).")

        return 0
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()


# ---------------------------------------------------------------------------
# Back-catalogue audit — READ ONLY.
# Compares what the files already carry with what the afilm export says should be there.
# Nothing here writes to a file; the only subprocess is `exiftool` in read mode.
# ---------------------------------------------------------------------------

AUDIT_GROUPS = [
    ("time", "Capture time"), ("camera", "Camera"), ("body", "Body serial"),
    ("lens", "Lens"), ("lensno", "Lens serial"), ("focal", "Focal length"),
    ("fnum", "Aperture"), ("shutter", "Shutter"), ("iso", "ISO"), ("gps", "GPS"),
    ("caption", "Caption"), ("keywords", "Keywords"), ("identity", "Artist/©"),
]
AUDIT_SYMBOL = {"ok": "✓", "absent": "·", "differs": "≠", "partial": "~", "n/a": "-"}


def _close(got: object, want: float, tol: float, rel: bool = False) -> bool:
    try:
        diff = abs(float(got) - float(want))
    except (TypeError, ValueError):
        return False
    return diff <= (tol * abs(want) if rel else tol)


def audit_statuses(
    record: FrameRecord, camera: dict, identity: dict, keywords: list[str], actual: dict
) -> dict[str, str]:
    """Per tag group: ok / absent / differs / partial / n/a. 'differs' is only ever
    reported — a value in the file may have been corrected on purpose in Lightroom, so
    the file wins."""

    def present(*names: str) -> bool:
        return any(actual.get(n) not in (None, "", []) for n in names)

    def single(names: tuple[str, ...], equal) -> str:
        if not present(*names):
            return "absent"
        return "ok" if equal() else "differs"

    s: dict[str, str] = {}
    s["time"] = single(
        ("DateTimeOriginal",),
        lambda: str(actual.get("DateTimeOriginal")) == record.date_time_original
        and (not actual.get("OffsetTimeOriginal") or str(actual["OffsetTimeOriginal"]) == record.offset_time_original),
    )
    s["camera"] = single(
        ("Make", "Model"),
        lambda: str(actual.get("Make")) == camera["make"] and str(actual.get("Model")) == camera["model"],
    )
    s["body"] = (
        single(("SerialNumber",), lambda: str(actual.get("SerialNumber")) == str(camera["body_serial"]))
        if camera.get("body_serial") else "n/a"
    )
    s["lens"] = single(
        ("LensModel",), lambda: str(actual.get("LensModel")) == (record.lens.get("model") or "")
    ) if record.lens.get("model") else "n/a"
    s["lensno"] = (
        single(("LensSerialNumber",), lambda: str(actual.get("LensSerialNumber")) == str(record.lens["lens_serial"]))
        if record.lens.get("lens_serial") else "n/a"
    )
    s["focal"] = (
        single(("FocalLength",), lambda: _close(actual.get("FocalLength"), record.lens["focal_length_mm"], 0.5))
        if record.lens.get("focal_length_mm") is not None else "n/a"
    )
    s["fnum"] = single(("FNumber",), lambda: _close(actual.get("FNumber"), record.fnumber, 0.05))
    s["shutter"] = single(
        ("ExposureTime",), lambda: _close(actual.get("ExposureTime"), _shutter_seconds(record.exposure_time), 0.02, rel=True)
    )
    s["iso"] = single(("ISO",), lambda: _close(actual.get("ISO"), record.iso, 0.5))
    if record.gps is None:
        s["gps"] = "n/a"
    else:
        s["gps"] = single(
            ("GPSLatitude", "GPSLongitude"),
            lambda: _close(actual.get("GPSLatitude"), record.gps["lat"], 1e-4)
            and _close(actual.get("GPSLongitude"), record.gps["lon"], 1e-4),
        )
    if not record.caption:
        s["caption"] = "n/a"
    else:
        s["caption"] = single(
            ("Caption-Abstract", "Description"),
            lambda: record.caption in (str(actual.get("Caption-Abstract")), str(actual.get("Description"))),
        )
    got = actual.get("Subject") or []
    got = [got] if isinstance(got, str) else list(got)
    if not keywords:
        s["keywords"] = "n/a"
    else:
        have = [k for k in keywords if k in got]
        s["keywords"] = "ok" if len(have) == len(keywords) else ("partial" if have else "absent")
    if not (identity.get("artist") or identity.get("copyright")):
        s["identity"] = "n/a"
    else:
        s["identity"] = single(
            ("Artist", "Copyright"),
            lambda: (not identity.get("artist") or str(actual.get("Artist")) == identity["artist"])
            and (not identity.get("copyright") or str(actual.get("Copyright")) == identity["copyright"]),
        )
    return s


def classify_frame(per_file: list[dict[str, str]]) -> str:
    """full: every applicable group ok or differs, in every file. none: no group present in
    any file. Otherwise partial."""
    cells = [st for statuses in per_file for st in statuses.values() if st != "n/a"]
    if not cells:
        return "none"
    if all(c in ("ok", "differs") for c in cells):
        return "full"
    if all(c == "absent" for c in cells):
        return "none"
    return "partial"


def run_audit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="filmstamp audit",
        description="Report which stamped tags a finished roll's files already carry. Writes nothing.",
    )
    parser.add_argument("roll", help="roll shorthand like 'R49', or the afilm export (.zip / data.json)")
    parser.add_argument("folder", type=Path, nargs="?", default=None)
    parser.add_argument("--profile", type=Path, default=default_profile_path())
    parser.add_argument("--details", action="store_true", help="list every differing or missing tag per file")
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)
    safety_root_raw = profile.get("safety_root")
    if not safety_root_raw:
        raise FilmstampError("Profile is missing required top-level 'safety_root'.")
    safety_root = Path(safety_root_raw).expanduser()

    export_arg = str(args.roll)
    export_path, folder = Path(export_arg), args.folder
    if ROLL_SHORTHAND_RE.match(export_arg):
        shorthand = export_arg.upper()
        downloads = profile.get("downloads_dir")
        if not downloads:
            raise FilmstampError("Profile is missing required top-level 'downloads_dir'.")
        export_path = resolve_export_shorthand(shorthand, Path(downloads).expanduser())
        if folder is None:
            folder = resolve_scan_folder_shorthand(shorthand, safety_root)
    elif folder is None:
        raise FilmstampError("A folder is required unless the first argument is a roll shorthand like 'R49'.")
    validate_scan_path(folder, safety_root)

    folder_match = SHEET_FOLDER_RE.match(folder.resolve().name)
    if not folder_match:
        raise FilmstampError(f"Folder name {folder.resolve().name!r} does not look like 'R<roll>-<lab number>'.")
    folder_roll = str(int(folder_match.group(1)))

    roll, _, tmpdir = load_export(export_path)
    try:
        roll_name = _require(roll, "name", "on the roll")
        export_roll = parse_roll_number(roll_name)
        if export_roll is None or str(int(export_roll)) != folder_roll:
            raise FilmstampError(
                f"{export_path.name} is the export for {roll_name!r}, but this folder is R{folder_roll}. "
                "Refusing to compare different rolls."
            )
        _, camera = resolve_camera(_require(roll, "cameraName", "on the roll"), profile)
        stock, _ = resolve_film_stock(roll_name, profile)
        keywords = build_keywords(profile, camera, stock, profile.get("roll_defaults", {}).get("filter", ""))
        identity = profile.get("identity", {})

        # Files: the roll folder's own images; Export/ and anything that is not an image
        # (.acr, pdf, html, backups) are never looked at.
        by_frame: dict[int, list[Path]] = {}
        wrong_roll: list[tuple[Path, str]] = []
        for p in sorted(folder.iterdir()):
            if not p.is_file() or p.name.startswith(".") or p.suffix.lower() not in SHEET_EXTENSIONS:
                continue
            file_roll, frame = parse_frame_ref(p.name)
            if file_roll != folder_roll:
                wrong_roll.append((p, file_roll))
                continue
            by_frame.setdefault(frame, []).append(p)
        all_files = [p for paths in by_frame.values() for p in paths]
        if not all_files:
            raise FilmstampError(f"No image files for R{folder_roll} found in {folder}.")
        placeholders = sorted(p.name for p in all_files if is_cloud_placeholder(p))
        if placeholders:
            raise FilmstampError(
                "These file(s) are Synology Drive cloud placeholders, not downloaded yet; materialize "
                "them and re-run:\n  " + "\n  ".join(placeholders)
            )

        frame_numbers = {int(k) for k in roll.get("frames", {})}
        logged = {n: p for n, p in by_frame.items() if n in frame_numbers}
        no_afilm = sorted(n for n in by_frame if n not in frame_numbers)
        no_files = sorted(frame_numbers - set(by_frame))

        records, _, _ = build_frame_records(roll, profile, camera, logged, None, None)
        actual = read_tags_for_verification(all_files)
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()

    title = roll_name.strip()
    print(f"AUDIT (read-only) — {title} — {camera.get('model', '?')} — {folder.resolve().name}")
    print("Legend: " + "  ".join(f"{AUDIT_SYMBOL[k]} {k}" for k in ("ok", "absent", "differs", "partial", "n/a")))
    print()
    names = "  ".join(f"{i + 1}={label}" for i, (_, label) in enumerate(AUDIT_GROUPS))
    print(f"Columns: {names}")
    print()
    width = max(len(p.name) for p in all_files)
    totals = {"full": 0, "partial": 0, "none": 0}
    details: list[str] = []
    titles = 0
    for r in records:
        per_file = []
        for path in sorted(r.scan_paths):
            st = audit_statuses(r, camera, identity, keywords, actual.get(path.name, {}))
            per_file.append(st)
            marks = " ".join(AUDIT_SYMBOL[st[g]] for g, _ in AUDIT_GROUPS)
            print(f"{r.frame_number:>3}  {path.name:<{width}}  {marks}")
            if actual.get(path.name, {}).get("Title"):
                titles += 1
            if args.details:
                for g, label in AUDIT_GROUPS:
                    if st[g] in ("absent", "differs", "partial"):
                        details.append(f"  {path.name}: {label} {st[g]}")
        totals[classify_frame(per_file)] += 1

    print()
    print(
        f"{len(records)} frame(s) matched to the afilm log: "
        f"{totals['full']} fully stamped, {totals['partial']} partially stamped, {totals['none']} unstamped."
    )
    if no_afilm:
        print(f"{len(no_afilm)} frame number(s) have files but no afilm frame: {no_afilm}")
    if no_files:
        print(f"{len(no_files)} afilm frame(s) have no file in this folder: {no_files}")
    for p, file_roll in wrong_roll:
        print(f"WARNING: {p.name} carries roll R{file_roll}, but this folder is R{folder_roll}; ignored.", file=sys.stderr)
    print(f"{titles} of {len(all_files)} file(s) already carry an XMP Title.")
    if details:
        print("\nDetails (differs = reported only, never changed):")
        print("\n".join(details))
    print(
        "\nNothing was written. Before ANY later fill-in, in Lightroom: select the roll → "
        "Metadata → Save Metadata to Files (⌘S), so the files hold everything the catalog has; "
        "after it, Metadata → Read Metadata from Files."
    )
    return 0


# ---------------------------------------------------------------------------
# Helpers for the macOS app (and for people): resolve a roll, check the profile
# ---------------------------------------------------------------------------

def validate_profile(profile: dict) -> list[str]:
    """Everything the tool would otherwise only discover mid-run, as a list of problems."""
    problems: list[str] = []
    if not profile.get("safety_root"):
        problems.append("Missing top-level 'safety_root'.")
    tz = profile.get("time", {}).get("timezone")
    if not tz:
        problems.append("Missing [time] timezone.")
    else:
        try:
            ZoneInfo(tz)
        except Exception:
            problems.append(f"[time] timezone {tz!r} is not a valid IANA zone.")
    lenses = profile.get("lenses", {})
    for key, lens in lenses.items():
        if "alias_of" in lens:
            if lens["alias_of"] not in lenses:
                problems.append(f"[lenses.{key!r}] alias_of {lens['alias_of']!r} does not exist.")
        elif not lens.get("model"):
            problems.append(f"[lenses.{key!r}] has no model.")
    for key, cam in profile.get("cameras", {}).items():
        for field in ("make", "model"):
            if not cam.get(field):
                problems.append(f"[cameras.{key!r}] is missing {field}.")
        default_lens = cam.get("default_lens")
        if default_lens and default_lens not in lenses:
            problems.append(f"[cameras.{key!r}] default_lens {default_lens!r} is not in [lenses].")
        if cam.get("film_format") and str(cam["film_format"]).lower() not in FRAMES_PER_STRIP:
            problems.append(
                f"[cameras.{key!r}] film_format {cam['film_format']!r} has no contact-sheet layout "
                f"(known: {', '.join(FRAMES_PER_STRIP)})."
            )
    for key, stock in profile.get("film_stocks", {}).items():
        if not stock.get("name"):
            problems.append(f"[film_stocks.{key!r}] has no name.")
    return problems


def run_check_profile(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="filmstamp check-profile", description="Validate a roll-profile.toml.")
    parser.add_argument("path", type=Path, nargs="?", default=None)
    args = parser.parse_args(argv)
    path = args.path or default_profile_path()
    try:
        profile = load_profile(path)
    except tomllib.TOMLDecodeError as exc:
        print(f"TOML syntax error in {path.name}: {exc}", file=sys.stderr)
        return 1
    problems = validate_profile(profile)
    if problems:
        print(f"{len(problems)} problem(s) in {path.name}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"Profile OK: {len(profile.get('cameras', {}))} camera(s), {len(profile.get('lenses', {}))} "
        f"lens entries, {len(profile.get('film_stocks', {}))} film stock(s)."
    )
    return 0


def run_lenses(argv: list[str]) -> int:
    """Print, as JSON, the lenses a frame with no recorded lens can be given."""
    parser = argparse.ArgumentParser(prog="filmstamp lenses", description="List the profile's lenses.")
    parser.add_argument("--profile", type=Path, default=default_profile_path())
    args = parser.parse_args(argv)
    profile = load_profile(args.profile)
    print(json.dumps([{"key": k, "model": v.get("model", k)} for k, v in concrete_lenses(profile).items()]))
    return 0


def run_resolve(argv: list[str]) -> int:
    """Print, as JSON, which export zip and scan folder a roll shorthand resolves to."""
    parser = argparse.ArgumentParser(prog="filmstamp resolve", description="Resolve R## to its export and folder.")
    parser.add_argument("roll")
    parser.add_argument("--profile", type=Path, default=default_profile_path())
    args = parser.parse_args(argv)
    profile = load_profile(args.profile)
    shorthand = args.roll.upper()
    result: dict = {"roll": shorthand, "export": None, "scan_folder": None, "errors": []}
    if not ROLL_SHORTHAND_RE.match(shorthand):
        result["errors"].append(f"{args.roll!r} is not a roll shorthand like 'R49'.")
    else:
        try:
            downloads = profile.get("downloads_dir")
            if not downloads:
                raise FilmstampError("Profile is missing 'downloads_dir'.")
            result["export"] = str(resolve_export_shorthand(shorthand, Path(downloads).expanduser()))
        except FilmstampError as exc:
            result["errors"].append(str(exc))
        try:
            root = profile.get("safety_root")
            if not root:
                raise FilmstampError("Profile is missing 'safety_root'.")
            result["scan_folder"] = str(resolve_scan_folder_shorthand(shorthand, Path(root).expanduser()))
        except FilmstampError as exc:
            result["errors"].append(str(exc))
    print(json.dumps(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "sheet":
            return run_sheet(parse_sheet_args(argv[1:]))
        if argv and argv[0] == "audit":
            return run_audit(argv[1:])
        if argv and argv[0] == "check-profile":
            return run_check_profile(argv[1:])
        if argv and argv[0] == "lenses":
            return run_lenses(argv[1:])
        if argv and argv[0] == "resolve":
            return run_resolve(argv[1:])
        return run(parse_args(argv))
    except FilmstampError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
