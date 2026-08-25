"""Tests for siglent_bin, run against real SDS814X HD captures in fixtures/.

Every fixture is unmodified scope output. Run with `pytest` or directly:
`python3 tests/test_siglent_bin.py`.
"""

import os
import struct
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
FIX = os.path.join(HERE, "fixtures")

import siglent_bin  # noqa: E402


def test_read_single_channel():
    d = siglent_bin.read(os.path.join(FIX, "sds814x_C1_30.bin"))
    assert d["source"] == "C1"
    assert d["sample_rate"] == 20000.0
    assert d["npoints"] == 100000
    assert d["data_width"] == 1  # 16-bit
    assert d["raw"].dtype == np.uint16
    assert len(d["values"]) == len(d["raw"]) == len(d["time"])
    assert np.all(np.diff(d["time"]) > 0)  # time strictly increasing


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
    assert abs(z1["time"][0] - 0.005) < 1e-9  # 15 ms centre - 10 ms half-window
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


def test_time_delay_sign():
    """Locks the time-axis formula's `- time_delay` sign. This capture is one
    trigger-synced sequence segment: rising-edge trigger at ~2 V with the delay
    at -100 ms, so the trigger instant (the ~2 V crossing) must sit at sample
    (tdiv*grid/2 + tdelay)*fs = 4000 — where time[] reads 0. A flipped sign
    would put it at sample 6000."""
    d = siglent_bin.read(os.path.join(FIX, "trigsync_neg100ms.bin"))
    assert d["time_delay"] == -0.1
    i0 = int(np.argmin(np.abs(d["time"])))  # sample where t = 0
    assert i0 == 4000
    v = d["values"]
    assert 1.5 < v[i0] < 2.5  # trigger level ~2 V, mid-edge
    assert v[i0 - 200] < 1.0 < 4.0 < v[i0 + 500]  # edge brackets the trigger


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


def test_read_group_aligned():
    chans = siglent_bin.read_group(siglent_bin.find_group(FIX, 30))
    assert sorted(chans) == ["C1", "C2", "C3", "C4"]
    rates = {d["sample_rate"] for d in chans.values()}
    npts = {d["npoints"] for d in chans.values()}
    assert rates == {20000.0} and npts == {100000}
    # shared time base
    t0 = chans["C1"]["time"]
    for c in chans:
        assert np.array_equal(chans[c]["time"], t0)


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


def test_read_group_rejects_mixed_acquisitions():
    """Strict read_group compares every field the shared time axis is built from —
    a file differing in time_div is not from the same acquisition."""
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
            assert "time_div" in str(e)
        assert sorted(siglent_bin.read_group(group, strict=False)) == ["C1", "C2"]
    finally:
        os.unlink(path)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
