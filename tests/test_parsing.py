import json
import os
import re
import stat
import sys
import subprocess
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import filmstamp as fs

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROFILE_PATH = Path(__file__).resolve().parent.parent / "roll-profile.toml"


@pytest.fixture(autouse=True)
def opened(monkeypatch):
    """Never launch a viewer from the test run; record what would have been opened."""
    calls: list[Path] = []
    monkeypatch.setattr(fs, "open_in_viewer", calls.append)
    return calls


@pytest.fixture
def profile():
    return fs.load_profile(PROFILE_PATH)


@pytest.fixture
def camera(profile):
    return profile["cameras"]["pentacon"]


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_parse_aperture_ok():
    assert fs.parse_aperture("f/5.6") == 5.6
    assert fs.parse_aperture("f/11") == 11.0


def test_parse_aperture_error():
    with pytest.raises(fs.FilmstampError):
        fs.parse_aperture("f/x")


def test_parse_shutter_fraction_passes_through():
    assert fs.parse_shutter("1/250") == "1/250"


def test_parse_shutter_bare_integer_errors():
    with pytest.raises(fs.FilmstampError, match="bare integer, may be a seconds value"):
        fs.parse_shutter("250")


def test_date_and_offset_summer():
    dt = fs.datetime.fromisoformat("2026-08-11T13:07:43.232")
    stamp, offset, subsec = fs.format_capture_time(dt, ZoneInfo("America/Los_Angeles"))
    assert stamp == "2026:08:11 13:07:43"
    assert offset == "-07:00"
    assert subsec == "232"


def test_date_and_offset_winter_dst_regression():
    dt = fs.datetime.fromisoformat("2026-01-11T13:07:43.232")
    _, offset, _ = fs.format_capture_time(dt, ZoneInfo("America/Los_Angeles"))
    assert offset == "-08:00"


def test_null_location_no_gps_tags():
    assert fs.gps_tags(None) is None


def test_empty_lens_falls_back_to_default(camera, profile):
    lens_key, lens, used_fallback = fs.resolve_lens("", camera, profile)
    assert used_fallback is True
    assert lens_key == "biometar-80"
    assert lens["model"] == "Biometar 80mm f/2.8 Zebra"


def test_unmapped_lens_hard_stops(camera):
    bare_profile = {"lenses": {}}
    with pytest.raises(fs.FilmstampError, match="Car Zeiss"):
        fs.resolve_lens("Car Zeiss ", camera, bare_profile)


def test_whitespace_variants_resolve_to_same_lens(camera, profile):
    key1, lens1, _ = fs.resolve_lens("Car Zeiss ", camera, profile)
    key2, lens2, _ = fs.resolve_lens("Car Zeiss", camera, profile)
    assert key1 == key2 == "biometar-80"
    assert lens1 is lens2


def test_corrected_and_legacy_spelling_resolve_to_same_lens(camera, profile):
    key_new, lens_new, _ = fs.resolve_lens("Carl Zeiss", camera, profile)
    key_legacy, lens_legacy, _ = fs.resolve_lens("Car Zeiss ", camera, profile)
    assert key_new == key_legacy == "biometar-80"
    assert lens_new is lens_legacy


def test_expected_frames_is_per_camera(profile):
    pentacon = profile["cameras"]["pentacon"]
    ikonta = profile["cameras"]["zeiss ikon ikonta"]

    def _roll(camera_name, n):
        return {
            "name": "R00 Test",
            "iso": 100,
            "pullPush": "0",
            "cameraName": camera_name,
            "frames": {
                str(i): {
                    "lensName": "" if camera_name == "Zeiss Ikon Ikonta" else "mir-38b f3.5 65mm",
                    "aperture": "f/8",
                    "shutterSpeed": "1/250",
                    "date": "2026-08-11T13:07:43.000",
                    "note": "",
                    "location": None,
                }
                for i in range(1, n + 1)
            },
        }

    ikonta_roll = _roll("Zeiss Ikon Ikonta", 8)
    mapping = {i: [Path(f"f{i}.tif")] for i in range(1, 9)}
    _, warnings, _ = fs.build_frame_records(ikonta_roll, profile, ikonta, mapping, None, None)
    assert not any("expected" in w for w in warnings)

    pentacon_roll = _roll("Pentacon", 8)
    _, warnings, _ = fs.build_frame_records(pentacon_roll, profile, pentacon, mapping, None, None)
    assert any("expected 13" in w for w in warnings)


def test_film_stock_resolution(profile):
    stock, warned = fs.resolve_film_stock("R36 Ilford FP4 ", profile)
    assert warned is False
    assert stock["name"] == "Ilford FP4 Plus 125"

    stock, warned = fs.resolve_film_stock("R55 GP3", profile)
    assert warned is False
    assert stock["name"] == "Shanghai GP3 100"


def test_pull_push_doubles_iso():
    assert fs.parse_iso(100, "1") == 200


def test_burst_flags_both_frames_dont_alter_timestamps(profile, camera):
    roll = {
        "name": "R99 Test",
        "iso": 100,
        "pullPush": "0",
        "cameraName": "Pentacon ",
        "frames": {
            "1": {
                "lensName": "mir-38b f3.5 65mm",
                "aperture": "f/8",
                "shutterSpeed": "1/250",
                "date": "2026-08-11T13:07:43.000",
                "note": "",
                "location": None,
            },
            "2": {
                "lensName": "mir-38b f3.5 65mm",
                "aperture": "f/8",
                "shutterSpeed": "1/250",
                "date": "2026-08-11T13:07:45.000",
                "note": "",
                "location": None,
            },
        },
    }
    mapping = {1: [Path("frame1.tif")], 2: [Path("frame2.tif")]}
    records, _, _ = fs.build_frame_records(roll, profile, camera, mapping, None, None)
    assert "BURST" not in records[0].flags  # first frame has no previous
    assert "BURST" in records[1].flags
    assert records[0].date_time_original == "2026:08:11 13:07:43"
    assert records[1].date_time_original == "2026:08:11 13:07:45"


def test_teleconverter_note_only_touches_caption(profile, camera):
    roll = {
        "name": "R49 Ilford FP4 ",
        "iso": 125,
        "pullPush": "0",
        "cameraName": "Pentacon ",
        "frames": {
            "1": {
                "lensName": "Car Zeiss ",
                "aperture": "f/8",
                "shutterSpeed": "1/250",
                "date": "2026-06-05T08:04:36.423",
                "note": "2x Teleconverter ",
                "location": {"latitude": 42.0, "longitude": -82.5},
            }
        },
    }
    mapping = {1: [Path("frame1.tif")]}
    records, _, _ = fs.build_frame_records(roll, profile, camera, mapping, None, None)
    record = records[0]
    assert record.caption == "2x Teleconverter"
    assert record.fnumber == 8.0
    assert record.lens["focal_length_mm"] == 80  # nominal Biometar 80, unchanged


def test_gps_derives_toronto_with_mismatch_warning(profile, camera):
    roll = {
        "name": "R49 Ilford FP4 ",
        "iso": 125,
        "pullPush": "0",
        "cameraName": "Pentacon ",
        "frames": {
            "1": {
                "lensName": "Car Zeiss",
                "aperture": "f/11",
                "shutterSpeed": "1/250",
                "date": "2026-06-06T06:34:10.531",
                "note": "",
                "location": {"latitude": 43.1, "longitude": -79.1},
            }
        },
    }
    mapping = {1: [Path("frame1.tif")]}
    records, warnings, _ = fs.build_frame_records(roll, profile, camera, mapping, None, None)
    record = records[0]
    assert record.offset_time_original == "-04:00"
    assert "TZ-DERIVED" in record.flags
    assert any("America/Toronto" in w for w in warnings)


# ---------------------------------------------------------------------------
# Integration — the four-roll corpus
# ---------------------------------------------------------------------------

def _extract_fixture(zip_name: str, tmp_path: Path) -> Path:
    dest = tmp_path / "export"
    dest.mkdir()
    with zipfile.ZipFile(FIXTURES / zip_name) as zf:
        zf.extractall(dest)
    return dest / "data.json"


def _fake_safe_root(tmp_path: Path) -> Path:
    return tmp_path / "home" / "Pictures" / "Photos"


def _fake_downloads_dir(tmp_path: Path) -> Path:
    return tmp_path / "home" / "Downloads"


def _profile_for(tmp_path: Path) -> Path:
    """A copy of the real profile with safety_root/downloads_dir repointed at this
    test's tmp tree."""
    text = PROFILE_PATH.read_text(encoding="utf-8")
    text = re.sub(
        r'^safety_root = ".*"$',
        f'safety_root = "{_fake_safe_root(tmp_path)}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r'^downloads_dir = ".*"$',
        f'downloads_dir = "{_fake_downloads_dir(tmp_path)}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    out_path = tmp_path / "roll-profile-test.toml"
    out_path.write_text(text, encoding="utf-8")
    return out_path


def _html_name(data_json: Path, scan_dir: Path) -> str:
    """The record name production would pick for this roll."""
    name = json.loads(data_json.read_text(encoding="utf-8"))["name"]
    return fs.review_filename(fs.parse_roll_number(name), scan_dir)


def _make_scan_folder(
    tmp_path: Path,
    frame_count: int = 13,
    include_linear: bool = False,
    skip_frames: set[int] | None = None,
) -> Path:
    scan_dir = _fake_safe_root(tmp_path) / "Pentacon" / "R99-LabNr"
    scan_dir.mkdir(parents=True)
    skip_frames = skip_frames or set()
    for i in range(1, frame_count + 1):
        if i in skip_frames:
            continue
        # a real (if tiny) TIFF, so `sips` can generate a scan thumbnail from it
        img = Image.new("RGB", (8, 8), color=(120, 80, 40))
        img.save(scan_dir / f"Pentacon Six R99 {i:03d}-positive.tif")
        if include_linear:
            # the linear source scan — same exposure, same frame number
            img.save(scan_dir / f"Pentacon Six R99 {i:03d}.tif")
    return scan_dir


@pytest.mark.parametrize(
    "zip_name",
    ["R55 GP3.zip", "R36 Ilford FP4 .zip", "R49 Ilford FP4 .zip", "R54 Ilford FP4 .zip"],
)
def test_all_four_rolls_pass_clean_dry_run(zip_name, tmp_path):
    data_json = _extract_fixture(zip_name, tmp_path)
    scan_dir = _make_scan_folder(tmp_path)

    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(_profile_for(tmp_path))])
    exit_code = fs.run(args)

    assert exit_code == 0
    review_path = scan_dir / _html_name(data_json, scan_dir)
    assert review_path.exists()
    html = review_path.read_text(encoding="utf-8")
    assert "<table>" in html
    assert "frame-num" in html

    # run-level header block: provenance, not per-row data
    assert "Run provenance" in html
    assert "13 logged / 13 expected for Pentacon Six TL" in html

    # both thumbnails are embedded as base64, side by side — no file:// link
    assert "file://" not in html
    assert html.count("data:image/jpeg;base64,") >= 13  # at least one scan thumb per frame


