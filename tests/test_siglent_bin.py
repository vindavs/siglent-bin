"""Tests for siglent_bin, run against real SDS814X HD captures in fixtures/.

Every fixture is unmodified scope output. Run with `pytest` or directly:
`python3 tests/test_siglent_bin.py`.
"""

import glob
import gzip
import hashlib
import os
import shutil
import stat
import struct
import sys
import tempfile
import warnings

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
FIX = os.path.join(HERE, "fixtures")

import siglent_bin  # noqa: E402


def test_read_single_channel():
    d = siglent_bin.read(os.path.join(FIX, "sds814x_C1_30.bin"))
    assert d["source"] == "C1"
    assert d["digital_enabled"] is False
    assert d["unsupported_sources"] == ()
    assert d["sample_rate"] == 20000.0
    assert d["npoints"] == 100000
    assert d["data_width"] == 1  # 16-bit
    assert d["raw"].dtype == np.uint16
    assert d["values"].dtype == np.float32
    t = siglent_bin.time_axis(d)
    assert t.dtype == np.float64
    assert len(d["values"]) == len(d["raw"]) == len(t)
    assert np.all(np.diff(t) > 0)  # time strictly increasing


def test_raw_code_round_trips_exactly_through_float32_values():
    """Representative SDS814X voltage and current fixtures round-trip to their raw codes."""
    for f, apply_probe in [
        ("sds814x_C1_30.bin", False),
        ("sds814x_C1_30.bin", True),
        ("known3v_probe10x.bin", True),
        ("amps_30a.bin", True),
    ]:
        d = siglent_bin.read(os.path.join(FIX, f), apply_probe=apply_probe)
        v = d["values"].astype(np.float64)  # widen only for the inverse arithmetic
        if apply_probe:
            v = v / d["probe"]
        code = np.round((v + d["voff"]) * d["code_per_div"] / d["vdiv"]) + 32768
        assert np.array_equal(code.astype(np.int64), d["raw"].astype(np.int64)), f


def test_offset_binary_not_signed():
    """Regression guard for the bug this library exists to avoid: samples must be
    read as offset-binary uint16, never signed int16. C3_30 straddles code 32768
    (levels on both sides), so a signed read inverts the trace: the largest uint16
    sample (a 'high' level, code > 32767) becomes the *most negative* int16 sample.
    """
    path = os.path.join(FIX, "sds814x_C3_30.bin")
    d = siglent_bin.read(path)
    assert d["raw"].dtype == np.uint16
    assert d["raw"].min() < 32768 < d["raw"].max()  # genuinely straddles
    with open(path, "rb") as f:
        as_signed = np.frombuffer(f.read(), dtype="<i2", offset=siglent_bin.HEADER_BYTES).astype(
            np.int64
        )
    upper = d["raw"] > 32768  # upper cluster in uint16
    # correct (uint16): upper cluster sits above the lower cluster
    assert d["raw"][upper].mean() > d["raw"][~upper].mean()
    # signed int16 inverts it: those same samples wrap negative, now the lower cluster
    assert as_signed[upper].mean() < as_signed[~upper].mean()


def test_volts_calibration():
    """Locks in the volts formula (esp. the `- vert_offset` sign) against known
    PSU levels at two more vertical settings.

    known4v5_dc: flat 4.5 V DC at 20 mV/div with the window centred on ~4.4 V —
    0 V is far outside the screen, so the large offset term amplifies any sign
    or scaling error (a `+ vert_offset` reading would come out negative).
    known5v_vernier: a 0 -> 5 V edge at a non-1-2-5 vernier setting
    (0.315 V/div) on a slow sparse timebase; both plateaus asserted."""
    v = siglent_bin.read(os.path.join(FIX, "known4v5_dc.bin"))["values"]
    dc = float(np.median(v))
    assert abs(dc - 4.5) < 0.1, f"DC level {dc:.3f} V, expected ~4.5"

    d = siglent_bin.read(os.path.join(FIX, "known5v_vernier.bin"))
    assert d["vdiv"] == 0.315 and d["sample_rate"] == 2000.0
    v = d["values"]
    mid = (v.min() + v.max()) / 2
    low = float(np.median(v[v < mid]))
    high = float(np.median(v[v >= mid]))
    assert abs(low - 0.0) < 0.1, f"low level {low:.3f} V, expected ~0"
    assert abs(high - 5.0) < 0.1, f"high level {high:.3f} V, expected ~5"


def test_probe_rule():
    """`vdiv` is stored pre-probe; volts = (...)·probe. A known 3.0 V level (PSU
    turn-on edge) reads 3 V at both 1x and 10x probe — the multiply is required
    (10x) and correct (1x)."""
    for f in ("known3v_probe1x", "known3v_probe10x"):
        v = siglent_bin.read(os.path.join(FIX, f + ".bin"))["values"]
        high = float(np.median(v[v >= (v.min() + v.max()) / 2]))
        assert abs(high - 3.0) < 0.1, f"{f}: high {high:.3f} V, expected ~3"


