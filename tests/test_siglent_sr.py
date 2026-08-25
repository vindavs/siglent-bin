"""Tests for srzip export from saved waveform fixtures."""

import configparser
import os
import re
import sys
import tempfile
import zipfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
FIX = os.path.join(HERE, "fixtures")

import siglent_bin  # noqa: E402
import siglent_sr  # noqa: E402


def _meta(zf):
    """Parse an open session's metadata entry."""
    cp = configparser.ConfigParser()
    cp.read_string(zf.read("metadata").decode("ascii"))
    return cp


def test_analog_only_structure():
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe10x.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "a.sr"), d, logic=False)
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            assert names == {"version", "metadata", "analog-1-1-1"}
            assert zf.read("version").decode("ascii").strip() == "2"
            cp = _meta(zf)
            dev = cp["device 1"]
            assert dev["total analog"] == "1"
            assert dev["analog1"] == "C1"
            assert dev["samplerate"] == "10000"
            assert "capturefile" not in dev
            assert "unitsize" not in dev
            assert "total probes" not in dev
            vals = np.frombuffer(zf.read("analog-1-1-1"), dtype=np.float32)
            assert len(vals) == 2000
            assert np.allclose(vals, d["values"].astype(np.float32))
            # Preserve the fixture's 10x probe scaling.
            assert 0.39 < float(vals.min()) < 0.40
            assert 3.20 < float(vals.max()) < 3.21


def test_analog_false_omits_chunk():
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe10x.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "b.sr"), d, analog=False)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert not any(n.startswith("analog-") for n in names)


def test_metadata_key_order():
    """Place channel totals before the names that resolve against them."""
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe10x.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "a.sr"), d, logic=False)
        with zipfile.ZipFile(out) as zf:
            text = zf.read("metadata").decode("ascii")
            assert text.index("total analog") < text.index("analog1=")

    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "l.sr"), d, analog=False)
        with zipfile.ZipFile(out) as zf:
            text = zf.read("metadata").decode("ascii")
            assert text.index("total probes") < text.index("probe1=")


def test_auto_threshold_uses_percentiles_not_extremes():
    """Keep one extreme spike from moving the threshold outside the signal swing."""
    v = np.concatenate([np.zeros(500), np.full(500, 3.0)])
    v[123] = 30.0
    th = siglent_sr.auto_threshold(v)
    assert 1.0 < th < 2.0


def test_logic_only_structure_and_edges():
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "l.sr"), d, analog=False)
        with zipfile.ZipFile(out) as zf:
            assert set(zf.namelist()) == {"version", "metadata", "logic-1-1"}
            dev = _meta(zf)["device 1"]
            assert dev["capturefile"] == "logic-1"
            assert dev["unitsize"] == "1"
            assert dev["total probes"] == "1"
            assert dev["probe1"] == "C1_d"
            assert dev["samplerate"] == "1000000"
            assert "total analog" not in dev
            raw = np.frombuffer(zf.read("logic-1-1"), dtype=np.uint8)
            assert len(raw) == 5000
            bits = (raw & 1).astype(bool)
            # Five cycles of a 1 kHz square contain ten transitions.
            assert int(np.count_nonzero(np.diff(bits))) == 10


def test_explicit_threshold_overrides_auto():
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        # 5 V is above the high level, so every sample reads low.
        out = siglent_sr.write(os.path.join(tmp, "hi.sr"), d, analog=False, threshold=5.0)
        with zipfile.ZipFile(out) as zf:
            raw = np.frombuffer(zf.read("logic-1-1"), dtype=np.uint8)
            assert int(np.count_nonzero(raw & 1)) == 0


def test_per_source_threshold_dict():
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "p.sr"), d, analog=False, threshold={"C1": 5.0})
        with zipfile.ZipFile(out) as zf:
            raw = np.frombuffer(zf.read("logic-1-1"), dtype=np.uint8)
            assert int(np.count_nonzero(raw & 1)) == 0


def test_per_source_threshold_dict_falls_back_to_auto_on_miss():
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    threshold, was_auto = siglent_sr._resolve_threshold("out.sr", d, {"C2": 5.0})
    assert was_auto
    assert threshold == siglent_sr.auto_threshold(d["values"])