# ---------------------------------------------------------------------------
# Frame -> multiple files, --include, and partial-scan shortfalls
# ---------------------------------------------------------------------------

def test_frame_maps_to_every_matching_file_by_default(tmp_path):
    # A real roll folder holds both "… 001.tif" (linear source scan) and
    # "… 001-positive.tif" (converted positive) for every frame — the same exposure,
    # stamped identically. Colour rolls have no "-positive" file at all, so there must
    # be no default filter: both files map to the same frame, not an error.
    scan_dir = _make_scan_folder(tmp_path, include_linear=True)
    files = sorted(scan_dir.iterdir())
    assert len(files) == 26  # 13 linear + 13 positive

    frame_numbers = list(range(1, 14))
    mapping, errors, warnings = fs.build_mapping(
        scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS
    )

    assert not errors
    assert not warnings
    assert len(mapping) == 13  # frames, not files
    for frame_no, paths in mapping.items():
        assert len(paths) == 2
        names = {p.name.endswith("-positive.tif") for p in paths}
        assert names == {True, False}


def test_include_glob_selects_only_positives_when_given(tmp_path):
    # --include has no default, but remains available as an explicit filter.
    scan_dir = _make_scan_folder(tmp_path, include_linear=True)
    frame_numbers = list(range(1, 14))
    mapping, errors, warnings = fs.build_mapping(
        scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS, "*-positive.tif"
    )

    assert not errors
    assert not warnings
    assert len(mapping) == 13
    for frame_no, paths in mapping.items():
        assert len(paths) == 1
        assert paths[0].name.endswith("-positive.tif")


def test_include_glob_falls_back_when_it_matches_nothing(tmp_path):
    scan_dir = _make_scan_folder(tmp_path)  # positives only, no "-positive" in this glob
    frame_numbers = list(range(1, 14))
    mapping, errors, warnings = fs.build_mapping(
        scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS, "*-nonexistent-suffix.tif"
    )

    assert not errors
    assert len(mapping) == 13  # fell back to all files matching --ext
    assert any("matched no files" in w for w in warnings)


def test_include_glob_default_end_to_end_via_cli(tmp_path):
    # Exercised through fs.run() against both file variants on disk, using the CLI's
    # actual default (no --include at all): both must appear in the review.
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path, include_linear=True)

    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(_profile_for(tmp_path))])
    exit_code = fs.run(args)

    assert exit_code == 0
    html = (scan_dir / _html_name(data_json, scan_dir)).read_text(encoding="utf-8")
    for i in range(1, 14):
        assert f"Pentacon Six R99 {i:03d}-positive.tif" in html
        assert f"Pentacon Six R99 {i:03d}.tif" in html


def _make_r50_scan_folder(tmp_path: Path, scanned_frames: list[int]) -> Path:
    """R50-shaped: 2 files per scanned frame (source + positive), 11 JSON frames."""
    scan_dir = _fake_safe_root(tmp_path) / "Pentacon" / "R50-LabNr"
    scan_dir.mkdir(parents=True)
    for i in scanned_frames:
        img = Image.new("RGB", (8, 8), color=(60, 60, 60))
        img.save(scan_dir / f"Pentacon Six R50 {i:03d}.tif")
        img.save(scan_dir / f"Pentacon Six R50 {i:03d}-positive.tif")
    return scan_dir


def test_trailing_shortfall_warns_and_stamps_what_is_present(tmp_path):
    # 7 frames x 2 files each (frames 1-7 scanned), 11 JSON frames (8-11 not yet
    # scanned) — a contiguous trailing shortfall: normal, common, not an error.
    scan_dir = _make_r50_scan_folder(tmp_path, list(range(1, 8)))
    frame_numbers = list(range(1, 12))
    mapping, errors, warnings = fs.build_mapping(
        scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS
    )

    assert not errors
    assert any("Partially scanned roll" in w and "[8, 9, 10, 11]" in w for w in warnings)
    assert len(mapping) == 7
    total_files = sum(len(paths) for paths in mapping.values())
    assert total_files == 14


def test_midroll_gap_is_a_hard_error(tmp_path):
    # Same shape, but frame 4 is also missing: the shortfall is no longer a
    # contiguous trailing suffix (it interrupts the 1-7 run), so it must be an error.
    scan_dir = _make_r50_scan_folder(tmp_path, [1, 2, 3, 5, 6, 7])
    frame_numbers = list(range(1, 12))
    mapping, errors, warnings = fs.build_mapping(
        scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS
    )

    assert errors
    assert any("[4, 8, 9, 10, 11]" in e for e in errors)


def test_file_mapping_to_unknown_frame_is_always_a_hard_error(tmp_path):
    # Not a trailing-excess case: 99 isn't contiguous with the last logged frame (11),
    # and frames 8-11 are also missing, so this stays a hard error.
    scan_dir = _make_r50_scan_folder(tmp_path, list(range(1, 8)))
    # a stray file numbered outside the roll's own frame range
    Image.new("RGB", (8, 8), color=(1, 1, 1)).save(scan_dir / "Pentacon Six R50 099.tif")
    frame_numbers = list(range(1, 12))
    mapping, errors, warnings = fs.build_mapping(
        scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS
    )

    assert any("99" in e for e in errors)


def test_trailing_excess_under_logged_roll_warns_and_stamps_logged_frames(tmp_path):
    # Trailing-excess case: files exist for frames 1..11 but the roll
    # only logs 1..8, and every logged frame (1-8) already has a file — an
    # under-logged roll, not a data error.
    scan_dir = _make_r50_scan_folder(tmp_path, list(range(1, 12)))
    frame_numbers = list(range(1, 9))
    mapping, errors, warnings = fs.build_mapping(
        scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS
    )

    assert not errors
    assert any("Not logged: frame(s) [9, 10, 11]" in w for w in warnings)
    # the extra files are still mapped (harmless — build_frame_records never looks
    # at frame numbers outside the roll's own JSON), just never turned into records
    assert set(mapping) == set(range(1, 12))
    for i in range(1, 9):
        assert len(mapping[i]) == 2


def test_trailing_excess_with_midroll_gap_below_m_stays_a_hard_error(tmp_path):
    # Same trailing-excess shape (files for 9-10 beyond the last logged frame, 8),
    # but frame 4 below m is also missing — mid-roll gaps below m stay hard errors
    # even when there's an under-logged tail above m.
    scan_dir = _make_r50_scan_folder(tmp_path, [1, 2, 3, 5, 6, 7, 8, 9, 10])
    frame_numbers = list(range(1, 9))
    mapping, errors, warnings = fs.build_mapping(
        scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS
    )

    assert any("[9, 10]" in e for e in errors)
    assert any("[4]" in e for e in errors)


def _write_r50_json(tmp_path: Path) -> Path:
    frames = {
        str(i): {
            "lensName": "mir-38b f3.5 65mm",
            "aperture": "f/8",
            "shutterSpeed": "1/250",
            "date": f"2026-08-11T13:{i:02d}:00.000",
            "note": "",
            "location": None,
        }
        for i in range(1, 12)
    }
    roll = {
        "name": "R50 Test",
        "iso": 125,
        "pullPush": "0",
        "cameraName": "Pentacon ",
        "note": "",
        "date": "2026-08-11T09:23:49.198",
        "frames": frames,
    }
    path = tmp_path / "r50-data.json"
    path.write_text(json.dumps(roll), encoding="utf-8")
    return path


def test_r50_shaped_partial_roll_writes_and_verifies_fourteen_files(tmp_path):
    data_json = _write_r50_json(tmp_path)
    scan_dir = _make_r50_scan_folder(tmp_path, list(range(1, 8)))
    profile_path = _profile_for(tmp_path)

    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"])
    exit_code = fs.run(args)

    assert exit_code == 0
    stamped = fs.find_already_stamped(sorted(scan_dir.glob("*.tif")))
    assert len(stamped) == 14


def test_r50_shaped_midroll_gap_errors(tmp_path):
    data_json = _write_r50_json(tmp_path)
    scan_dir = _make_r50_scan_folder(tmp_path, [1, 2, 3, 5, 6, 7])  # frame 4 missing
    profile_path = _profile_for(tmp_path)

    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"])
    exit_code = fs.run(args)

    assert exit_code == 1
    assert not (scan_dir / _html_name(data_json, scan_dir)).exists()


def test_under_logged_roll_end_to_end_dates_unlogged_frames(tmp_path):
    # R50 JSON logs 11 frames, but files exist for 13 — frames 12-13 are an
    # under-logged tail: warned, listed in the review header, and given only the
    # date of the last logged frame (no exposure, lens or GPS data).
    data_json = _write_r50_json(tmp_path)
    scan_dir = _make_r50_scan_folder(tmp_path, list(range(1, 14)))
    profile_path = _profile_for(tmp_path)

    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"])
    exit_code = fs.run(args)

    assert exit_code == 0
    stamped = fs.find_already_stamped(sorted(scan_dir.glob("*.tif")))
    assert len(stamped) == 26  # 13 frames x 2 files each

    out = json.loads(subprocess.run(
        ["exiftool", "-json", "-DateTimeOriginal", "-FNumber", "-GPSLatitude", "-LensModel",
         *[str(p) for p in sorted(scan_dir.glob("*012*.tif"))]],
        capture_output=True, text=True, check=True,
    ).stdout)
    for row in out:
        assert row["DateTimeOriginal"] == "2026:08:11 13:11:00"
        assert row["FNumber"] == 8 and "GPSLatitude" not in row and "LensModel" in row

    html = (scan_dir / _html_name(data_json, scan_dir)).read_text(encoding="utf-8")
    assert "Unlogged" in html
    assert "frame(s) 12, 13" in html