def test_amps_unit_and_probe():
    """A channel in amps display carries the 'A' unit descriptor. `probe` still
    multiplies (uniform rule) — here it's the 1/(V/A) factor — and reproduces the
    scope's reading across V/A settings spanning three decades (one ~0.3 V input
    displayed as 0.03 / 0.3 / 30 A)."""
    for f, expect in [("amps_30ma", 0.03), ("amps_300ma", 0.3), ("amps_30a", 30.0)]:
        d = siglent_bin.read(os.path.join(FIX, f + ".bin"))
        assert d["unit"] == "A"
        v = d["values"]
        high = float(np.median(v[v >= (v.min() + v.max()) / 2]))
        assert abs(high - expect) < 0.2 * expect, f"{f}: {high} A, expected ~{expect}"


def test_inverted_capture():
    """Channel-invert has no header flag (it's applied to the samples), so the reader
    reproduces an inverted capture faithfully — a 0 -> +3 V rising edge captured with
    invert on reads back as a 0 -> -3 V falling edge."""
    d = siglent_bin.read(os.path.join(FIX, "inverted_3v.bin"))
    v = d["values"]
    mid = (v.min() + v.max()) / 2
    settled = float(np.median(v[v < mid]))  # post-edge plateau
    assert abs(settled - (-3.0)) < 0.1, f"inverted level {settled:.3f} V, expected ~-3"
    assert v.max() < 0.2  # pre-edge level near 0


def test_cal_square():
    """The scope's own ~1 kHz / 3 V calibration-terminal square, captured at
    1 MSa/s: a dense multi-period waveform checking levels, duty and timing."""
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    v = d["values"]
    assert d["unit"] == "V" and d["sample_rate"] == 1e6
    hi = v >= (v.min() + v.max()) / 2
    assert abs(hi.mean() - 0.5) < 0.02  # ~50% duty
    assert abs(float(np.median(v[hi])) - 3.0) < 0.1
    assert abs(float(np.median(v[~hi])) - 0.0) < 0.1
    edges = np.diff(hi.astype(int)).nonzero()[0]
    period = 2 * np.diff(edges).mean() / d["sample_rate"]
    assert abs(period - 1e-3) < 5e-5  # ~1 kHz


def test_zoom_save():
    """A zoom (Z1) save is structurally a normal single-trace file whose samples
    are a contiguous slice of the parent record. It carries its own timebase
    (zoom_switch/zoom_td_val/zoom_trig_delay_val): this capture's zoom window
    was centred at +15 ms with 2 ms/div, so its time axis must span 5..25 ms.
    The vertical conversion stays the parent channel's (same codes, same volts)."""
    c1 = siglent_bin.read(os.path.join(FIX, "zoom_C1.bin"))
    z1 = siglent_bin.read(os.path.join(FIX, "zoom_Z1.bin"))
    assert not c1["zoom"] and z1["zoom"]
    assert z1["source"] == "C1"  # the zoomed channel
    assert abs(z1["time_div"] - 0.002) < 1e-12
    assert abs(z1["time_delay"] - 0.015) < 1e-12
    assert abs(z1["t0"] - 0.005) < 1e-9  # 15 ms centre - 10 ms half-window
    idx = c1["raw"].tobytes().find(z1["raw"].tobytes())
    assert idx >= 0 and idx % 2 == 0  # contiguous slice of the record
    s = idx // 2
    assert np.array_equal(z1["values"], c1["values"][s : s + len(z1["values"])])


def test_math_channel():
    """A math (F1) save: enable flag, vdiv/vpos and code_per_div come from the
    math header fields, no probe factor applies. Ground truth: F1 was inv(C1+C1)
    of the companion capture, positioned at -20 V, so its values must equal
    -2x the C1 values to within a few counts."""
    f1 = siglent_bin.read(os.path.join(FIX, "math_F1.bin"))
    c1 = siglent_bin.read(os.path.join(FIX, "math_C1.bin"))
    assert f1["source"] == "F1" and f1["unit"] == "V" and f1["probe"] == 1.0
    assert f1["voff"] == -20.0
    assert f1["sample_rate"] == c1["sample_rate"]
    assert len(f1["values"]) == len(c1["values"])
    assert float(np.abs(f1["values"] + 2 * c1["values"]).max()) < 0.01