def test_hysteresis_suppresses_noise_transitions():
    """A Schmitt trigger chatters less than a comparator on a noisy ramp."""
    rng = np.random.default_rng(0)
    v = np.concatenate([np.zeros(200), np.full(200, 1.0), np.full(200, 2.0)])
    v = v + rng.normal(0, 0.25, v.size)
    plain = siglent_sr._to_bits(v, 1.0, 0.0)
    schmitt = siglent_sr._to_bits(v, 1.0, 1.0)
    assert int(np.count_nonzero(np.diff(schmitt))) < int(np.count_nonzero(np.diff(plain)))


def test_pack_logic_nine_channels_crosses_the_byte_boundary():
    """Pack the ninth logic channel into bit 0 of the second byte."""
    n_chan = 9
    unitsize = siglent_sr._unitsize(n_chan)
    assert unitsize == 2
    bit_arrays = [np.zeros(3, dtype=bool) for _ in range(n_chan)]
    bit_arrays[0] = np.array([False, True, True])  # channel 0: byte 0 bit 0
    bit_arrays[8] = np.array([True, False, True])  # channel 8: byte 1 bit 0
    packed = siglent_sr._pack_logic(bit_arrays, unitsize)
    raw = np.frombuffer(packed, dtype=np.uint8).reshape(3, unitsize)
    assert raw[0, 0] == 0 and raw[0, 1] & 1 == 1  # only channel 8 high
    assert raw[1, 0] & 1 == 1 and raw[1, 1] == 0  # only channel 0 high
    assert raw[2, 0] & 1 == 1 and raw[2, 1] & 1 == 1  # both high


def test_logic_and_analog_index_offset():
    """Offset analog indices by the preceding logic channel count."""
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "b.sr"), d)
        with zipfile.ZipFile(out) as zf:
            assert set(zf.namelist()) == {"version", "metadata", "logic-1-1", "analog-1-2-1"}
            dev = _meta(zf)["device 1"]
            assert dev["total probes"] == "1"
            assert dev["total analog"] == "1"
            assert dev["probe1"] == "C1_d"
            assert dev["analog2"] == "C1"
            assert "analog1" not in dev


def test_four_channel_group():
    paths = [os.path.join(FIX, f"sds814x_C{i}_30.bin") for i in (1, 2, 3, 4)]
    group = siglent_bin.read_group(paths)
    traces = [group[k] for k in ("C1", "C2", "C3", "C4")]
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "g.sr"), traces)
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            assert "logic-1-1" in names
            for k in (5, 6, 7, 8):  # 4 logic channels, then analog
                assert f"analog-1-{k}-1" in names
            dev = _meta(zf)["device 1"]
            assert dev["total probes"] == "4"
            assert dev["total analog"] == "4"
            assert dev["unitsize"] == "1"  # 4 channels still fit one byte
            assert dev["probe1"] == "C1_d"
            assert dev["probe4"] == "C4_d"
            assert dev["analog5"] == "C1"
            assert dev["analog8"] == "C4"
            raw = np.frombuffer(zf.read("logic-1-1"), dtype=np.uint8)
            assert len(raw) == 100000
            # Bit i belongs to channel i.
            th = siglent_sr.auto_threshold(traces[0]["values"])
            assert np.array_equal((raw & 1).astype(bool), traces[0]["values"] > th)


def test_write_accepts_read_group_mapping_directly():
    """Treat a read_group mapping like its list of trace values."""
    paths = [os.path.join(FIX, f"sds814x_C{i}_30.bin") for i in (1, 2, 3, 4)]
    group = siglent_bin.read_group(paths)
    with tempfile.TemporaryDirectory() as tmp:
        out_dict = siglent_sr.write(os.path.join(tmp, "dict.sr"), group)
        out_list = siglent_sr.write(os.path.join(tmp, "list.sr"), list(group.values()))
        with zipfile.ZipFile(out_dict) as zf:
            dict_logic = zf.read("logic-1-1")
            dict_meta = zf.read("metadata")
        with zipfile.ZipFile(out_list) as zf:
            list_logic = zf.read("logic-1-1")
            list_meta = zf.read("metadata")
        assert dict_logic == list_logic
        assert dict_meta == list_meta