def test_unlogged_frame_mid_roll_gets_interpolated_date(tmp_path):
    # Frame 5 was shot and scanned but never entered in afilm. Scans are in order, so
    # it is stamped with a date interpolated between frames 4 (13:04) and 6 (13:06).
    data_json = _write_r50_json(tmp_path)
    roll = json.loads(data_json.read_text(encoding="utf-8"))
    del roll["frames"]["5"]
    roll["frames"]["6"]["date"] = "2026-08-11T13:08:00.000"
    data_json.write_text(json.dumps(roll), encoding="utf-8")
    scan_dir = _make_r50_scan_folder(tmp_path, list(range(1, 12)))
    profile_path = _profile_for(tmp_path)

    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"])
    assert fs.run(args) == 0

    out = json.loads(subprocess.run(
        ["exiftool", "-json", "-DateTimeOriginal", "-Make", "-FNumber",
         *[str(p) for p in sorted(scan_dir.glob("*005*.tif"))]],
        capture_output=True, text=True, check=True,
    ).stdout)
    assert len(out) == 2
    for row in out:
        assert row["DateTimeOriginal"] == "2026:08:11 13:06:00"  # 13:04 + (13:08-13:04)/2
        assert row["Make"] and row["FNumber"] == 8


def test_r55_baseline_details(tmp_path):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile = fs.load_profile(PROFILE_PATH)
    camera = profile["cameras"]["pentacon"]
    roll, snaps_dir, tmpdir = fs.load_export(data_json)
    try:
        frame_numbers = [int(k) for k in roll["frames"]]
        mapping, errors, _ = fs.build_mapping(scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS)
        assert not errors
        records, warnings, _ = fs.build_frame_records(roll, profile, camera, mapping, snaps_dir, None)
    finally:
        if tmpdir:
            tmpdir.cleanup()

    assert len(records) == 13
    by_frame = {r.frame_number: r for r in records}

    # snaps present on 1-7, 9; absent on 8, 10-13
    for n in [1, 2, 3, 4, 5, 6, 7, 9]:
        assert by_frame[n].snap_path is not None, n
    for n in [8, 10, 11, 12, 13]:
        assert by_frame[n].snap_path is None, n

    # GPS blank on 8, 11
    assert "NO-GPS" in by_frame[8].flags
    assert "NO-GPS" in by_frame[11].flags
    for n in [1, 2, 3, 4, 5, 6, 7, 9, 10, 12, 13]:
        assert "NO-GPS" not in by_frame[n].flags

    # frame 1 lens falls back with a warning
    assert "LENS-FALLBACK" in by_frame[1].flags
    assert any("Frame 1" in w and "default_lens" in w for w in warnings)

    # frames 4-5 resolve car zeiss -> biometar-80 via the shipped profile alias
    assert by_frame[4].lens_key == "biometar-80"
    assert by_frame[5].lens_key == "biometar-80"


def test_r36_uniform_lens_and_phoenix_tz(tmp_path):
    data_json = _extract_fixture("R36 Ilford FP4 .zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile = fs.load_profile(PROFILE_PATH)
    camera = profile["cameras"]["pentacon"]
    roll, snaps_dir, tmpdir = fs.load_export(data_json)
    try:
        frame_numbers = [int(k) for k in roll["frames"]]
        mapping, errors, _ = fs.build_mapping(scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS)
        assert not errors
        records, warnings, _ = fs.build_frame_records(roll, profile, camera, mapping, snaps_dir, None)
    finally:
        if tmpdir:
            tmpdir.cleanup()

    assert len(records) == 13
    assert all(r.lens_key == "biometar-80" for r in records)

    no_gps = [r.frame_number for r in records if "NO-GPS" in r.flags]
    assert no_gps == [4, 6, 7, 11]

    # all three days resolve to America/Phoenix (no DST) -> offset always -07:00
    assert all(r.offset_time_original == "-07:00" for r in records)
    assert any("America/Phoenix" in w for w in warnings)


def test_r49_hard_one(tmp_path):
    data_json = _extract_fixture("R49 Ilford FP4 .zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile = fs.load_profile(PROFILE_PATH)
    camera = profile["cameras"]["pentacon"]
    roll, snaps_dir, tmpdir = fs.load_export(data_json)
    try:
        frame_numbers = [int(k) for k in roll["frames"]]
        mapping, errors, _ = fs.build_mapping(scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS)
        assert not errors
        records, warnings, _ = fs.build_frame_records(roll, profile, camera, mapping, snaps_dir, None)
    finally:
        if tmpdir:
            tmpdir.cleanup()

    assert len(records) == 13
    # both whitespace variants collapse to the same lens
    assert all(r.lens_key == "biometar-80" for r in records)

    burst_frames = [r.frame_number for r in records if "BURST" in r.flags]
    assert len(burst_frames) == 7

    # timezone derives to Eastern with a mismatch warning
    assert any("America/Toronto" in w for w in warnings)
    by_frame = {r.frame_number: r for r in records}
    assert by_frame[1].offset_time_original == "-04:00"

    # teleconverter note reaches Caption without touching exposure tags
    assert by_frame[1].caption == "2x Teleconverter"
    assert by_frame[1].fnumber == 8.0
    assert by_frame[1].lens["focal_length_mm"] == 80


def test_r49_note_appears_in_stdout_and_review_table(tmp_path, capsys):
    # Regression: frame notes were reaching FrameRecord.caption correctly but never
    # made it into either rendered output — the stdout table had no Note column at
    # all, and this locks in that both tables actually show it.
    data_json = _extract_fixture("R49 Ilford FP4 .zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile_path = _profile_for(tmp_path)

    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path)])
    exit_code = fs.run(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    stdout_frame1_line = next(line for line in out.splitlines() if line.strip().startswith("1 "))
    assert "2x Teleconverter" in stdout_frame1_line

    html = (scan_dir / _html_name(data_json, scan_dir)).read_text(encoding="utf-8")
    assert '<td class="caption">2x Teleconverter</td>' in html


def test_biometar_post_rename_export_names_resolve(camera, profile):
    # afilm now exports a distinct name per lens after the app-side rename, so no
    # --lens frame-range override is needed to disambiguate — these must resolve
    # directly through the profile alias table instead.
    key_120, lens_120, _ = fs.resolve_lens("Biometar 2.8/120 mm", camera, profile)
    assert key_120 == "biometar-120"
    assert lens_120["make"] == "Carl Zeiss Jena"
    assert lens_120["model"] == "Biometar 120mm f/2.8 Zebra"
    assert lens_120["focal_length_mm"] == 120

    key_80, lens_80, _ = fs.resolve_lens("Biometar 2.8/80", camera, profile)
    assert key_80 == "biometar-80"
    assert lens_80["model"] == "Biometar 80mm f/2.8 Zebra"


def test_r54_mid_roll_lens_change(tmp_path):
    data_json = _extract_fixture("R54 Ilford FP4 .zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile = fs.load_profile(PROFILE_PATH)
    camera = profile["cameras"]["pentacon"]
    roll, snaps_dir, tmpdir = fs.load_export(data_json)
    try:
        frame_numbers = [int(k) for k in roll["frames"]]
        mapping, errors, _ = fs.build_mapping(scan_dir, frame_numbers, 0, None, fs.DEFAULT_EXTENSIONS)
        assert not errors
        records, _, _ = fs.build_frame_records(roll, profile, camera, mapping, snaps_dir, None)
    finally:
        if tmpdir:
            tmpdir.cleanup()

    by_frame = {r.frame_number: r for r in records}
    for n in range(1, 11):
        assert by_frame[n].lens_key == "mir-38b", n
    for n in range(11, 14):
        assert by_frame[n].lens_key == "biometar-80", n


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def test_refuses_path_outside_safe_root(tmp_path):
    safe_root = tmp_path / "home" / "Pictures" / "Photos"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(fs.FilmstampError):
        fs.validate_scan_path(outside, safe_root)


def test_refuses_dotdot_traversal(tmp_path):
    safe_root = tmp_path / "home" / "Pictures" / "Photos"
    safe_root.mkdir(parents=True)
    with pytest.raises(fs.FilmstampError, match=r"\.\."):
        fs.validate_scan_path(safe_root / "Pentacon" / ".." / ".." / "elsewhere", safe_root)


def test_allows_symlinked_safe_root(tmp_path):
    # Photos is itself a symlink into CloudStorage on the real machine —
    # the check must follow it via resolve(), not string-match the raw path.
    real_root = tmp_path / "CloudStorage" / "Photos"
    (real_root / "Pentacon" / "R99").mkdir(parents=True)
    linked_root = tmp_path / "home" / "Pictures" / "Photos"
    linked_root.parent.mkdir(parents=True)
    linked_root.symlink_to(real_root)
    fs.validate_scan_path(linked_root / "Pentacon" / "R99", linked_root)  # must not raise


def test_refuses_nas_path(tmp_path):
    nas_path = tmp_path / "Photography" / "RAW Files" / "roll"
    nas_path.mkdir(parents=True)
    with pytest.raises(fs.FilmstampError):
        fs.validate_scan_path(nas_path, tmp_path)


def test_run_requires_safety_root_in_profile(tmp_path):
    profile_text = re.sub(
        r'^safety_root = ".*"\n', "", PROFILE_PATH.read_text(encoding="utf-8"), flags=re.MULTILINE
    )
    stripped_profile = tmp_path / "no-safety-root.toml"
    stripped_profile.write_text(profile_text, encoding="utf-8")

    scan_dir = _make_scan_folder(tmp_path)
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(stripped_profile)])
    with pytest.raises(fs.FilmstampError, match="safety_root"):
        fs.run(args)


# ---------------------------------------------------------------------------
# Roll-shorthand resolution ("R49" -> export zip + scan folder)
# ---------------------------------------------------------------------------

def _simple_roll_dict(name: str = "R49 Test", n_frames: int = 3) -> dict:
    return {
        "name": name,
        "iso": 125,
        "pullPush": "0",
        "cameraName": "Pentacon ",
        "note": "",
        "date": "2026-08-11T09:23:49.198",
        "frames": {
            str(i): {
                "lensName": "mir-38b f3.5 65mm",
                "aperture": "f/8",
                "shutterSpeed": "1/250",
                "date": f"2026-08-11T13:{i:02d}:00.000",
                "note": "",
                "location": None,
            }
            for i in range(1, n_frames + 1)
        },
    }


def _write_roll_zip(zip_path: Path, roll: dict) -> None:
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("data.json", json.dumps(roll))


def _make_shorthand_scan_dir(tmp_path: Path, folder_name: str, n_frames: int = 3) -> Path:
    scan_dir = _fake_safe_root(tmp_path) / "Pentacon" / folder_name
    scan_dir.mkdir(parents=True)
    for i in range(1, n_frames + 1):
        Image.new("RGB", (8, 8), color=(4, 5, 6)).save(
            scan_dir / f"Pentacon Six R49 {i:03d}-positive.tif"
        )
    return scan_dir