def test_read_group_rejects_zoom_mix():
    """strict read_group refuses to mix zoom and non-zoom traces even when the
    numeric timebase fields coincide — their time-axis formulas differ, so the
    time arrays would be misaligned."""
    with open(os.path.join(FIX, "zoom_C1.bin"), "rb") as f:
        blob = bytearray(f.read())
    struct.pack_into("<i", blob, 0x08, 0)  # C1 off
    struct.pack_into("<i", blob, 0x0C, 1)  # C2 on -> no duplicate source
    struct.pack_into("<i", blob, 0xAF4, 1)  # mark as the zoom trace
    blob[0x40:0x68] = blob[0x18:0x40]  # give C2 C1's vertical fields
    blob[0xE0:0x108] = blob[0xB8:0xE0]
    blob[0x24C:0x254] = blob[0x244:0x24C]
    blob[0x274:0x278] = blob[0x270:0x274]
    # make the zoom timebase numerically identical to the main one
    blob[0xAF8 : 0xAF8 + 12] = blob[0x19C : 0x19C + 12]
    blob[0xB20 : 0xB20 + 12] = blob[0x1C4 : 0x1C4 + 12]
    path = _tmp_bin(bytes(blob))
    try:
        try:
            siglent_bin.read_group([os.path.join(FIX, "zoom_C1.bin"), path])
            raise AssertionError("expected ValueError for zoom/non-zoom mix")
        except ValueError as e:
            assert "zoom" in str(e)
    finally:
        os.unlink(path)