def test_non_dict_traces_rejected():
    """Reject non-dict traces with a path-bearing ValueError."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "m.sr")
        try:
            siglent_sr.write(out, ["C1", "C2"])
            raise AssertionError("expected ValueError for non-dict traces")
        except ValueError as e:
            assert out in str(e)


def test_as_list_empty_iterable_and_multi_item():
    try:
        siglent_sr._as_list([])
        raise AssertionError("expected ValueError for an empty iterable")
    except ValueError as e:
        assert "no traces" in str(e)

    a = {"source": "C1", "values": np.zeros(3)}
    b = {"source": "C2", "values": np.zeros(3)}
    assert siglent_sr._as_list([a, b]) == [a, b]
    assert siglent_sr._as_list((a, b)) == [a, b]


def test_trigger_sample_from_time_axis():
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    assert siglent_sr.trigger_sample(d) == 2500
    assert siglent_sr.trigger_sample(d, decimate=10) == 250
    d2 = siglent_bin.read(os.path.join(FIX, "sds814x_C1_30.bin"))
    assert siglent_sr.trigger_sample(d2) == 9900


def test_metadata_comments_are_ignorable_and_present():
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "c.sr"), d)
        with zipfile.ZipFile(out) as zf:
            text = zf.read("metadata").decode("ascii")
            assert "# siglent_sr: trigger_sample = 2500" in text
            assert "trigger_time_s = -2.5" in text
            assert "threshold_C1" in text
            assert "threshold_C1 = 1.5000 V (auto, p1..p99 midpoint), 10 edges" in text
            # Comments remain ignorable to the INI parser.
            dev = _meta(zf)["device 1"]
            for key in dev:
                assert not key.startswith("#")
                assert "trigger_sample" not in key


def test_source_file_comment_recorded_and_ordered():
    """Record source_file between trigger and threshold metadata."""
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "s.sr"), d, source_file="x.bin")
        with zipfile.ZipFile(out) as zf:
            text = zf.read("metadata").decode("ascii")
        assert "# siglent_sr: source_file = x.bin" in text
        assert text.index("trigger_time_s") < text.index("source_file")
        assert text.index("source_file") < text.index("threshold_C1")

        # Omit the comment when no source is supplied.
        out2 = siglent_sr.write(os.path.join(tmp, "n.sr"), d)
        with zipfile.ZipFile(out2) as zf:
            text2 = zf.read("metadata").decode("ascii")
        assert "source_file" not in text2


def test_mismatched_traces_rejected():
    a = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    b = siglent_bin.read(os.path.join(FIX, "known3v_probe10x.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        try:
            siglent_sr.write(os.path.join(tmp, "m.sr"), [a, b])
            raise AssertionError("expected ValueError for mismatched traces")
        except ValueError as e:
            assert "sample_rate" in str(e) or "length" in str(e)


def test_length_mismatch_rejected():
    """Reject different lengths when sample rates match."""
    a = {
        "source": "C1",
        "sample_rate": 1000.0,
        "values": np.zeros(100),
        "t0": -0.05,
    }
    b = {
        "source": "C2",
        "sample_rate": 1000.0,
        "values": np.zeros(50),
        "t0": -0.025,
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "m.sr")
        try:
            siglent_sr.write(out, [a, b])
            raise AssertionError("expected ValueError for length mismatch")
        except ValueError as e:
            assert out in str(e)
            assert "length" in str(e)


def test_t0_mismatch_rejected():
    a = {
        "source": "C1",
        "sample_rate": 1000.0,
        "values": np.zeros(100),
        "t0": -0.05,
    }
    b = dict(a, source="C2", t0=-0.04)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "m.sr")
        try:
            siglent_sr.write(out, [a, b])
            raise AssertionError("expected ValueError for mismatched t0")
        except ValueError as e:
            assert out in str(e)
            assert "t0 mismatch" in str(e)
            assert "C1" in str(e) and "C2" in str(e)
            assert not os.path.exists(out)


def test_nonfinite_t0_rejected():
    for i, t0 in enumerate((float("nan"), float("inf"), float("-inf"))):
        trace = {
            "source": "C1",
            "sample_rate": 1000.0,
            "values": np.zeros(10),
            "t0": t0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, f"m{i}.sr")
            try:
                siglent_sr.write(out, trace)
                raise AssertionError("expected ValueError for non-finite t0")
            except ValueError as e:
                assert out in str(e)
                assert "t0" in str(e) and "finite" in str(e)
                assert not os.path.exists(out)


def test_missing_values_rejected():
    """Reject a missing values field with a path-bearing ValueError."""
    a = {"source": "C1", "sample_rate": 1000.0}  # no 'values' at all
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "m.sr")
        try:
            siglent_sr.write(out, [a])
            raise AssertionError("expected ValueError for missing values")
        except ValueError as e:
            assert out in str(e)


def test_empty_values_array_rejected():
    """Reject an empty values array before thresholding."""
    a = {
        "source": "C1",
        "sample_rate": 1000.0,
        "values": np.zeros(0),
        "t0": -0.005,
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "m.sr")
        try:
            siglent_sr.write(out, [a])
            raise AssertionError("expected ValueError for empty values")
        except ValueError as e:
            assert out in str(e)
            assert "values" in str(e)


def test_missing_sample_rate_rejected():
    """Reject a missing sample_rate with a path-bearing ValueError."""
    a = {"source": "C1", "values": np.zeros(10)}  # no 'sample_rate' at all
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "m.sr")
        try:
            siglent_sr.write(out, [a])
            raise AssertionError("expected ValueError for missing sample_rate")
        except ValueError as e:
            assert out in str(e)


def test_none_sample_rate_rejected():
    """Reject sample_rate=None with a path-bearing ValueError."""
    a = {"source": "C1", "values": np.zeros(10), "sample_rate": None}
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "m.sr")
        try:
            siglent_sr.write(out, [a])
            raise AssertionError("expected ValueError for sample_rate=None")
        except ValueError as e:
            assert out in str(e)


def test_missing_t0_rejected():
    """Reject a missing t0 with a path-bearing ValueError."""
    a = {"source": "C1", "sample_rate": 1000.0, "values": np.zeros(10)}
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "m.sr")
        try:
            siglent_sr.write(out, [a])
            raise AssertionError("expected ValueError for missing t0")
        except ValueError as e:
            assert out in str(e)


def test_missing_source_rejected():
    """Reject a missing source with a path-bearing ValueError."""
    a = {"sample_rate": 1000.0, "values": np.zeros(10), "t0": -0.005}
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "m.sr")
        try:
            siglent_sr.write(out, [a])
            raise AssertionError("expected ValueError for missing source")
        except ValueError as e:
            assert out in str(e)


def test_refuses_oversized_capture_and_names_a_factor_that_fits():
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "big.sr")
        try:
            # 5000 pts x (1 logic + 1 analog) = 10000 samples, limit 1000
            siglent_sr.write(out, d, max_points=1000)
            raise AssertionError("expected ValueError for oversized capture")
        except ValueError as e:
            msg = str(e)
            assert "--decimate 10" in msg
            assert "1000" in msg
            assert not os.path.exists(out)  # nothing half-written
            # 5000 pts x (4 B analog + 1 B packed logic) = 25 KB.
            assert "25.00 KB" in msg


def test_size_guard_message_omits_logic_byte_when_logic_is_false():
    """Do not include a logic byte in an analog-only size estimate."""
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "big.sr")
        try:
            # 5000 pts x 1 analog-only channel = 5000 samples, limit 1000
            siglent_sr.write(out, d, logic=False, max_points=1000)
            raise AssertionError("expected ValueError for oversized capture")
        except ValueError as e:
            msg = str(e)
            # 5000 pts x 4 B analog = 20 KB.
            assert "20.00 KB" in msg
            assert "25.00 KB" not in msg


def test_invalid_export_controls_are_rejected_before_writing():
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    cases = [
        ("threshold", {"threshold": float("nan")}),
        ("threshold", {"threshold": {"C1": float("inf")}}),
        ("hysteresis", {"hysteresis": float("nan")}),
        ("hysteresis", {"hysteresis": -0.1}),
        ("decimate", {"decimate": 1.5}),
        ("decimate", {"decimate": True}),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for i, (label, kw) in enumerate(cases):
            out = os.path.join(tmp, f"invalid-{i}.sr")
            try:
                siglent_sr.write(out, d, **kw)
                raise AssertionError(f"expected ValueError for {kw!r}")
            except ValueError as e:
                assert label in str(e)
                assert not os.path.exists(out)


def test_decimation_divides_samplerate_and_length():
    import contextlib
    import io

    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = siglent_sr.write(os.path.join(tmp, "d.sr"), d, decimate=10)
        # The shortest complete run is 500 samples, so decimate=10 is safe.
        assert err.getvalue() == ""
        with zipfile.ZipFile(out) as zf:
            dev = _meta(zf)["device 1"]
            assert dev["samplerate"] == "100000"
            raw = np.frombuffer(zf.read("logic-1-1"), dtype=np.uint8)
            assert len(raw) == 500
            # The decimated square still has ten transitions.
            assert int(np.count_nonzero(np.diff((raw & 1).astype(bool)))) == 10


def test_decimation_scales_trigger_metadata_to_written_coordinates():
    trace = {
        "source": "C1",
        "sample_rate": 100.0,
        "values": np.zeros(100),
        "t0": -0.5,
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "d.sr"), trace, logic=False, decimate=10)
        with zipfile.ZipFile(out) as zf:
            text = zf.read("metadata").decode("ascii")
            vals = np.frombuffer(zf.read("analog-1-1-1"), dtype=np.float32)
    assert len(vals) == 10
    assert "# siglent_sr: trigger_sample = 5" in text
    assert 0 <= 5 < len(vals)


def test_samplerate_exact_comment_when_decimation_is_inexact():
    """Record an exact rate when srzip's integer samplerate rounds it."""
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "r.sr"), d, decimate=3)
        with zipfile.ZipFile(out) as zf:
            text = zf.read("metadata").decode("ascii")
        assert "samplerate=333333" in text
        assert "# siglent_sr: samplerate_exact = 333333.3333333333" in text

        out2 = siglent_sr.write(os.path.join(tmp, "e.sr"), d, decimate=10)
        with zipfile.ZipFile(out2) as zf:
            text2 = zf.read("metadata").decode("ascii")
        assert "samplerate_exact" not in text2