def test_roll_shorthand_resolves_export_and_scan_folder(tmp_path):
    downloads_dir = _fake_downloads_dir(tmp_path)
    downloads_dir.mkdir(parents=True)
    zip_path = downloads_dir / "R49 GP3.zip"
    _write_roll_zip(zip_path, _simple_roll_dict())

    scan_dir = _make_shorthand_scan_dir(tmp_path, "R49-8842")
    profile_path = _profile_for(tmp_path)

    args = fs.parse_args(["R49", "--profile", str(profile_path)])
    exit_code = fs.run(args)

    assert exit_code == 0
    assert args.export == zip_path
    assert args.scan_folder == scan_dir
    record = scan_dir / fs.review_filename(fs.parse_roll_number(_simple_roll_dict()["name"]), scan_dir)
    assert record.exists()
    html = record.read_text(encoding="utf-8")
    assert str(zip_path.resolve()) in html
    assert str(scan_dir.resolve()) in html


@pytest.mark.parametrize("separator", [" ", "_", "."])
def test_roll_shorthand_accepts_documented_separators(tmp_path, separator):
    downloads_dir = _fake_downloads_dir(tmp_path)
    downloads_dir.mkdir(parents=True)
    zip_path = downloads_dir / f"R49{separator}GP3.zip"
    _write_roll_zip(zip_path, _simple_roll_dict())
    scan_dir = _make_shorthand_scan_dir(tmp_path, "R49-8842")
    profile_path = _profile_for(tmp_path)

    args = fs.parse_args(["R49", "--profile", str(profile_path)])
    assert fs.run(args) == 0


def test_roll_shorthand_r4_does_not_match_r49(tmp_path):
    # The separator class is what prevents "R4" from matching "R49 …" — a glob of
    # "R4[ _.]*.zip" must not match "R49 GP3.zip" (no separator between "4" and "9").
    downloads_dir = _fake_downloads_dir(tmp_path)
    downloads_dir.mkdir(parents=True)
    _write_roll_zip(downloads_dir / "R49 GP3.zip", _simple_roll_dict())

    with pytest.raises(fs.FilmstampError) as excinfo:
        fs.resolve_export_shorthand("R4", downloads_dir)
    assert "found 0" in str(excinfo.value)


def test_roll_shorthand_ambiguous_export_exits_with_candidates(tmp_path, capsys):
    downloads_dir = _fake_downloads_dir(tmp_path)
    downloads_dir.mkdir(parents=True)
    _write_roll_zip(downloads_dir / "R49 GP3.zip", _simple_roll_dict())
    _write_roll_zip(downloads_dir / "R49_retry.zip", _simple_roll_dict())
    _make_shorthand_scan_dir(tmp_path, "R49-8842")
    profile_path = _profile_for(tmp_path)

    exit_code = fs.main(["R49", "--profile", str(profile_path)])
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "R49 GP3.zip" in err
    assert "R49_retry.zip" in err


def test_roll_shorthand_ambiguous_scan_folder_exits_with_candidates(tmp_path, capsys):
    downloads_dir = _fake_downloads_dir(tmp_path)
    downloads_dir.mkdir(parents=True)
    _write_roll_zip(downloads_dir / "R49 GP3.zip", _simple_roll_dict())
    _make_shorthand_scan_dir(tmp_path, "R49-8842")
    _make_shorthand_scan_dir(tmp_path, "R49-9911")
    profile_path = _profile_for(tmp_path)

    exit_code = fs.main(["R49", "--profile", str(profile_path)])
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "R49-8842" in err
    assert "R49-9911" in err


def test_roll_shorthand_zero_matches_exits_with_empty_candidates(tmp_path, capsys):
    downloads_dir = _fake_downloads_dir(tmp_path)
    downloads_dir.mkdir(parents=True)  # empty: no export zip at all
    profile_path = _profile_for(tmp_path)

    exit_code = fs.main(["R49", "--profile", str(profile_path)])
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "found 0" in err


def test_explicit_scan_folder_wins_over_shorthand_resolution(tmp_path):
    downloads_dir = _fake_downloads_dir(tmp_path)
    downloads_dir.mkdir(parents=True)
    zip_path = downloads_dir / "R49 GP3.zip"
    _write_roll_zip(zip_path, _simple_roll_dict())

    # two scan-folder candidates would normally be ambiguous, but an explicit
    # scan_folder argument must bypass that resolution entirely
    _make_shorthand_scan_dir(tmp_path, "R49-8842")
    explicit_scan_dir = _make_shorthand_scan_dir(tmp_path, "R49-9911")
    profile_path = _profile_for(tmp_path)

    args = fs.parse_args(["R49", str(explicit_scan_dir), "--profile", str(profile_path)])
    exit_code = fs.run(args)

    assert exit_code == 0
    assert args.export == zip_path
    assert args.scan_folder == explicit_scan_dir


def test_explicit_paths_bypass_shorthand_resolution_entirely(tmp_path):
    # A normal .zip/.json export path must never be treated as shorthand, even if
    # downloads_dir/safety_root would otherwise resolve ambiguously or to nothing.
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile_path = _profile_for(tmp_path)  # downloads_dir doesn't even exist on disk

    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path)])
    exit_code = fs.run(args)

    assert exit_code == 0
    assert args.export == data_json
    assert args.scan_folder == scan_dir


def test_scan_folder_required_without_shorthand(tmp_path):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    profile_path = _profile_for(tmp_path)

    args = fs.parse_args([str(data_json), "--profile", str(profile_path)])
    with pytest.raises(fs.FilmstampError, match="scan_folder is required"):
        fs.run(args)


# ---------------------------------------------------------------------------
# --write
# ---------------------------------------------------------------------------

def test_write_requires_review_in_the_same_run(tmp_path):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    args = fs.parse_args(
        [str(data_json), str(scan_dir), "--profile", str(_profile_for(tmp_path)), "--write", "--lens", "biometar-80", "--no-review"]
    )
    with pytest.raises(fs.FilmstampError, match="review"):
        fs.run(args)
    # refused before touching anything
    assert not (scan_dir / _html_name(data_json, scan_dir)).exists()
    assert not any("_original" in p.name for p in scan_dir.iterdir())


def test_write_aborts_on_midroll_gap(tmp_path):
    # A trailing shortfall is a warning now, so this must be a genuine
    # mid-roll gap to still be a hard error.
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path, skip_frames={6})
    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(_profile_for(tmp_path)), "--write", "--lens", "biometar-80"])
    exit_code = fs.run(args)
    assert exit_code == 1
    assert not (scan_dir / _html_name(data_json, scan_dir)).exists()
    assert not any("_original" in p.name for p in scan_dir.iterdir())