def test_synthetic_format_variants():
    """Locks the code paths the SDS814X can't produce, using synthetic
    derivations of a real capture (real files from other models still welcome):
    big-endian samples, 8-bit samples, and the CH5-8 header bank."""
    src = os.path.join(FIX, "known3v_probe1x.bin")
    with open(src, "rb") as f:
        good = f.read()
    ref = siglent_bin.read(src)
    codes = np.frombuffer(good, dtype="<u2", offset=siglent_bin.HEADER_BYTES)

    be = bytearray(good)
    be[0x265] = 1  # byte_order = big-endian
    be[0x1000:] = codes.astype(">u2").tobytes()
    path = _tmp_bin(bytes(be))
    try:
        d = siglent_bin.read(path)
        assert d["raw"].dtype == np.uint16
        assert np.array_equal(d["values"], ref["values"])
    finally:
        os.unlink(path)

    b8 = bytearray(good)
    b8[0x264] = 0  # data_width = 8-bit
    struct.pack_into("<i", b8, 0x270, ref["code_per_div"] // 256)  # rescale cpd
    b8[0x1000:] = (codes >> 8).astype("u1").tobytes()
    path = _tmp_bin(bytes(b8))
    try:
        d = siglent_bin.read(path)
        assert len(d["values"]) == len(ref["values"])
        step = abs(ref["vdiv"] / d["code_per_div"] * ref["probe"])  # 1 LSB in volts
        assert float(np.abs(d["values"] - ref["values"]).max()) <= step
        with tempfile.TemporaryDirectory() as td:
            try:
                siglent_bin.write(os.path.join(td, "promoted.bin"), d)
                raise AssertionError("expected 8-bit write to be rejected")
            except ValueError as e:
                assert "only 16-bit" in str(e) and "centred on 128" in str(e)
    finally:
        os.unlink(path)

    c5 = bytearray(good)
    struct.pack_into("<i", c5, 0x08, 0)  # C1 off
    struct.pack_into("<i", c5, 0x404, 1)  # C5 on
    c5[0x414:0x43C] = good[0x18:0x40]  # vdiv DWU -> bank 2
    c5[0x4B4:0x4DC] = good[0xB8:0xE0]  # voff DWU
    c5[0x554:0x55C] = good[0x244:0x24C]  # probe
    c5[0x574:0x578] = good[0x270:0x274]  # code_per_div
    path = _tmp_bin(bytes(c5))
    try:
        d = siglent_bin.read(path)
        assert d["source"] == "C5"
        assert np.array_equal(d["values"], ref["values"])
    finally:
        os.unlink(path)


def test_time_axis_reference_position():
    """A 30% reference places this fixture's ~2 V trigger crossing at t=0."""
    path = os.path.join(FIX, "trigsync_neg100ms.bin")
    d = siglent_bin.read(path, ref_position=30)
    assert d["time_delay"] == -0.1
    assert d["ref_position"] == 30
    t = siglent_bin.time_axis(d)
    i0 = int(np.argmin(np.abs(t)))  # sample where t = 0
    assert i0 == 4000
    v = d["values"]
    edge = np.where((v[:-1] <= 2.0) & (v[1:] > 2.0))[0]
    assert abs(int(edge[0]) - i0) <= 10  # t = 0 really is the trigger
    assert 1.5 < v[i0] < 2.5  # trigger level ~2 V, mid-edge
    assert v[i0 - 200] < 1.0 < 4.0 < v[i0 + 500]  # edge brackets the trigger

    # The 50% default misplaces this 30%-reference capture.
    d50 = siglent_bin.read(path)
    assert d50["ref_position"] == 50.0
    t50 = siglent_bin.time_axis(d50)
    assert int(np.argmin(np.abs(t50))) == 6000
    # Only the offset moves; spacing is identical, so relative timing is safe.
    assert np.allclose(np.diff(t), np.diff(t50))


def test_ref_position_shifts_the_axis_linearly():
    path = os.path.join(FIX, "trigsync_neg100ms.bin")
    a = siglent_bin.read(path, ref_position=0)
    b = siglent_bin.read(path, ref_position=25)
    span = a["time_div"] * a["grid"]
    assert np.allclose(siglent_bin.time_axis(a) - siglent_bin.time_axis(b), 0.25 * span)


def test_read_rejects_invalid_ref_position():
    path = os.path.join(FIX, "trigsync_neg100ms.bin")
    for value in (-1, 101, float("nan"), float("inf"), True, "50"):
        try:
            siglent_bin.read(path, ref_position=value)
            raise AssertionError(f"expected ValueError for ref_position={value!r}")
        except ValueError as e:
            assert "ref_position" in str(e) and "0 to 100" in str(e)


def test_read_group_passes_ref_position():
    paths = [os.path.join(FIX, f"sds814x_C{c}_30.bin") for c in (1, 2)]
    g = siglent_bin.read_group(paths, ref_position=30)
    assert all(d["ref_position"] == 30 for d in g.values())
    centre = siglent_bin.read_group(paths)
    span = g["C1"]["time_div"] * g["C1"]["grid"]
    axis_centre = siglent_bin.time_axis(centre["C1"])
    axis_30 = siglent_bin.time_axis(g["C1"])
    assert np.allclose(axis_centre - axis_30, -0.20 * span)


def test_si_prefix():
    assert siglent_bin._si(20000) == (20, "k")
    assert siglent_bin._si(1e6) == (1, "M")
    assert siglent_bin._si(2.5e9) == (2.5, "G")
    assert siglent_bin._si(500) == (500, "")


def test_raw_uint16_helper():
    samples, fs = siglent_bin.raw_uint16(os.path.join(FIX, "sds814x_C2_30.bin"))
    assert samples.dtype == np.uint16
    assert fs == 20000.0


def test_find_group():
    paths = siglent_bin.find_group(FIX, 30)
    assert len(paths) == 4
    assert [os.path.basename(p) for p in paths] == [
        "sds814x_C1_30.bin",
        "sds814x_C2_30.bin",
        "sds814x_C3_30.bin",
        "sds814x_C4_30.bin",
    ]


def test_find_group_supports_gzip_and_rejects_duplicate_channels():
    source = os.path.join(FIX, "known3v_probe1x.bin")
    with tempfile.TemporaryDirectory() as td:
        plain = os.path.join(td, "cap_C1_7.bin")
        compressed = os.path.join(td, "cap_C2_7.bin.gz")
        shutil.copyfile(source, plain)
        with open(source, "rb") as src, gzip.open(compressed, "wb") as dst:
            shutil.copyfileobj(src, dst)
        assert siglent_bin.find_group(td, 7) == [plain, compressed]

        duplicate = os.path.join(td, "cap_C1_7.bin.gz")
        with open(source, "rb") as src, gzip.open(duplicate, "wb") as dst:
            shutil.copyfileobj(src, dst)
        try:
            siglent_bin.find_group(td, 7)
            raise AssertionError("expected duplicate plain/gzip channel rejection")
        except ValueError as e:
            assert "channel C1 appears in both" in str(e)


def test_read_group_aligned():
    chans = siglent_bin.read_group(siglent_bin.find_group(FIX, 30))
    assert sorted(chans) == ["C1", "C2", "C3", "C4"]
    rates = {d["sample_rate"] for d in chans.values()}
    npts = {d["npoints"] for d in chans.values()}
    assert rates == {20000.0} and npts == {100000}
    # shared time base
    axis0 = siglent_bin.time_axis(chans["C1"])
    for c in chans:
        assert np.array_equal(siglent_bin.time_axis(chans[c]), axis0)


def _tmp_bin(blob):
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
        tf.write(blob)
    return tf.name


def test_malformed_files_rejected():
    """Structurally broken files fail with a clear ValueError, not a numpy/struct
    traceback: too short, wrong version, data offset past EOF, truncated or
    missing sample data."""
    with open(os.path.join(FIX, "sds814x_C1_30.bin"), "rb") as f:
        good = f.read()
    cases = {
        "too short": b"\x00" * 100,
        "wrong version": b"\x07" + good[1:],
        "data offset past EOF": good[:4] + struct.pack("<i", len(good) + 1) + good[8:],
        "truncated sample data": good[:-1001],
        "no sample data": good[: siglent_bin.HEADER_BYTES],
        "NaN vdiv": good[:0x18] + struct.pack("<d", float("nan")) + good[0x20:],
        "negative vdiv": good[:0x18] + struct.pack("<d", -0.5) + good[0x20:],
        "zero probe": good[:0x244] + struct.pack("<d", 0.0) + good[0x24C:],
        "magnitude index out of range": good[:0x20] + struct.pack("<i", 999) + good[0x24:],
        "zero grid": good[:0x26C] + struct.pack("<i", 0) + good[0x270:],
        "invalid zoom_switch": good[:0xAF4] + struct.pack("<i", 2) + good[0xAF8:],
        "invalid enable flag": good[:0x08] + struct.pack("<i", 2) + good[0x0C:],
        "invalid digital enable flag": good[:0x158] + struct.pack("<i", 2) + good[0x15C:],
    }
    for what, blob in cases.items():
        path = _tmp_bin(blob)
        try:
            siglent_bin.read(path)
            raise AssertionError(f"{what}: expected ValueError")
        except ValueError:
            pass
        finally:
            os.unlink(path)


def test_read_group_rejects_axis_incompatible_files():
    """Strict read_group rejects files that cannot share one time axis."""
    with open(os.path.join(FIX, "sds814x_C1_30.bin"), "rb") as f:
        blob = bytearray(f.read())
    struct.pack_into("<d", blob, 0x19C, struct.unpack_from("<d", blob, 0x19C)[0] * 2)
    path = _tmp_bin(bytes(blob))
    group = [path, os.path.join(FIX, "sds814x_C2_30.bin")]
    try:
        try:
            siglent_bin.read_group(group)
            raise AssertionError("expected ValueError for mismatched time_div")
        except ValueError as e:
            assert "time_div" in str(e) and "axis-incompatible" in str(e)
        assert sorted(siglent_bin.read_group(group, strict=False)) == ["C1", "C2"]
    finally:
        os.unlink(path)


# Historical float64-axis hashes generated at commit 37e55a35 from
# `read(...)["time"].tobytes()`. Do not regenerate them from current code.
_PRE_MIGRATION_AXIS = [
    ("sds814x_C1_30.bin", "b2270304868bafd1773144d7c136afbe", 100000),
    ("cal_square_1khz.bin", "e7b82e7b8255fed947e58095376a2ec8", 5000),
    ("trigsync_neg100ms.bin", "84606c2c4d168d4dc6e62d5843abfa4c", 10000),
    ("zoom_Z1.bin", "4bbfca8e2e4fee5ba362b309ac5acf59", 200),
]


def test_time_axis_matches_the_pre_migration_axis_bit_for_bit():
    """Cover default, non-default-reference and zoom time-axis paths."""
    for name, want_hash, want_len in _PRE_MIGRATION_AXIS:
        d = siglent_bin.read(os.path.join(FIX, name))
        t = siglent_bin.time_axis(d)
        assert t.dtype == np.float64, (name, t.dtype)
        assert len(t) == want_len, (name, len(t))
        got = hashlib.sha256(np.ascontiguousarray(t).tobytes()).hexdigest()[:32]
        assert got == want_hash, f"{name}: axis changed since the migration ({got})"


def test_t0_is_a_double_precision_scalar():
    """Keep ``t0`` a Python float; float32 timestamps collide at deep-memory rates."""
    d = siglent_bin.read(os.path.join(FIX, "sds814x_C1_30.bin"))
    assert isinstance(d["t0"], float), type(d["t0"])
    assert float(np.float32(d["t0"])) != d["t0"]


def _fixtures():
    return sorted(glob.glob(os.path.join(FIX, "*.bin")))


def test_write_round_trips_every_analog_fixture():
    """Round-trip the supported fields and samples of every analog fixture."""
    checked = 0
    scalars = [
        "source",
        "sample_rate",
        "time_div",
        "time_delay",
        "grid",
        "npoints",
        "data_width",
        "vdiv",
        "voff",
        "code_per_div",
        "probe",
        "unit",
        "unit_raw",
        "zoom",
        "t0",
    ]
    with tempfile.TemporaryDirectory() as td:
        for src in _fixtures():
            d = siglent_bin.read(src)
            if not d["source"].startswith("C") or d["zoom"]:
                continue  # math and zoom are refused by design, covered below
            out = os.path.join(td, os.path.basename(src))
            siglent_bin.write(out, d)
            r = siglent_bin.read(out)
            for k in scalars:
                assert r[k] == d[k], f"{os.path.basename(src)}: {k} {d[k]!r} -> {r[k]!r}"
            assert np.array_equal(r["raw"], d["raw"]), src
            assert np.array_equal(r["values"], d["values"]), src
            assert r["values"].dtype == np.float32
            checked += 1
    assert checked >= 10, f"only {checked} analog fixtures exercised"


def test_write_preserves_an_amps_unit_descriptor():
    """Preserve an amps descriptor instead of defaulting to volts."""
    d = siglent_bin.read(os.path.join(FIX, "amps_300ma.bin"))
    assert d["unit"] == "A"
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "a.bin")
        siglent_bin.write(out, d)
        r = siglent_bin.read(out)
        assert r["unit"] == "A"
        assert r["unit_raw"] == d["unit_raw"] == (0, 0, 1, 1, 1, 0, 1)