def test_min_run_length():
    bits = np.array([0, 0, 0, 1, 1, 0, 0, 0, 0], dtype=bool)
    assert siglent_sr.min_run_length(bits) == 2
    assert siglent_sr.min_run_length(np.ones(7, dtype=bool)) == 7
    # Ignore a truncated leading run; the shortest interior run is 4.
    boundary_short = np.concatenate([np.ones(1), np.zeros(4), np.ones(4), np.zeros(3)]).astype(bool)
    assert siglent_sr.min_run_length(boundary_short) == 4
    # With three runs, only the middle run is complete.
    three_runs = np.concatenate([np.ones(3), np.zeros(1), np.ones(2)]).astype(bool)
    assert siglent_sr.min_run_length(three_runs) == 1
    # Two runs are both truncated at capture boundaries.
    two_runs = np.concatenate([np.zeros(3), np.ones(4)]).astype(bool)
    assert siglent_sr.min_run_length(two_runs) == two_runs.size == 7


def test_decimate_less_than_one_rejected():
    a = {
        "source": "C1",
        "sample_rate": 1000.0,
        "values": np.zeros(10),
        "t0": -0.005,
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "m.sr")
        try:
            siglent_sr.write(out, a, decimate=0)
            raise AssertionError("expected ValueError for decimate < 1")
        except ValueError as e:
            assert "decimate" in str(e)