def test_write_stamps_verifies_and_is_idempotent(tmp_path, capsys):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    scans = sorted(scan_dir.glob("*.tif"))
    assert len(scans) == 13

    profile_path = _profile_for(tmp_path)
    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"])
    exit_code = fs.run(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Wrote and verified 13 file(s); 0 already stamped, skipped." in out
    assert "VERIFICATION FAILED" not in out

    # no ExifTool *_original backups are left behind
    assert not any("_original" in p.name for p in scan_dir.iterdir())

    # independent read-back diff: every file actually carries the intended tags,
    # and Title was never written under any code path
    actual = fs.read_tags_for_verification(scans)
    for p in scans:
        assert actual[p.name].get("DateTimeOriginal"), p.name
        assert "Title" not in actual[p.name], p.name

    # a second run without --force must skip every file and write nothing
    args2 = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"])
    exit_code2 = fs.run(args2)
    out2 = capsys.readouterr().out

    assert exit_code2 == 0
    assert "Skipped 13 file(s)" in out2
    assert "Wrote and verified 0 file(s); 13 already stamped, skipped." in out2


def test_write_force_restamp_does_not_duplicate_keywords(tmp_path):
    # Regression: XMP-dc:Subject is a repeated tag ("+="). Re-stamping under --force
    # without clearing it first would append onto the prior run's keywords instead
    # of replacing them.
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile_path = _profile_for(tmp_path)

    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"])
    assert fs.run(args) == 0

    args_force = fs.parse_args(
        [str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80", "--force"]
    )
    assert fs.run(args_force) == 0

    actual = fs.read_tags_for_verification(sorted(scan_dir.glob("*.tif")))
    for name, tags in actual.items():
        subject = tags.get("Subject")
        subject = [subject] if isinstance(subject, str) else subject
        assert subject == ["Shanghai GP3 100", "6x6", "Pentacon Six TL"], name


def test_is_cloud_placeholder_detects_sparse_file_but_not_real_ones(tmp_path):
    real = tmp_path / "real.tif"
    Image.new("RGB", (8, 8), color=(1, 2, 3)).save(real)
    assert fs.is_cloud_placeholder(real) is False

    # A Synology Drive (or iCloud/OneDrive) on-demand placeholder: reports its full
    # remote size but has no data blocks actually allocated on disk.
    placeholder = tmp_path / "placeholder.tif"
    with open(placeholder, "wb") as f:
        f.truncate(real.stat().st_size)
    assert fs.is_cloud_placeholder(placeholder) is True


def test_write_refuses_cloud_placeholder_files(tmp_path):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile_path = _profile_for(tmp_path)

    target = scan_dir / "Pentacon Six R99 001-positive.tif"
    real_size = target.stat().st_size
    target.unlink()
    with open(target, "wb") as f:
        f.truncate(real_size)
    assert fs.is_cloud_placeholder(target)

    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"])
    with pytest.raises(fs.FilmstampError, match="cloud placeholder") as excinfo:
        fs.run(args)
    assert target.name in str(excinfo.value)

    # refused before writing anything, including files that were fully materialized
    assert not any("_original" in p.name for p in scan_dir.iterdir())


def _single_frame_roll(tmp_path: Path, note: str = "", location: dict | None = None) -> Path:
    roll = {
        "name": "R99 Test",
        "iso": 100,
        "pullPush": "0",
        "cameraName": "Pentacon ",
        "note": "",
        "date": "2026-08-11T09:23:49.198",
        "frames": {
            "1": {
                "lensName": "mir-38b f3.5 65mm",
                "aperture": "f/8",
                "shutterSpeed": "1/250",
                "date": "2026-08-11T13:07:43.000",
                "note": note,
                "location": location,
            }
        },
    }
    path = tmp_path / "data.json"
    path.write_text(json.dumps(roll), encoding="utf-8")
    return path


def test_write_survives_multiline_and_leading_dash_notes(tmp_path):
    # Regression: an argfile is one argument per line with no shell quoting, so an
    # inlined value containing a newline used to split into a bogus extra line —
    # and, if that fragment happened to start with '-', a misparsed ExifTool option.
    roll = {
        "name": "R99 Test",
        "iso": 100,
        "pullPush": "0",
        "cameraName": "Pentacon ",
        "note": "",
        "date": "2026-08-11T09:23:49.198",
        "frames": {
            "1": {
                "lensName": "mir-38b f3.5 65mm",
                "aperture": "f/8",
                "shutterSpeed": "1/250",
                "date": "2026-08-11T13:07:43.000",
                "note": "Line one\nLine two",
                "location": None,
            },
            "2": {
                "lensName": "mir-38b f3.5 65mm",
                "aperture": "f/8",
                "shutterSpeed": "1/250",
                "date": "2026-08-11T13:08:43.000",
                "note": "-1 stop underexposed",
                "location": None,
            },
        },
    }
    data_json = tmp_path / "data.json"
    data_json.write_text(json.dumps(roll), encoding="utf-8")
    scan_dir = _make_scan_folder(tmp_path, frame_count=2)
    profile_path = _profile_for(tmp_path)

    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"])
    assert fs.run(args) == 0

    actual = fs.read_tags_for_verification(sorted(scan_dir.glob("*.tif")))
    assert actual["Pentacon Six R99 001-positive.tif"]["Caption-Abstract"] == "Line one\nLine two"
    assert actual["Pentacon Six R99 002-positive.tif"]["Caption-Abstract"] == "-1 stop underexposed"


def test_force_restamp_clears_fields_dropped_from_reexport(tmp_path):
    scan_dir = _make_scan_folder(tmp_path, frame_count=1)
    profile_path = _profile_for(tmp_path)
    scan_file = scan_dir / "Pentacon Six R99 001-positive.tif"

    data_json = _single_frame_roll(
        tmp_path, note="Original note", location={"latitude": 48.2, "longitude": -122.7}
    )
    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"])
    assert fs.run(args) == 0

    before = fs.read_tags_for_verification([scan_file])[scan_file.name]
    assert before["Caption-Abstract"] == "Original note"
    assert before.get("GPSLatitude") is not None

    # re-exported without the note or GPS fix; --force must clear both on the file
    data_json = _single_frame_roll(tmp_path, note="", location=None)
    args2 = fs.parse_args(
        [str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80", "--force"]
    )
    assert fs.run(args2) == 0

    after = fs.read_tags_for_verification([scan_file])[scan_file.name]
    assert "Caption-Abstract" not in after
    assert "Description" not in after
    assert "GPSLatitude" not in after
    assert "GPSLongitude" not in after


def test_map_explains_only_its_own_gap_cleanly(tmp_path):
    # The doc's own example: the lab's scan numbering shifted after frame 4 was
    # never scanned, so the file literally named "004" is really frame 5's exposure.
    scan_dir = _fake_safe_root(tmp_path) / "Pentacon" / "R55-LabNr"
    scan_dir.mkdir(parents=True)
    for i in [1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13]:
        Image.new("RGB", (8, 8), color=(9, 9, 9)).save(
            scan_dir / f"Pentacon Six R55 {i:03d}-positive.tif"
        )
    map_path = tmp_path / "map.toml"
    map_path.write_text('"Pentacon Six R55 004-positive.tif" = 5\n', encoding="utf-8")

    frame_numbers = list(range(1, 14))
    mapping, errors, warnings = fs.build_mapping(
        scan_dir, frame_numbers, 0, map_path, fs.DEFAULT_EXTENSIONS
    )

    assert not errors
    assert not warnings
    assert set(mapping) == set(frame_numbers) - {4}
    assert mapping[5][0].name == "Pentacon Six R55 004-positive.tif"


def test_map_does_not_suppress_an_unrelated_gap(tmp_path):
    # Same map as above (explains frame 4's absence), but frame 6 is also missing
    # for an unrelated reason — that gap must still be a hard error.
    scan_dir = _fake_safe_root(tmp_path) / "Pentacon" / "R55-LabNr"
    scan_dir.mkdir(parents=True)
    for i in [1, 2, 3, 4, 7, 8, 9, 10, 11, 12, 13]:  # 6 missing too
        Image.new("RGB", (8, 8), color=(9, 9, 9)).save(
            scan_dir / f"Pentacon Six R55 {i:03d}-positive.tif"
        )
    map_path = tmp_path / "map.toml"
    map_path.write_text('"Pentacon Six R55 004-positive.tif" = 5\n', encoding="utf-8")

    frame_numbers = list(range(1, 14))
    mapping, errors, warnings = fs.build_mapping(
        scan_dir, frame_numbers, 0, map_path, fs.DEFAULT_EXTENSIONS
    )

    assert any("[6]" in e for e in errors)


def test_write_reports_three_groups_on_partial_batch_failure(tmp_path):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile_path = _profile_for(tmp_path)

    locked = scan_dir / "Pentacon Six R99 005-positive.tif"
    os.chflags(locked, stat.UF_IMMUTABLE)
    try:
        args = fs.parse_args(
            [str(data_json), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"]
        )
        exit_code = fs.run(args)
    finally:
        os.chflags(locked, 0)  # always clear it, even on assertion failure below

    assert exit_code == 1

    # the invariant is that nothing is left unverified — the 12 files ExifTool
    # actually reached were still verified and reported, not silently dropped
    # because a sibling file in the same batch failed
    actual = fs.read_tags_for_verification(sorted(scan_dir.glob("*.tif")))
    assert not actual[locked.name].get("DateTimeOriginal")
    for p in scan_dir.glob("*.tif"):
        if p.name != locked.name:
            assert actual[p.name].get("DateTimeOriginal"), p.name


# ---------------------------------------------------------------------------
# Serials, fixed-lens bodies, R##.html as a permanent record
# ---------------------------------------------------------------------------

def test_serials_written_and_verified_and_omitted_when_unset(tmp_path):
    scan_dir = _make_scan_folder(tmp_path, frame_count=1)
    scan_file = scan_dir / "Pentacon Six R99 001-positive.tif"
    data_json = _single_frame_roll(tmp_path, note="", location=None)
    args = fs.parse_args(
        [str(data_json), str(scan_dir), "--profile", str(_profile_for(tmp_path)), "--write", "--lens", "biometar-80"]
    )
    assert fs.run(args) == 0
    tags = fs.read_tags_for_verification([scan_file])[scan_file.name]
    assert str(tags["SerialNumber"]) == "100001"
    assert "LensSerialNumber" not in tags  # the MIR 38B has no serial in the profile

    # no serial in the profile -> tag absent, not an empty string
    camera = {"make": "M", "model": "X"}
    identity = {}
    rec = _minimal_record()
    rec.lens = {"make": "L", "model": "L1"}
    built = fs.build_frame_tags(rec, camera, identity, [])
    assert built["EXIF:SerialNumber"] == "" and built["EXIF:LensSerialNumber"] == ""


def _minimal_record():
    return fs.FrameRecord(
        frame_number=1, scan_paths=[], snap_path=None,
        date_time_original="2026:08:11 13:07:43", offset_time_original="-07:00",
        subsec_time_original="000", lens_key="k", lens={}, fnumber=8.0,
        exposure_time="1/250", iso=100, gps=None, caption=None, flags=[],
    )


def test_fixed_lens_camera_resolves_default_without_fallback_flag(profile):
    ikonta = profile["cameras"]["zeiss ikon ikonta"]
    key, lens, used_fallback = fs.resolve_lens("", ikonta, profile)
    assert key == "tessar-105"
    assert lens["focal_length_mm"] == 105
    assert lens["lens_serial"] == "200001"
    assert used_fallback is False
    assert ikonta["body_serial"] == "IK0001"


def test_ikonta_camera_name_resolves_from_afilm_string(profile):
    cam_key, camera = fs.resolve_camera("Zeiss Ikon Ikonta", profile)
    assert camera["model"] == "Ikonta 521/2"


def test_review_is_named_for_the_roll(tmp_path):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", str(_profile_for(tmp_path))])
    assert fs.run(args) == 0
    assert (scan_dir / _html_name(data_json, scan_dir)).exists()
    assert not (scan_dir / "review.html").exists()
    assert 'name="filmstamp-record" content="dry-run"' in (scan_dir / _html_name(data_json, scan_dir)).read_text()


def test_dry_run_never_overwrites_a_written_record(tmp_path):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile_path = str(_profile_for(tmp_path))
    assert fs.run(fs.parse_args([str(data_json), str(scan_dir), "--profile", profile_path, "--write", "--lens", "biometar-80"])) == 0
    record = (scan_dir / _html_name(data_json, scan_dir)).read_text()
    assert 'content="written"' in record

    assert fs.run(fs.parse_args([str(data_json), str(scan_dir), "--profile", profile_path])) == 0
    assert (scan_dir / _html_name(data_json, scan_dir)).read_text() == record


def test_second_write_keeps_previous_record_aside(tmp_path):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile_path = str(_profile_for(tmp_path))
    argv = [str(data_json), str(scan_dir), "--profile", profile_path, "--write", "--lens", "biometar-80", "--force"]
    assert fs.run(fs.parse_args(argv)) == 0
    first = (scan_dir / _html_name(data_json, scan_dir)).read_text()
    assert fs.run(fs.parse_args(argv)) == 0

    kept = [p for p in scan_dir.glob("R55.*.html")]
    assert len(kept) == 1 and kept[0].read_text() == first
    assert (scan_dir / _html_name(data_json, scan_dir)).exists()


def test_review_filename_without_roll_number_uses_scan_folder_not_a_constant(tmp_path):
    assert fs.review_filename(None, tmp_path / "Mystery Roll!") == "Mystery_Roll.html"
    assert fs.review_filename(None, tmp_path / "...") == "roll.html"
    assert fs.review_filename("7", tmp_path / "x") == "R7.html"


def test_declined_dry_run_says_so_on_stdout(tmp_path, capsys):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile_path = str(_profile_for(tmp_path))
    assert fs.run(fs.parse_args([str(data_json), str(scan_dir), "--profile", profile_path, "--write", "--lens", "biometar-80"])) == 0
    capsys.readouterr()
    assert fs.run(fs.parse_args([str(data_json), str(scan_dir), "--profile", profile_path])) == 0
    assert "No review page produced" in capsys.readouterr().out


def test_failed_record_write_leaves_existing_record_in_place(tmp_path, monkeypatch):
    out = tmp_path / "R1.html"
    out.write_text('<meta name="filmstamp-record" content="written">', encoding="utf-8")

    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        fs.write_review(out, "<html></html>", "written")
    monkeypatch.undo()
    assert out.exists() and 'content="written"' in out.read_text()
    assert list(tmp_path.glob("R1.*.html")) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["R1.html"]  # no orphaned temp file


# ---------------------------------------------------------------------------
# export archive
# ---------------------------------------------------------------------------

def test_write_archives_the_export_beside_the_scans(tmp_path):
    zip_path = FIXTURES / "R55 GP3.zip"
    data_json_dir = tmp_path
    scan_dir = _make_scan_folder(tmp_path)
    profile_path = str(_profile_for(tmp_path))

    assert fs.run(fs.parse_args([str(zip_path), str(scan_dir), "--profile", profile_path, "--write", "--lens", "biometar-80"])) == 0
    archive = scan_dir / "R55.afilm.zip"
    assert archive.exists()
    assert archive.read_bytes() == zip_path.read_bytes()


def test_dry_run_does_not_archive(tmp_path):
    scan_dir = _make_scan_folder(tmp_path)
    args = fs.parse_args([str(FIXTURES / "R55 GP3.zip"), str(scan_dir), "--profile", str(_profile_for(tmp_path))])
    assert fs.run(args) == 0
    assert list(scan_dir.glob("*.afilm*.zip")) == []


def test_identical_rewrite_does_not_duplicate_archive(tmp_path):
    zip_path = FIXTURES / "R55 GP3.zip"
    scan_dir = _make_scan_folder(tmp_path)
    argv = [str(zip_path), str(scan_dir), "--profile", str(_profile_for(tmp_path)), "--write", "--lens", "biometar-80", "--force"]
    assert fs.run(fs.parse_args(argv)) == 0
    assert fs.run(fs.parse_args(argv)) == 0
    assert [p.name for p in scan_dir.glob("*.afilm*.zip")] == ["R55.afilm.zip"]


def test_differing_reexport_is_kept_alongside_never_overwritten(tmp_path):
    scan_dir = _make_scan_folder(tmp_path)
    first = tmp_path / "first.zip"
    first.write_bytes(b"first export")
    second = tmp_path / "second.zip"
    second.write_bytes(b"second export")

    path1, status1 = fs.archive_export(first, scan_dir, "55")
    path2, status2 = fs.archive_export(second, scan_dir, "55")
    again, status3 = fs.archive_export(second, scan_dir, "55")

    assert (path1.name, status1) == ("R55.afilm.zip", "archived")
    assert status2 == "archived" and path2.name.startswith("R55.afilm.2") and path2 != path1
    assert path1.read_bytes() == b"first export"
    assert (again, status3) == (path2, "already-archived")


def test_failed_verification_archives_nothing(tmp_path):
    scan_dir = _make_scan_folder(tmp_path, skip_frames={6})
    # a mid-roll gap is a hard error, so nothing is written and nothing archived
    args = fs.parse_args([
        str(FIXTURES / "R55 GP3.zip"), str(scan_dir), "--profile", str(_profile_for(tmp_path)), "--write", "--lens", "biometar-80",
    ])
    fs.run(args)
    assert list(scan_dir.glob("*.afilm*.zip")) == []


# ---------------------------------------------------------------------------
# contact sheet (`filmstamp sheet <folder>`)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "n, per, expected",
    [
        (10, 3, [2, 2, 3, 3]),
        (11, 3, [2, 3, 3, 3]),
        (12, 3, [3, 3, 3, 3]),
        (13, 3, [2, 2, 3, 3, 3]),
        (8, 2, [2, 2, 2, 2]),
        (7, 2, [1, 2, 2, 2]),
        (24, 3, [3] * 8),
        (22, 3, [2, 2, 2, 2, 3, 3, 3, 3]) if False else (22, 3, [2, 2, 3, 3, 3, 3, 3, 3]),
    ],
)
def test_sheet_strips_short_strips_first(n, per, expected):
    assert fs.sheet_strips(n, per) == expected
    assert sum(expected) == n


def test_sheet_pages_13_frames_is_one_page_in_reading_order():
    pages = fs.sheet_pages(fs.sheet_strips(13, 3))
    assert pages == [[[1, 2, None], [3, 4, None], [5, 6, 7], [8, 9, 10], [11, 12, 13]]]


def test_sheet_pages_6x9_eight_frames_one_page():
    pages = fs.sheet_pages(fs.sheet_strips(8, 2))
    assert len(pages) == 1 and pages[0] == [[1, 2], [3, 4], [5, 6], [7, 8]]


def test_sheet_pages_longer_rolls_continue_on_a_second_page():
    # 15 frames is the most a single page holds at 3 per strip; 24 frames (220 film) is 8 strips
    assert len(fs.sheet_pages(fs.sheet_strips(15, 3))) == 1
    pages = fs.sheet_pages(fs.sheet_strips(24, 3))
    assert [len(p) for p in pages] == [5, 3]


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Pentacon Six TL R44 003.tif", ("44", 3)),
        ("Pentacon Six R50 001-positive.tif", ("50", 1)),
        ("Between the Viewer and the View R25 006.jpg", ("25", 6)),
    ],
)
def test_parse_frame_ref(name, expected):
    assert fs.parse_frame_ref(name) == expected


@pytest.mark.parametrize("name", ["cover.jpg", "R25 006 and R26 007.jpg"])
def test_parse_frame_ref_ambiguity_is_a_hard_error_naming_the_file(name):
    with pytest.raises(fs.FilmstampError, match=re.escape(name)):
        fs.parse_frame_ref(name)


def test_sheet_title_five_fields_and_missing_lab():
    assert (
        fs.build_sheet_title("Pentacon Six TL", "R46", "Ilford FP4 Plus 125", "2026", "8594")
        == "Pentacon Six TL · R46 · Ilford FP4 Plus 125 · 2026 · Lab 8594"
    )
    assert fs.build_sheet_title("X", "R1", None, "2026", None).endswith("· Lab —")


def _touch_img(path: Path, size=(8, 8)) -> None:
    Image.new("RGB", size, color=(120, 80, 40)).save(path)


def test_select_prefers_positive_then_jpg_then_tif(tmp_path):
    for name in [
        "Pentacon Six R9 001.tif", "Pentacon Six R9 001-positive.tif", "Held In Place R9 001.jpg",
        "Pentacon Six R9 002.tif", "Held In Place R9 002.jpg",
        "Pentacon Six R9 003.tif",
    ]:
        _touch_img(tmp_path / name)
    (tmp_path / "R9.pdf").write_bytes(b"x")
    (tmp_path / "x.acr").write_bytes(b"x")
    (tmp_path / "Export").mkdir()

    chosen = fs.select_sheet_files(tmp_path, "9")
    assert chosen[1].path.name.endswith("001-positive.tif") and chosen[1].tier == fs.TIER_POSITIVE
    assert chosen[2].path.suffix == ".jpg" and chosen[2].tier == fs.TIER_JPG
    assert chosen[3].path.name == "Pentacon Six R9 003.tif" and chosen[3].tier == fs.TIER_TIF


def test_select_flags_wrong_roll_but_keeps_the_frame(tmp_path):
    _touch_img(tmp_path / "Pentacon Six TL R47 001.tif")
    _touch_img(tmp_path / "Pentacon Six TL R44 003.tif")
    chosen = fs.select_sheet_files(tmp_path, "47")
    assert chosen[1].wrong_roll is None
    assert chosen[3].wrong_roll == "44"


def test_select_duplicate_frame_in_same_tier_is_a_hard_error(tmp_path):
    _touch_img(tmp_path / "Pentacon Six R9 001-positive.tif")
    _touch_img(tmp_path / "Other Name R9 001-positive.tif")
    with pytest.raises(fs.FilmstampError, match="ambiguous"):
        fs.select_sheet_files(tmp_path, "9")


def _stamped_roll(tmp_path: Path, frame_count: int = 13) -> tuple[Path, Path]:
    scan_dir = _make_scan_folder(tmp_path, frame_count=frame_count)
    profile_path = _profile_for(tmp_path)
    args = fs.parse_args(
        [str(FIXTURES / "R55 GP3.zip"), str(scan_dir), "--profile", str(profile_path), "--write", "--lens", "biometar-80"]
    )
    assert fs.run(args) == 0
    return scan_dir, profile_path


def _sheet(scan_dir: Path, profile_path: Path, *extra: str) -> int:
    return fs.main(["sheet", str(scan_dir), "--profile", str(profile_path), "--no-open", *extra])


def test_sheet_end_to_end_one_page_and_title(tmp_path, capsys):
    scan_dir, profile_path = _stamped_roll(tmp_path)
    capsys.readouterr()
    assert _sheet(scan_dir, profile_path, "--subtitle", "Test & (Sub)") == 0
    out = capsys.readouterr().out
    assert "Pentacon Six TL · R99 · Shanghai GP3 100 · 2025 · Lab LabNr" in out
    assert "strips 2,2,3,3,3, 1 page(s)" in out

    pdf = (scan_dir / "R99.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    assert pdf.count(b"/Type /Page /") == 1
    assert b"/Count 1" in pdf
    assert pdf.count(b"/Subtype /Image") == 13
    # a real PDF reader (macOS's) can open and rasterise it
    png = tmp_path / "check.png"
    subprocess.run(["sips", "-s", "format", "png", str(scan_dir / "R99.pdf"), "--out", str(png)], check=True, capture_output=True)
    assert png.stat().st_size > 1000


def test_sheet_refuses_to_overwrite_without_force(tmp_path):
    scan_dir, profile_path = _stamped_roll(tmp_path, frame_count=3)
    assert _sheet(scan_dir, profile_path) == 0
    first = (scan_dir / "R99.pdf").read_bytes()
    assert _sheet(scan_dir, profile_path) == 1
    assert (scan_dir / "R99.pdf").read_bytes() == first
    assert _sheet(scan_dir, profile_path, "--force") == 0


def test_sheet_flags_wrong_roll_in_output_and_still_renders(tmp_path, capsys):
    scan_dir, profile_path = _stamped_roll(tmp_path, frame_count=4)
    (scan_dir / "Pentacon Six R99 003-positive.tif").rename(scan_dir / "Pentacon Six TL R44 003-positive.tif")
    capsys.readouterr()
    assert _sheet(scan_dir, profile_path) == 0
    err = capsys.readouterr().err
    assert "frame 3" in err and "R44" in err and "R99" in err
    assert (scan_dir / "R99.pdf").exists()


def test_sheet_notes_gap_in_numbering(tmp_path, capsys):
    scan_dir, profile_path = _stamped_roll(tmp_path, frame_count=5)
    (scan_dir / "Pentacon Six R99 003-positive.tif").unlink()
    capsys.readouterr()
    assert _sheet(scan_dir, profile_path) == 0
    assert "[3]" in capsys.readouterr().out


def test_sheet_on_unstamped_folder_needs_strips_and_marks_no_exif(tmp_path, capsys):
    scan_dir = _make_scan_folder(tmp_path, frame_count=6)
    profile_path = _profile_for(tmp_path)
    assert _sheet(scan_dir, profile_path) == 1
    assert "film format is unknown" in capsys.readouterr().err
    assert _sheet(scan_dir, profile_path, "--strips", "3,3") == 0
    out = capsys.readouterr().out
    assert "no EXIF" in out and "· R99 · — · — · Lab LabNr" in out


def test_sheet_strips_override_must_cover_every_frame(tmp_path, capsys):
    scan_dir, profile_path = _stamped_roll(tmp_path, frame_count=6)
    assert _sheet(scan_dir, profile_path, "--strips", "2,2") == 1
    assert "frame 6" in capsys.readouterr().err


def test_sheet_filename_captions_mode(tmp_path):
    scan_dir, profile_path = _stamped_roll(tmp_path, frame_count=2)
    assert _sheet(scan_dir, profile_path, "--captions", "filename") == 0
    assert (scan_dir / "R99.pdf").exists()


def test_sheet_refuses_folder_outside_safety_root(tmp_path, capsys):
    profile_path = _profile_for(tmp_path)
    outside = tmp_path / "elsewhere" / "R1-1"
    outside.mkdir(parents=True)
    assert _sheet(outside, profile_path) == 1
    assert "outside" in capsys.readouterr().err


def test_sheet_never_modifies_scans(tmp_path):
    scan_dir, profile_path = _stamped_roll(tmp_path, frame_count=3)
    before = {p.name: p.read_bytes() for p in scan_dir.glob("*.tif")}
    assert _sheet(scan_dir, profile_path) == 0
    assert {p.name: p.read_bytes() for p in scan_dir.glob("*.tif")} == before


def test_text_width_and_pdf_escaping():
    assert fs.text_width("", 10) == 0
    assert fs.text_width("A", 10) == pytest.approx(6.67)
    assert fs._pdf_text("a(b)\\c·") == b"a\\(b\\)\\\\c\xb7"


# ---------------------------------------------------------------------------
# Usability: short commands that work from any folder
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shorthand", ["R99", "r99"])
def test_sheet_accepts_roll_shorthand(tmp_path, shorthand):
    scan_dir, profile_path = _stamped_roll(tmp_path, frame_count=3)
    assert fs.main(["sheet", shorthand, "--profile", str(profile_path), "--no-open"]) == 0
    assert (scan_dir / "R99.pdf").exists()


def test_sheet_shorthand_with_no_matching_folder_is_a_clear_error(tmp_path, capsys):
    _, profile_path = _stamped_roll(tmp_path, frame_count=2)
    assert fs.main(["sheet", "R5", "--profile", str(profile_path), "--no-open"]) == 1
    assert "R5" in capsys.readouterr().err


def test_sheet_opens_the_pdf_by_default_and_not_with_no_open(tmp_path, opened):
    scan_dir, profile_path = _stamped_roll(tmp_path, frame_count=2)
    opened.clear()
    assert fs.main(["sheet", str(scan_dir), "--profile", str(profile_path)]) == 0
    assert opened == [scan_dir / "R99.pdf"]
    opened.clear()
    assert _sheet(scan_dir, profile_path, "--force") == 0  # _sheet passes --no-open
    assert opened == []


def test_stamper_opens_the_review_page_unless_told_not_to(tmp_path, opened):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    profile = str(_profile_for(tmp_path))
    opened.clear()
    assert fs.run(fs.parse_args([str(data_json), str(scan_dir), "--profile", profile])) == 0
    assert opened == [scan_dir / "R55.html"]
    opened.clear()
    assert fs.run(fs.parse_args([str(data_json), str(scan_dir), "--profile", profile, "--no-open"])) == 0
    assert opened == []


def test_profile_defaults_to_the_one_beside_the_tool_from_any_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # a folder with no roll-profile.toml
    assert fs.default_profile_path() == Path(fs.__file__).resolve().parent / "roll-profile.toml"
    assert fs.default_profile_path().exists()


def test_launcher_runs_from_any_folder_and_through_a_symlink(tmp_path):
    launcher = Path(fs.__file__).resolve().parent / "filmstamp"
    link = tmp_path / "bin" / "filmstamp"
    link.parent.mkdir()
    link.symlink_to(launcher)
    out = subprocess.run([str(link), "sheet", "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0 and "roll shorthand" in out.stdout


# ---------------------------------------------------------------------------
# Contact sheet for a roll that was never stamped: the afilm export fills the gaps
# ---------------------------------------------------------------------------

def _export_zip(path: Path, name: str = "R99 Ilford FP4 ", n_frames: int = 6) -> Path:
    _write_roll_zip(path, _simple_roll_dict(name=name, n_frames=n_frames))
    return path


def test_sheet_unstamped_roll_uses_archived_export_beside_the_scans(tmp_path, capsys):
    scan_dir = _make_scan_folder(tmp_path, frame_count=6)
    _export_zip(scan_dir / "R99.afilm.zip")
    assert _sheet(scan_dir, _profile_for(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Pentacon Six TL · R99 · Ilford FP4 Plus 125 · 2026 · Lab LabNr" in out
    assert "Filled in from R99.afilm.zip: camera, film stock, year." in out
    assert "no EXIF" not in out
    assert "strips 3,3, 1 page(s)" in out


def test_sheet_unstamped_roll_falls_back_to_the_downloads_export(tmp_path, capsys):
    scan_dir = _make_scan_folder(tmp_path, frame_count=4)
    downloads = _fake_downloads_dir(tmp_path)
    downloads.mkdir(parents=True)
    _export_zip(downloads / "R99 Ilford FP4 .zip", n_frames=4)
    assert _sheet(scan_dir, _profile_for(tmp_path)) == 0
    assert "Filled in from R99 Ilford FP4 .zip" in capsys.readouterr().out


def test_sheet_explicit_export_for_another_roll_is_refused(tmp_path, capsys):
    scan_dir = _make_scan_folder(tmp_path, frame_count=3)
    other = _export_zip(tmp_path / "other.zip", name="R50 Ilford FP4 ")
    assert _sheet(scan_dir, _profile_for(tmp_path), "--export", str(other)) == 1
    assert "Refusing to mix rolls" in capsys.readouterr().err
    assert not (scan_dir / "R99.pdf").exists()


def test_sheet_unstamped_without_any_export_points_at_the_options(tmp_path, capsys):
    scan_dir = _make_scan_folder(tmp_path, frame_count=3)
    assert _sheet(scan_dir, _profile_for(tmp_path)) == 1
    assert "--export" in capsys.readouterr().err


def test_sheet_stamped_files_win_over_the_export(tmp_path, capsys):
    scan_dir, profile_path = _stamped_roll(tmp_path, frame_count=3)  # stamped as Shanghai GP3 100
    _export_zip(scan_dir / "R99.afilm.zip", name="R99 Ilford FP4 ")
    capsys.readouterr()
    assert _sheet(scan_dir, profile_path, "--force") == 0
    out = capsys.readouterr().out
    assert "Shanghai GP3 100" in out and "Ilford FP4" not in out
    assert "Filled in" not in out


# ---------------------------------------------------------------------------
# Helpers for the macOS app: `resolve` and `check-profile`
# ---------------------------------------------------------------------------

def test_shipped_profile_validates(profile):
    assert fs.validate_profile(profile) == []


def test_validate_profile_reports_each_problem():
    bad = {
        "time": {"timezone": "Mars/Olympus"},
        "cameras": {"x": {"make": "M", "default_lens": "nope", "film_format": "35mm"}},
        "lenses": {"a": {"alias_of": "ghost"}, "b": {}},
        "film_stocks": {"s": {}},
    }
    problems = " | ".join(fs.validate_profile(bad))
    for needle in ("safety_root", "Mars/Olympus", "missing model", "default_lens 'nope'",
                   "35mm", "ghost", "has no model", "no name"):
        assert needle in problems, needle


def test_check_profile_command(tmp_path, capsys):
    assert fs.main(["check-profile", str(PROFILE_PATH)]) == 0
    assert "Profile OK" in capsys.readouterr().out
    broken = tmp_path / "broken.toml"
    broken.write_text("[time\ntimezone = 1", encoding="utf-8")
    assert fs.main(["check-profile", str(broken)]) == 1
    assert "TOML syntax error" in capsys.readouterr().err


def test_resolve_command_reports_both_paths_as_json(tmp_path, capsys):
    scan_dir = _make_shorthand_scan_dir(tmp_path, "R49-8842")
    downloads = _fake_downloads_dir(tmp_path)
    downloads.mkdir(parents=True)
    _write_roll_zip(downloads / "R49 GP3.zip", _simple_roll_dict())
    assert fs.main(["resolve", "r49", "--profile", str(_profile_for(tmp_path))]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["roll"] == "R49" and result["errors"] == []
    assert result["export"] == str(downloads / "R49 GP3.zip")
    assert result["scan_folder"] == str(scan_dir)


def test_resolve_command_lists_what_is_missing(tmp_path, capsys):
    _fake_safe_root(tmp_path).mkdir(parents=True)
    assert fs.main(["resolve", "R77", "--profile", str(_profile_for(tmp_path))]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["export"] is None and result["scan_folder"] is None
    assert len(result["errors"]) == 2


def test_temp_files_are_unique_per_call_and_leave_nothing_behind(tmp_path):
    out = tmp_path / "R1.html"
    a, b = fs._temp_sibling(out), fs._temp_sibling(out)
    assert a != b
    a.unlink(); b.unlink()
    fs.write_review(out, "<html></html>", "dry-run")
    fs.write_review(out, "<html></html>", "dry-run")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["R1.html"]


def test_outputs_keep_normal_file_permissions(tmp_path):
    reference = tmp_path / "plain.txt"
    reference.write_text("x")
    normal_mode = reference.stat().st_mode & 0o777

    html = tmp_path / "R1.html"
    fs.write_review(html, "<html></html>", "dry-run")
    zip_src = tmp_path / "src.zip"
    zip_src.write_bytes(b"zip")
    archive, _ = fs.archive_export(zip_src, tmp_path, "1")
    assert html.stat().st_mode & 0o777 == normal_mode
    assert archive.stat().st_mode & 0o777 == normal_mode


def test_sheet_pdf_keeps_normal_file_permissions(tmp_path):
    scan_dir, profile_path = _stamped_roll(tmp_path, frame_count=2)
    assert _sheet(scan_dir, profile_path) == 0
    reference = tmp_path / "plain.txt"
    reference.write_text("x")
    assert (scan_dir / "R99.pdf").stat().st_mode & 0o777 == reference.stat().st_mode & 0o777


def test_failed_archive_after_verified_write_has_its_own_exit_code(tmp_path, monkeypatch, capsys):
    scan_dir = _make_scan_folder(tmp_path)
    profile = str(_profile_for(tmp_path))

    def boom(*a, **k):
        raise fs.FilmstampError("disk is full")

    monkeypatch.setattr(fs, "archive_export", boom)
    code = fs.run(fs.parse_args([str(FIXTURES / "R55 GP3.zip"), str(scan_dir), "--profile", profile, "--write", "--lens", "biometar-80"]))
    err = capsys.readouterr().err
    assert code == fs.EXIT_ARCHIVE_FAILED == 3
    assert "written and verified" in err and "could not be archived" in err
    # the stamping really did land
    actual = fs.read_tags_for_verification(sorted(scan_dir.glob("*.tif")))
    assert all(r.get("DateTimeOriginal") for r in actual.values())


# ---------------------------------------------------------------------------
# Back-catalogue audit (report only)
# ---------------------------------------------------------------------------

def _audit_setup(tmp_path: Path, n: int = 3):
    scan_dir = _make_scan_folder(tmp_path, frame_count=n)
    export = tmp_path / "R99 Ilford FP4 .zip"
    _write_roll_zip(export, _simple_roll_dict(name="R99 Ilford FP4 ", n_frames=n))
    return scan_dir, export, str(_profile_for(tmp_path))


def _audit(export, scan_dir, profile, *extra):
    return fs.main(["audit", str(export), str(scan_dir), "--profile", profile, *extra])


def _stamp(export, scan_dir, profile):
    assert fs.run(fs.parse_args([str(export), str(scan_dir), "--profile", profile, "--write", "--lens", "biometar-80"])) == 0


def _exiftool(*args):
    subprocess.run(["exiftool", "-overwrite_original", *map(str, args)], check=True, capture_output=True)


def test_audit_unstamped_roll_reports_everything_missing_and_changes_nothing(tmp_path, capsys):
    scan_dir, export, profile = _audit_setup(tmp_path)
    before = {p.name: p.read_bytes() for p in scan_dir.iterdir()}
    assert _audit(export, scan_dir, profile) == 0
    out = capsys.readouterr().out
    assert "0 fully stamped, 0 partially stamped, 3 unstamped" in out
    assert "Nothing was written" in out and "Save Metadata to Files" in out
    assert {p.name: p.read_bytes() for p in scan_dir.iterdir()} == before  # no files added or touched


def test_audit_fully_stamped_roll(tmp_path, capsys):
    scan_dir, export, profile = _audit_setup(tmp_path)
    _stamp(export, scan_dir, profile)
    capsys.readouterr()
    assert _audit(export, scan_dir, profile) == 0
    assert "3 fully stamped, 0 partially stamped, 0 unstamped" in capsys.readouterr().out


def test_audit_reports_a_cleared_tag_as_partial(tmp_path, capsys):
    scan_dir, export, profile = _audit_setup(tmp_path)
    _stamp(export, scan_dir, profile)
    _exiftool("-XMP-dc:Subject=", scan_dir / "Pentacon Six R99 002-positive.tif")
    capsys.readouterr()
    assert _audit(export, scan_dir, profile, "--details") == 0
    out = capsys.readouterr().out
    assert "2 fully stamped, 1 partially stamped, 0 unstamped" in out
    assert "002-positive.tif: Keywords absent" in out


def test_audit_reports_but_never_changes_a_differing_value(tmp_path, capsys):
    scan_dir, export, profile = _audit_setup(tmp_path)
    _stamp(export, scan_dir, profile)
    target = scan_dir / "Pentacon Six R99 001-positive.tif"
    _exiftool("-DateTimeOriginal=2020:01:01 00:00:00", target)
    capsys.readouterr()
    assert _audit(export, scan_dir, profile, "--details") == 0
    out = capsys.readouterr().out
    assert "001-positive.tif: Capture time differs" in out
    assert "3 fully stamped" in out  # a deliberate correction is not a gap
    assert fs.read_tags_for_verification([target])[target.name]["DateTimeOriginal"] == "2020:01:01 00:00:00"


def test_audit_counts_files_without_an_afilm_frame_and_flags_wrong_roll(tmp_path, capsys):
    scan_dir, export, profile = _audit_setup(tmp_path)
    _touch_img(scan_dir / "Pentacon Six R99 009-positive.tif")
    _touch_img(scan_dir / "Pentacon Six TL R44 004-positive.tif")
    assert _audit(export, scan_dir, profile) == 0
    cap = capsys.readouterr()
    assert "have files but no afilm frame: [9]" in cap.out
    assert "R44" in cap.err and "ignored" in cap.err


def test_audit_notes_afilm_frames_without_files(tmp_path, capsys):
    scan_dir, _, profile = _audit_setup(tmp_path, n=2)
    export = tmp_path / "five.zip"
    _write_roll_zip(export, _simple_roll_dict(name="R99 Ilford FP4 ", n_frames=5))
    assert _audit(export, scan_dir, profile) == 0
    assert "no file in this folder: [3, 4, 5]" in capsys.readouterr().out


def test_audit_refuses_an_export_from_another_roll(tmp_path, capsys):
    scan_dir, _, profile = _audit_setup(tmp_path)
    other = tmp_path / "other.zip"
    _write_roll_zip(other, _simple_roll_dict(name="R50 Ilford FP4 ", n_frames=3))
    assert _audit(other, scan_dir, profile) == 1
    assert "Refusing to compare different rolls" in capsys.readouterr().err


def test_audit_refuses_folders_outside_the_safety_root(tmp_path, capsys):
    _, export, profile = _audit_setup(tmp_path)
    outside = tmp_path / "elsewhere" / "R99-1"
    outside.mkdir(parents=True)
    assert _audit(export, outside, profile) == 1
    assert "outside" in capsys.readouterr().err


def test_audit_only_ever_runs_exiftool_in_read_mode(tmp_path, monkeypatch):
    scan_dir, export, profile = _audit_setup(tmp_path)
    _stamp(export, scan_dir, profile)
    calls = []
    real_run = fs.subprocess.run

    def spy(cmd, *a, **k):
        calls.append(list(cmd))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(fs.subprocess, "run", spy)
    assert _audit(export, scan_dir, profile) == 0
    assert calls and all(c[0] == "exiftool" for c in calls)
    for cmd in calls:
        assert "-@" not in cmd and not any(re.match(r"^-[\w:-]+[+<]?=", a) for a in cmd[1:])
        assert "-overwrite_original" not in cmd


def test_audit_shorthand_resolves_export_and_folder(tmp_path, capsys):
    scan_dir = _make_scan_folder(tmp_path, frame_count=2)
    downloads = _fake_downloads_dir(tmp_path)
    downloads.mkdir(parents=True)
    _write_roll_zip(downloads / "R99 Ilford FP4 .zip", _simple_roll_dict(name="R99 Ilford FP4 ", n_frames=2))
    assert fs.main(["audit", "r99", "--profile", str(_profile_for(tmp_path))]) == 0
    assert "2 frame(s) matched" in capsys.readouterr().out


# --- lens choice for frames the afilm log has no lens for ---------------------------

def _lens_args(tmp_path):
    data_json = _extract_fixture("R55 GP3.zip", tmp_path)
    scan_dir = _make_scan_folder(tmp_path)
    return data_json, scan_dir, str(_profile_for(tmp_path))


def test_write_refuses_while_a_frame_has_no_lens_picked(tmp_path):
    data_json, scan_dir, profile = _lens_args(tmp_path)
    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", profile, "--write"])
    with pytest.raises(fs.FilmstampError, match="no lens is recorded for frame"):
        fs.run(args)
    assert not any(p.suffix == ".html" for p in scan_dir.iterdir())


def test_dry_run_flags_the_unpicked_lens_but_does_not_fail(tmp_path, capsys):
    data_json, scan_dir, profile = _lens_args(tmp_path)
    assert fs.run(fs.parse_args([str(data_json), str(scan_dir), "--profile", profile, "--no-open"])) == 0
    assert "no lens recorded" in capsys.readouterr().out


def test_picked_lens_is_stamped_and_flagged_chosen(tmp_path, capsys):
    data_json, scan_dir, profile = _lens_args(tmp_path)
    args = fs.parse_args(
        [str(data_json), str(scan_dir), "--profile", profile, "--no-open", "--lens", "1=mir-38b"]
    )
    assert fs.run(args) == 0
    out = capsys.readouterr().out
    assert "LENS-CHOSEN" in out
    assert "no lens recorded" not in out


def test_lens_pick_for_a_frame_that_has_a_lens_is_an_error(tmp_path):
    data_json, scan_dir, profile = _lens_args(tmp_path)
    args = fs.parse_args(
        [str(data_json), str(scan_dir), "--profile", profile, "--lens", "2=mir-38b"]
    )
    with pytest.raises(fs.FilmstampError, match="already have a lens"):
        fs.run(args)


def test_lens_pick_must_be_a_profile_lens(tmp_path):
    data_json, scan_dir, profile = _lens_args(tmp_path)
    args = fs.parse_args([str(data_json), str(scan_dir), "--profile", profile, "--lens", "nonsense"])
    with pytest.raises(fs.FilmstampError, match="not a lens in the profile"):
        fs.run(args)


def test_lenses_subcommand_lists_real_lenses_not_aliases(capsys):
    assert fs.main(["lenses", "--profile", str(PROFILE_PATH)]) == 0
    keys = [row["key"] for row in json.loads(capsys.readouterr().out)]
    assert "biometar-80" in keys and "mir-38b" in keys
    assert "carl zeiss" not in keys


def test_dry_run_says_when_the_saved_record_is_out_of_date(tmp_path, capsys):
    data_json, scan_dir, profile = _lens_args(tmp_path)
    base = [str(data_json), str(scan_dir), "--profile", profile, "--no-open"]
    assert fs.run(fs.parse_args(base + ["--write", "--lens", "biometar-80"])) == 0
    capsys.readouterr()
    # same inputs: record is current
    assert fs.run(fs.parse_args(base + ["--lens", "biometar-80"])) == 0
    assert "OUT OF DATE" not in capsys.readouterr().out
    # a different lens would be stamped now: the saved record no longer matches
    assert fs.run(fs.parse_args(base + ["--lens", "mir-38b"])) == 0
    assert "OUT OF DATE" in capsys.readouterr().out