def test_write_accepts_a_fetch_shaped_frame():
    """Accept a live frame with a unit label and integral float code_per_div."""
    frame = {
        "source": "C3",
        "sample_rate": 1e7,
        "time_div": 1e-2,
        "time_delay": 5.5e-2,
        "t0": -1.2e-2,
        "grid": 10,
        "npoints": 8,
        "data_width": 1,
        "vdiv": 0.2,
        "voff": -0.5,
        "code_per_div": 7680.0,  # float, as the live descriptor reports it
        "probe": 10.0,
        "unit": "V",
        "raw": np.array([0, 1, 32767, 32768, 32769, 40000, 65535, 100], dtype=np.uint16),
    }
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "live.bin")
        siglent_bin.write(out, frame)
        r = siglent_bin.read(out)
        assert r["source"] == "C3"
        assert r["code_per_div"] == 7680 and isinstance(r["code_per_div"], int)
        assert r["unit"] == "V"
        assert r["npoints"] == 8
        assert np.array_equal(r["raw"], frame["raw"])
        assert r["vdiv"] == 0.2 and r["voff"] == -0.5 and r["probe"] == 10.0
        assert np.isclose(r["sample_rate"], 1e7)
        assert abs(r["t0"] - frame["t0"]) < 1e-17
        assert r["time_delay"] == frame["t0"] + 0.5 * frame["time_div"] * frame["grid"]