def test_offered_decimate_factor_actually_fits_when_n_not_divisible():
    """Ensure the offered factor fits using ceil division, not floor division."""
    import contextlib
    import io

    a = {
        "source": "C1",
        "sample_rate": 1000.0,
        "values": np.array([0.0, 0.0, 3.0, 3.0]),
        "t0": -0.002,
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "r.sr")
        try:
            siglent_sr.write(out, a, max_points=3)
            raise AssertionError("expected ValueError for oversized capture")
        except ValueError as e:
            msg = str(e)
        m = re.search(r"--decimate (\d+)", msg)
        assert m, f"no --decimate factor found in: {msg}"
        factor = int(m.group(1))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out2 = siglent_sr.write(out, a, max_points=3, decimate=factor)
        with zipfile.ZipFile(out2) as zf:
            raw = np.frombuffer(zf.read("logic-1-1"), dtype=np.uint8)
            vals = np.frombuffer(zf.read("analog-1-2-1"), dtype=np.float32)
            written = len(raw) + len(vals)
            assert written <= 3


def test_decimation_warns_when_it_would_destroy_edges():
    """A 2-sample-wide pulse cannot survive decimate=10."""
    import contextlib
    import io

    v = np.zeros(1000)
    v[500:502] = 3.0
    trace = {
        "values": v,
        "sample_rate": 1e6,
        "source": "C1",
        "t0": 0.0,
        "probe": 1.0,
    }
    err = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(err):
        siglent_sr.write(os.path.join(tmp, "w.sr"), trace, decimate=10)
    text = err.getvalue()
    assert "narrowest" in text
    assert "decimate" in text