def test_write_round_trips_all_eight_channel_slots():
    """Place C1-C8 in the correct header bank and slot."""
    base = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    with tempfile.TemporaryDirectory() as td:
        for n in range(1, 9):
            t = dict(base, source=f"C{n}")
            out = os.path.join(td, f"c{n}.bin")
            siglent_bin.write(out, t)
            assert siglent_bin.read(out)["source"] == f"C{n}"


def test_write_refuses_math_and_zoom_traces():
    for name, needle in (("math_F1.bin", "C1-C8"), ("zoom_Z1.bin", "zoom")):
        d = siglent_bin.read(os.path.join(FIX, name))
        with tempfile.TemporaryDirectory() as td:
            try:
                siglent_bin.write(os.path.join(td, "x.bin"), d)
                raise AssertionError(f"expected ValueError writing {name}")
            except ValueError as e:
                assert needle in str(e), str(e)


def test_write_refuses_an_unknown_unit_rather_than_assuming_volts():
    """Reject an unknown unit instead of assuming volts."""
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    d.pop("unit_raw")
    d["unit"] = None
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "u.bin")
        try:
            siglent_bin.write(out, d)
            raise AssertionError("expected ValueError for an unknown unit")
        except ValueError as e:
            assert "Volts is not assumed" in str(e)
        # An explicit unit remains valid.
        siglent_bin.write(out, d, unit="A")
        assert siglent_bin.read(out)["unit"] == "A"


def test_write_refuses_a_fractional_code_per_div():
    """Reject a fractional live code_per_div that the int32 header cannot store."""
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    d["code_per_div"] = 7680.5
    with tempfile.TemporaryDirectory() as td:
        try:
            siglent_bin.write(os.path.join(td, "f.bin"), d)
            raise AssertionError("expected ValueError for a fractional code_per_div")
        except ValueError as e:
            assert "integer" in str(e)


def test_write_rejects_inconsistent_or_unstorable_samples():
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "x.bin")
        try:
            siglent_bin.write(out, dict(d, npoints=d["npoints"] + 1))
            raise AssertionError("expected ValueError for an npoints mismatch")
        except ValueError as e:
            assert "npoints says" in str(e)
        try:
            siglent_bin.write(out, dict(d, raw=np.array([70000]), npoints=1))
            raise AssertionError("expected ValueError for out-of-range samples")
        except ValueError as e:
            assert "uint16" in str(e)
        try:  # `values` passed where `raw` belongs would truncate silently
            siglent_bin.write(out, dict(d, raw=d["values"]))
            raise AssertionError("expected ValueError for float samples")
        except ValueError as e:
            assert "not its `values`" in str(e)


def test_write_canonicalises_reference_position_and_preserves_t0():
    """The format has no ref-position field. The writer canonicalises the delay
    to the reader's 50% convention so the file remains self-contained."""
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"), ref_position=30.0)
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "r.bin")
        siglent_bin.write(out, d)
        r = siglent_bin.read(out)
        assert r["t0"] == d["t0"]
        assert np.array_equal(siglent_bin.time_axis(r), siglent_bin.time_axis(d))
        assert r["time_delay"] != d["time_delay"]


def test_write_validates_every_stored_numeric_before_opening_destination():
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    bad = {
        "sample_rate": float("nan"),
        "time_div": 0,
        "time_delay": float("inf"),
        "t0": float("nan"),
        "vdiv": -1,
        "voff": float("nan"),
        "probe": 0,
        "grid": 10.9,
        "code_per_div": -1,
        "data_width": 0,
    }
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "must-not-exist.bin")
        for key, value in bad.items():
            try:
                siglent_bin.write(out, dict(d, **{key: value}))
                raise AssertionError(f"expected {key}={value!r} to be rejected")
            except ValueError as e:
                assert key in str(e) or (key == "t0" and "time_delay" in str(e)), str(e)
            assert not os.path.exists(out), f"{key} opened the destination before validation"

        try:
            siglent_bin.write(out, dict(d, code_per_div=1 << 1024))
            raise AssertionError("expected an overflowing int to be rejected")
        except ValueError as e:
            assert "code_per_div" in str(e)
        assert not os.path.exists(out)


def test_write_validates_unit_descriptor_without_coercion():
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    for raw in ((0, 1, 1), (0, 1.5, 1, 0, 1, 0, 1), (0, 1 << 40, 1, 0, 1, 0, 1)):
        with tempfile.TemporaryDirectory() as td:
            try:
                siglent_bin.write(os.path.join(td, "u.bin"), dict(d, unit_raw=raw))
                raise AssertionError(f"expected unit_raw {raw!r} to be rejected")
            except ValueError as e:
                assert "unit_raw" in str(e)


def test_write_populates_documented_data_with_unit_descriptors():
    d = siglent_bin.read(os.path.join(FIX, "amps_300ma.bin"))
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "units.bin")
        siglent_bin.write(out, d)
        with open(out, "rb") as f:
            h = f.read(siglent_bin.HEADER_BYTES)

        def descriptor(a):
            return struct.unpack_from("<7i", h, a + 0x0C)

        assert descriptor(0x18) == descriptor(0xB8) == d["unit_raw"]
        assert descriptor(0x19C) == descriptor(0x1C4) == (0, 0, 1, 0, 1, 1, 1)
        assert descriptor(0x1F0)[0] == 7  # named unit "Sa"


def test_gzip_is_transparent_both_ways():
    """Read and write gzip through the normal API."""
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    with tempfile.TemporaryDirectory() as td:
        plain = os.path.join(td, "a.bin")
        gz = os.path.join(td, "a.bin.gz")
        siglent_bin.write(plain, d)
        siglent_bin.write(gz, d)
        assert os.path.getsize(gz) < os.path.getsize(plain)
        r = siglent_bin.read(gz)
        assert np.array_equal(r["raw"], d["raw"])
        assert np.array_equal(r["values"], d["values"])
        assert r["unit"] == d["unit"] and r["code_per_div"] == d["code_per_div"]
        # raw_uint16 uses the same gzip path.
        codes, fs = siglent_bin.raw_uint16(gz)
        assert np.array_equal(codes, d["raw"]) and fs == d["sample_rate"]


def test_gzip_detected_by_magic_not_by_name():
    """Detect gzip by content rather than filename."""
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    with tempfile.TemporaryDirectory() as td:
        gz = os.path.join(td, "a.bin.gz")
        siglent_bin.write(gz, d)
        renamed = os.path.join(td, "no-extension")
        os.rename(gz, renamed)
        assert np.array_equal(siglent_bin.read(renamed)["raw"], d["raw"])
        # Writing is still selected by the destination suffix.
        misnamed = os.path.join(td, "plain-but-named.bin")
        siglent_bin.write(misnamed, d)
        os.rename(misnamed, os.path.join(td, "liar.gz"))
        assert np.array_equal(siglent_bin.read(os.path.join(td, "liar.gz"))["raw"], d["raw"])


def test_gzip_output_is_deterministic_and_does_not_embed_the_path():
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    with tempfile.TemporaryDirectory() as td:
        a = os.path.join(td, "first-name.bin.gz")
        b = os.path.join(td, "other-name.bin.gz")
        siglent_bin.write(a, d)
        siglent_bin.write(b, d)
        with open(a, "rb") as fa, open(b, "rb") as fb:
            assert fa.read() == fb.read()