def test_write_frames_one_file_per_frame():
    """Write each sequence frame separately because dead time is unknown."""
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    frames = [dict(d, frame=1), dict(d, frame=2), dict(d, frame=3)]
    with tempfile.TemporaryDirectory() as tmp:
        stem = os.path.join(tmp, "seq")
        paths = siglent_sr.write_frames(stem, frames, analog=False)
        assert [os.path.basename(p) for p in paths] == ["seq-01.sr", "seq-02.sr", "seq-03.sr"]
        for p in paths:
            with zipfile.ZipFile(p) as zf:
                assert "logic-1-1" in zf.namelist()
                assert _meta(zf)["device 1"]["samplerate"] == "1000000"


def test_write_frames_accepts_a_single_frame():
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        paths = siglent_sr.write_frames(os.path.join(tmp, "one"), [d])
        assert len(paths) == 1
        assert os.path.basename(paths[0]) == "one-01.sr"


def test_write_frames_100_plus_frames_use_dynamic_padding():
    """Use enough zero padding to preserve lexical order above 99 frames."""
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    frames = [dict(d, frame=i) for i in range(105)]
    with tempfile.TemporaryDirectory() as tmp:
        paths = siglent_sr.write_frames(os.path.join(tmp, "long"), frames, analog=False)
        basenames = [os.path.basename(p) for p in paths]
        assert len(basenames) == 105
        assert sorted(basenames) == basenames
        assert basenames[0] == "long-001.sr"
        assert basenames[8] == "long-009.sr"
        assert basenames[99] == "long-100.sr"
        assert basenames[104] == "long-105.sr"


def test_write_frames_each_file_has_correct_payload_size():
    """Each frame file contains one frame's payload."""
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    frames = [dict(d, frame=1), dict(d, frame=2)]
    with tempfile.TemporaryDirectory() as tmp:
        paths = siglent_sr.write_frames(os.path.join(tmp, "sep"), frames, analog=False)
        expected_bytes = len(d["values"])
        for p in paths:
            with zipfile.ZipFile(p) as zf:
                payload = zf.read("logic-1-1")
                assert len(payload) == expected_bytes


def test_write_frames_strips_sr_suffix_from_path_stem():
    """Strip an existing .sr suffix before adding frame numbering."""
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        paths = siglent_sr.write_frames(os.path.join(tmp, "seq.sr"), [d])
        assert len(paths) == 1
        assert os.path.basename(paths[0]) == "seq-01.sr"