def test_mixed_native_digital_is_warned_and_skipped_for_plain_and_gzip():
    """Unsupported native logic must not hide an otherwise usable analog trace.

    This synthetic payload tests detection and skip policy only; it does not
    validate the documented digital encoding.
    """
    source = os.path.join(FIX, "sds814x_C1_30.bin")
    with open(source, "rb") as f:
        blob = bytearray(f.read())
    reference = siglent_bin.read(source)
    struct.pack_into("<i", blob, 0x158, 1)  # digital_on
    struct.pack_into("<i", blob, 0x15C, 1)  # D0 enabled
    struct.pack_into("<i", blob, 0x168, 1)  # D3 enabled
    struct.pack_into("<i", blob, 0x218, 16)  # documented digital point count
    blob.extend(b"\xaa\x55\x0f\xf0")  # opaque unsupported payload

    with tempfile.TemporaryDirectory() as td:
        plain = os.path.join(td, "mixed.bin")
        compressed = os.path.join(td, "mixed.bin.gz")
        with open(plain, "wb") as f:
            f.write(blob)
        with open(compressed, "wb") as f:
            f.write(gzip.compress(blob, mtime=0))

        for path in (plain, compressed):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                d = siglent_bin.read(path)
            assert np.array_equal(d["raw"], reference["raw"])
            assert d["digital_enabled"] is True
            assert d["unsupported_sources"] == ("D0", "D3")
            assert len(caught) == 1
            assert issubclass(caught[0].category, siglent_bin.UnsupportedTraceWarning)
            assert caught[0].filename == __file__
            message = str(caught[0].message)
            assert path in message and "D0, D3" in message and "returning C1" in message

        # Unsupported per-channel metadata must not make the analog trace unusable.
        unknown_flags = bytearray(blob)
        struct.pack_into("<i", unknown_flags, 0x15C, 2)
        struct.pack_into("<i", unknown_flags, 0x168, 0)
        unknown_path = os.path.join(td, "mixed-unknown-flags.bin")
        with open(unknown_path, "wb") as f:
            f.write(unknown_flags)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            d = siglent_bin.read(unknown_path)
        assert d["unsupported_sources"] == ()
        assert len(caught) == 1
        assert "native digital data is enabled" in str(caught[0].message)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            samples, sample_rate = siglent_bin.raw_uint16(compressed)
        assert np.array_equal(samples, reference["raw"])
        assert sample_rate == reference["sample_rate"]
        assert len(caught) == 1
        assert issubclass(caught[0].category, siglent_bin.UnsupportedTraceWarning)
        assert caught[0].filename == __file__

        corrupt = bytearray(gzip.compress(blob, mtime=0))
        corrupt[-8] ^= 0xFF  # gzip CRC; the ignored tail must still be drained and checked
        corrupt_path = os.path.join(td, "mixed-corrupt.bin.gz")
        with open(corrupt_path, "wb") as f:
            f.write(corrupt)
        try:
            siglent_bin.read(corrupt_path)
            raise AssertionError("expected corrupt mixed gzip to be rejected")
        except ValueError as e:
            assert corrupt_path in str(e)


def test_digital_only_capture_is_unsupported():
    with open(os.path.join(FIX, "sds814x_C1_30.bin"), "rb") as f:
        blob = bytearray(f.read())
    struct.pack_into("<i", blob, 0x08, 0)  # C1 off
    struct.pack_into("<i", blob, 0x158, 1)  # digital_on
    struct.pack_into("<i", blob, 0x15C, 1)  # D0 enabled
    path = _tmp_bin(bytes(blob))
    try:
        try:
            siglent_bin.read(path)
            raise AssertionError("expected digital-only capture to be unsupported")
        except NotImplementedError as e:
            assert path in str(e) and "D0" in str(e) and "no analog/math trace" in str(e)
    finally:
        os.unlink(path)


def test_gzip_corruption_and_trailing_data_are_path_bearing_value_errors():
    with open(os.path.join(FIX, "known3v_probe1x.bin"), "rb") as f:
        good = f.read()
    cases = {}
    cases["bad-header"] = gzip.compress(b"\x07" + good[1:], mtime=0)
    member = gzip.compress(good, mtime=0)
    cases["truncated-member"] = member[:-5]
    bad_crc = bytearray(member)
    bad_crc[-8] ^= 0xFF
    cases["bad-crc"] = bytes(bad_crc)
    cases["decompressed-tail"] = gzip.compress(good + b"unexpected", mtime=0)

    with tempfile.TemporaryDirectory() as td:
        for name, blob in cases.items():
            path = os.path.join(td, name + ".gz")
            with open(path, "wb") as f:
                f.write(blob)
            try:
                siglent_bin.read(path)
                raise AssertionError(f"expected {name} to be rejected")
            except ValueError as e:
                assert path in str(e), str(e)


def test_failed_write_does_not_replace_an_existing_capture():
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "existing.bin.gz")
        sentinel = b"keep the previous capture"
        with open(out, "wb") as f:
            f.write(sentinel)

        original = siglent_bin.gzip.GzipFile

        class FailingGzipFile:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def write(self, _data):
                raise OSError("simulated full disk")

            def __exit__(self, *_args):
                return False

        siglent_bin.gzip.GzipFile = FailingGzipFile
        try:
            try:
                siglent_bin.write(out, d)
                raise AssertionError("expected simulated write failure")
            except OSError as e:
                assert "simulated full disk" in str(e)
        finally:
            siglent_bin.gzip.GzipFile = original
        with open(out, "rb") as f:
            assert f.read() == sentinel
        assert sorted(os.listdir(td)) == ["existing.bin.gz"]


def test_new_write_uses_normal_open_permissions_under_umask():
    if os.name != "posix":
        return
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "new.bin")
        previous = os.umask(0o027)
        try:
            siglent_bin.write(out, d)
        finally:
            os.umask(previous)
        assert stat.S_IMODE(os.stat(out).st_mode) == 0o640


def test_replacing_a_capture_preserves_its_mode():
    if os.name != "posix":
        return
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "existing.bin.gz")
        with open(out, "wb") as f:
            f.write(b"old")
        os.chmod(out, 0o604)
        siglent_bin.write(out, d)
        assert stat.S_IMODE(os.stat(out).st_mode) == 0o604


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