def test_sigrok_cli_loads_the_file():
    """Load the generated session with sigrok-cli when installed."""
    import shutil
    import subprocess

    exe = shutil.which("sigrok-cli")
    if not exe:
        print("    (skipped: sigrok-cli not on PATH)")
        return
    d = siglent_bin.read(os.path.join(FIX, "cal_square_1khz.bin"))
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "s.sr"), d)
        r = subprocess.run([exe, "-i", out, "--show"], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert re.search(r"\bC1_d\b", r.stdout), "logic channel C1_d not found"
        assert re.search(r"\bC1\b", r.stdout), "analog channel C1 not found"
        assert "1 MHz" in r.stdout or "1000000" in r.stdout


def _raw_meta(path):
    """Return metadata text including comments."""
    with zipfile.ZipFile(path) as zf:
        return zf.read("metadata").decode("ascii")


def test_amps_capture_is_not_labelled_volts():
    """Record amps in metadata instead of labelling the trace as volts."""
    d = siglent_bin.read(os.path.join(FIX, "amps_30a.bin"))
    assert d["unit"] == "A", "fixture precondition: this is a current capture"
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "a.sr"), d)
        text = _raw_meta(out)
    assert "unit_C1 = A" in text
    threshold = next(line for line in text.splitlines() if "threshold_C1" in line)
    assert " A (" in threshold, threshold
    assert " V (" not in threshold, threshold


def test_volts_capture_records_its_unit():
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe10x.bin"))
    assert d["unit"] == "V"
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "v.sr"), d)
        text = _raw_meta(out)
    assert "unit_C1 = V" in text
    assert " V (" in next(line for line in text.splitlines() if "threshold_C1" in line)


def test_trace_without_a_unit_is_not_guessed_at():
    """Keep a unitless threshold unitless."""
    n = 200
    trace = {
        "values": np.array([0.0, 1.0] * (n // 2)),
        "sample_rate": 1e6,
        "source": "C1",
        "t0": 0.0,
    }
    assert "unit" not in trace
    with tempfile.TemporaryDirectory() as tmp:
        out = siglent_sr.write(os.path.join(tmp, "u.sr"), trace)
        text = _raw_meta(out)
    assert "unit_C1" not in text
    threshold = next(line for line in text.splitlines() if "threshold_C1" in line)
    assert " V " not in threshold and " A " not in threshold, threshold
    assert "0.5000 (" in threshold, threshold


def test_non_volt_trace_names_its_unit_in_the_channel():
    """Append a non-volt unit to the analog channel name."""
    d = siglent_bin.read(os.path.join(FIX, "amps_300ma.bin"))
    assert d["unit"] == "A"
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "a.sr")
        siglent_sr.write(out, [d])
        with zipfile.ZipFile(out) as zf:
            dev = _meta(zf)["device 1"]
            assert dev["analog2"] == "C1[A]"
            assert dev["probe1"] == "C1_d"  # logic name is untouched
            assert "analog-1-2-1" in zf.namelist()


def test_volt_trace_name_is_unsuffixed():
    """Leave volt channel names unsuffixed."""
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    assert d["unit"] == "V"
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "v.sr")
        siglent_sr.write(out, [d], logic=False)
        with zipfile.ZipFile(out) as zf:
            assert _meta(zf)["device 1"]["analog1"] == "C1"


def test_unitless_trace_is_not_labelled():
    """Leave a unitless channel name unsuffixed."""
    d = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
    d.pop("unit")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "u.sr")
        siglent_sr.write(out, [d], logic=False)
        with zipfile.ZipFile(out) as zf:
            assert _meta(zf)["device 1"]["analog1"] == "C1"


def test_non_volt_analog_export_warns_once():
    """Warn once per non-volt analog trace and not for logic-only output."""
    import contextlib
    import io

    d = siglent_bin.read(os.path.join(FIX, "amps_300ma.bin"))
    with tempfile.TemporaryDirectory() as td:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            siglent_sr.write(os.path.join(td, "a.sr"), [d])
        lines = [ln for ln in err.getvalue().splitlines() if "srzip cannot store" in ln]
        assert len(lines) == 1, err.getvalue()
        assert "C1 is in A" in lines[0]

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            siglent_sr.write(os.path.join(td, "logic.sr"), [d], analog=False)
        assert "srzip cannot store" not in err.getvalue()

        # Volts needs no warning.
        v = siglent_bin.read(os.path.join(FIX, "known3v_probe1x.bin"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            siglent_sr.write(os.path.join(td, "v.sr"), [v])
        assert "srzip cannot store" not in err.getvalue()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
