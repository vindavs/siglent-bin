"""Loopback tests for the SDS800X HD SCPI waveform path."""

import math
import os
import socket
import struct
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import siglent_bin  # noqa: E402
import siglent_lan  # noqa: E402

NPOINTS = 8
VDIV, VOFF, CPD, PROBE = 0.5, 0.0, 7680.0, 10.0
INTERVAL = 2e-6
TIME_DIV, TIME_DELAY = 1e-3, 2.5e-3
REF_POS = 30.0  # horizontal reference position, %
FRAME_BASE = 1000  # frame n's codes start at n * FRAME_BASE


def _descriptor(
    sum_frames,
    npoints=NPOINTS,
    cpd=CPD,
    adc_bit=12,
    order=0,
    interval=INTERVAL,
    stamp_seconds=None,
    data_interval=1,
):
    d = bytearray(siglent_lan.DESC_BYTES)
    struct.pack_into("<h", d, 0x20, 1)  # 16-bit
    struct.pack_into("<h", d, 0x22, order)
    struct.pack_into("<i", d, 0x74, npoints)
    struct.pack_into("<i", d, 0x88, data_interval)  # echo of :WAVeform:INTerval
    struct.pack_into("<i", d, 0x90, 1)  # read_frames
    struct.pack_into("<i", d, 0x94, sum_frames)
    struct.pack_into("<f", d, 0x9C, VDIV)
    struct.pack_into("<f", d, 0xA0, VOFF)
    struct.pack_into("<f", d, 0xA4, cpd)
    struct.pack_into("<h", d, 0xAC, adc_bit)
    struct.pack_into("<f", d, 0xB0, interval)
    centre_delay = TIME_DELAY + (0.5 - REF_POS / 100.0) * TIME_DIV * siglent_lan.GRID
    struct.pack_into("<d", d, 0xB4, centre_delay)
    struct.pack_into("<h", d, 0x144, 20)  # off-by-one enum, must be ignored
    struct.pack_into("<f", d, 0x148, PROBE)
    tail = b""
    if stamp_seconds is not None:
        tail = struct.pack("<dBBBBh", stamp_seconds, 30, 12, 21, 8, 2026) + b"\0\0"
    return bytes(d) + tail


def _block(payload, header=b""):
    """Build an IEEE 488.2 block with an optional response prefix."""
    n = str(len(payload)).encode()
    return header + b"#" + str(len(n)).encode() + n + payload + b"\n"


class FakeScope:
    """Serve one connection using the per-frame read protocol."""

    def __init__(
        self,
        sum_frames=1,
        sequence=True,
        header=b"",
        order=0,
        cpd=CPD,
        npoints=NPOINTS,
        interval=INTERVAL,
        adc_bit=12,
        stamp=1.0,
        short_data=False,
        unit=b"V",
        data_interval=1,
        fragment_block_terminator=False,
    ):
        self.sum_frames, self.sequence = sum_frames, sequence
        self.header, self.order, self.cpd, self.npoints = header, order, cpd, npoints
        self.interval, self.adc_bit, self.stamp = interval, adc_bit, stamp
        self.short_data = short_data
        self.unit = unit  # None models a firmware that never answers :CHANnel<n>:UNIT?
        self.data_interval = data_interval  # 0 models a firmware that never fills 0x88 in
        self.fragment_block_terminator = fragment_block_terminator
        self.commands, self.frame = [], 1
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        buf = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    cmd = line.decode("ascii", "replace").strip()
                    self.commands.append(cmd)
                    try:
                        self._reply(conn, cmd.upper())
                    except OSError:
                        return

    def _reply(self, conn, up):
        if up.startswith(":WAVEFORM:SEQUENCE ") and "?" not in up:
            self.frame = int(up.split()[1].split(",")[0])
        elif up.endswith("PREAMBLE?"):
            self._send_block(
                conn,
                _descriptor(
                    self.sum_frames,
                    self.npoints,
                    self.cpd,
                    self.adc_bit,
                    self.order,
                    self.interval,
                    self.stamp,
                    self.data_interval,
                ),
            )
        elif up.endswith("DATA?"):
            n = self.npoints // 2 if self.short_data else self.npoints
            dt = ">i2" if self.order else "<i2"
            codes = (self.frame * FRAME_BASE + np.arange(n)).astype(dt)
            self._send_block(conn, codes.tobytes())
        elif "ACQUIRE:SEQUENCE?" in up:
            conn.sendall(b"ON\n" if self.sequence else b"OFF\n")
        elif "TIMEBASE:SCALE" in up:
            conn.sendall(f"{TIME_DIV:.6E}\n".encode())
        elif "TIMEBASE:DELAY" in up:
            conn.sendall(f"{TIME_DELAY:.6E}\n".encode())
        elif "REFERENCE:POSITION" in up:
            conn.sendall(f"{REF_POS:.6E}\n".encode())
        elif "TIMEBASE:REFERENCE?" in up:
            conn.sendall(b"DELay\n")
        elif up.endswith("UNIT?"):
            # Handle before the catch-all, whose "ON" would pass unit validation.
            if self.unit is not None:
                conn.sendall(self.unit + b"\n")
        elif "ACQUIRE:SRATE" in up:
            conn.sendall(f"{1.0 / INTERVAL:.6E}\n".encode())
        elif up.endswith("?"):
            conn.sendall(b"ON\n")

    def _send_block(self, conn, payload):
        response = _block(payload, header=self.header)
        if not self.fragment_block_terminator:
            conn.sendall(response)
            return
        conn.sendall(response[:-1])
        # Let the client send its next query before the late terminator.
        time.sleep(0.02)
        conn.sendall(response[-1:])

    def close(self):
        self._srv.close()


def _fetch(source="C1", **kw):
    fetch_kw = {k: kw.pop(k) for k in ("apply_probe", "stop") if k in kw}
    fake = FakeScope(**kw)
    try:
        return fake, siglent_lan.fetch("127.0.0.1", source, port=fake.port, timeout=5, **fetch_kw)
    finally:
        fake.close()


def test_single_frame_sequence_off():
    fake, frames = _fetch(sum_frames=1, sequence=False)
    assert len(frames) == 1
    f = frames[0]
    assert f["source"] == "C1" and f["frame"] == 1 and f["frames_total"] == 1
    assert f["sequence"] is False
    assert f["npoints"] == NPOINTS == len(f["values"])
    assert f["values"].dtype == np.float32
    assert "t0" in f
    assert f["vdiv"] == VDIV  # pre-probe, as in the file path
    assert f["probe"] == PROBE
    # Do not send a frame selector when sequence mode is off.
    assert not any(
        "SEQUENCE" in c.upper() and "?" not in c
        for c in fake.commands
        if c.upper().startswith(":WAV")
    )


def test_timebase_comes_from_scpi_not_the_descriptor():
    """Use queried reference geometry rather than the model-dependent enum."""
    _fake, frames = _fetch()
    f = frames[0]
    assert f["time_div"] == TIME_DIV and f["time_delay"] == TIME_DELAY
    # t=0 sits at ref_position% of the span, offset by time_delay.
    assert f["ref_position"] == REF_POS and f["ref_strategy"] == "DELay"
    assert np.isclose(f["t0"], -(REF_POS / 100) * TIME_DIV * 10 + TIME_DELAY)
    assert np.isclose(f["sample_rate"], 1.0 / INTERVAL, rtol=1e-9)
    expected_desc_delay = TIME_DELAY + (0.5 - REF_POS / 100) * TIME_DIV * siglent_lan.GRID
    assert np.isclose(f["desc_delay"], expected_desc_delay)
    assert np.all(np.diff(siglent_bin.time_axis(f)) > 0)


def test_reference_position_is_queried_not_assumed():
    """Query the horizontal reference instead of assuming screen centre."""
    fake, frames = _fetch()
    assert any("REFERENCE:POSITION" in c.upper() for c in fake.commands)
    span = TIME_DIV * 10
    centre_t0 = -0.5 * span + TIME_DELAY
    assert not np.isclose(frames[0]["t0"], centre_t0)
    assert np.isclose(frames[0]["t0"] - centre_t0, 0.20 * span)


def test_every_frame_of_a_sequence_run():
    _fake, frames = _fetch(sum_frames=4)
    assert [f["frame"] for f in frames] == [1, 2, 3, 4]
    assert all(f["frames_total"] == 4 and f["sequence"] for f in frames)
    # Each frame must carry distinct data.
    for k, f in enumerate(frames, start=1):
        assert f["raw"][0] == 32768 + k * FRAME_BASE
    assert len({f["raw"].tobytes() for f in frames}) == 4


def test_frame_selector_stays_inside_the_supported_form():
    """Use only the tested ``<frame>,1`` selector form."""
    fake, _ = _fetch(sum_frames=3)
    sel = [c for c in fake.commands if c.upper().startswith(":WAVEFORM:SEQUENCE") and "?" not in c]
    assert sel == [":WAVeform:SEQuence 1,1", ":WAVeform:SEQuence 2,1", ":WAVeform:SEQuence 3,1"]


def test_trigger_stop_precedes_the_read():
    """Stop before reading a completed sequence buffer."""
    fake, _ = _fetch(sum_frames=2)
    ups = [c.upper() for c in fake.commands]
    assert ":TRIGGER:STOP" in ups
    first_read = next(i for i, c in enumerate(ups) if c.endswith("PREAMBLE?"))
    assert ups.index(":TRIGGER:STOP") < first_read

    fake2, _ = _fetch(sum_frames=1, stop=False)
    assert ":TRIGGER:STOP" not in [c.upper() for c in fake2.commands]


def test_samples_are_signed_shifted_to_offset_binary():
    """Convert signed live samples to the file codec's offset-binary convention."""
    fake = FakeScope(sum_frames=1, sequence=False)
    fake.frame = -1  # negative codes
    try:
        f = siglent_lan.fetch("127.0.0.1", "C1", port=fake.port, timeout=5)[0]
        assert f["raw"].dtype == np.uint16
        assert f["raw"][0] == 32768 - FRAME_BASE  # below centre
        assert f["values"][0] < 0
        assert np.isclose(f["values"][0], -FRAME_BASE / CPD * VDIV * PROBE)
    finally:
        fake.close()


def test_probe_can_be_left_off():
    _fake, frames = _fetch(sum_frames=1, sequence=False, apply_probe=False)
    f = frames[0]
    assert f["vdiv"] == VDIV
    assert np.isclose(f["values"][0], FRAME_BASE / CPD * VDIV)


def test_width_is_set_before_the_scaling_preamble():
    """Set transfer width before reading its scaling preamble."""
    fake, _ = _fetch()
    ups = [c.upper() for c in fake.commands]
    width = next(i for i, c in enumerate(ups) if ":WIDTH" in c)
    preambles = [i for i, c in enumerate(ups) if c.endswith("PREAMBLE?")]
    assert preambles[0] < width < preambles[1]
    assert "WORD" in ups[width]  # adc_bit 12 > 8


def test_response_header_before_block():
    """Parse block responses with or without a text prefix."""
    _fake, frames = _fetch(sum_frames=2, header=b"C1:WF ")
    assert [f["frame"] for f in frames] == [1, 2]
    assert frames[1]["raw"][0] == 32768 + 2 * FRAME_BASE


def test_late_block_terminator_does_not_shift_the_next_text_reply():
    fake = FakeScope(sum_frames=1, sequence=False, fragment_block_terminator=True)
    try:
        frames = siglent_lan.fetch("127.0.0.1", "C1", port=fake.port, timeout=5)
        assert len(frames) == 1
        assert frames[0]["unit"] == "V"
        assert frames[0]["sample_rate"] == 1.0 / INTERVAL
    finally:
        fake.close()


def test_runaway_prefix_is_an_error():
    fake = FakeScope(header=b"x" * 200)
    try:
        try:
            siglent_lan.fetch("127.0.0.1", "C1", port=fake.port, timeout=5)
            raise AssertionError("expected ValueError for a missing block header")
        except ValueError as e:
            assert "no block header within" in str(e)
    finally:
        fake.close()


def test_big_endian_payload():
    _fake, frames = _fetch(sum_frames=1, sequence=False, order=1)
    assert frames[0]["raw"][1] == 32768 + FRAME_BASE + 1


def test_descriptor_stamp_decoded():
    _fake, frames = _fetch(sum_frames=1, sequence=False, stamp=1.5)
    assert frames[0]["descriptor_stamp"] == (2026, 8, 21, 12, 30, 1.5)


def test_missing_descriptor_stamp_tail_is_not_fatal():
    fake = FakeScope(sum_frames=1, sequence=False, stamp=None)
    try:
        f = siglent_lan.fetch("127.0.0.1", "C1", port=fake.port, timeout=5)[0]
        assert f["descriptor_stamp"] is None
    finally:
        fake.close()


def test_zeroed_descriptor_names_the_real_cause():
    """Report a zeroed descriptor as no addressable waveform."""
    fake = FakeScope(cpd=0.0, npoints=0, adc_bit=0)
    try:
        try:
            siglent_lan.fetch("127.0.0.1", "C1", port=fake.port, timeout=5)
            raise AssertionError("expected ValueError for a zeroed descriptor")
        except ValueError as e:
            assert "no addressable waveform" in str(e)
    finally:
        fake.close()


def test_nonfinite_descriptor_interval_is_tolerated():
    """Ignore a bad descriptor interval when the sample-rate query succeeds."""
    _fake, frames = _fetch(sum_frames=1, sequence=False, interval=float("inf"))
    assert np.isclose(frames[0]["sample_rate"], 1.0 / INTERVAL, rtol=1e-9)
    assert math.isfinite(frames[0]["t0"])
    assert np.all(np.diff(siglent_bin.time_axis(frames[0])) > 0)


def test_short_transfer_is_an_error():
    fake = FakeScope(sum_frames=1, sequence=False, short_data=True)
    try:
        try:
            siglent_lan.fetch("127.0.0.1", "C1", port=fake.port, timeout=5)
            raise AssertionError("expected ValueError for a short transfer")
        except ValueError as e:
            assert "short transfer" in str(e)
    finally:
        fake.close()


def test_bad_descriptor_rejected():
    d = bytearray(_descriptor(1))
    struct.pack_into("<f", d, 0xA4, 0.0)  # cpd 0 but points present
    try:
        siglent_lan._desc(bytes(d))
        raise AssertionError("expected ValueError mentioning code_per_div")
    except ValueError as e:
        assert "code_per_div" in str(e)
    try:
        siglent_lan._desc(b"\0" * 10)
        raise AssertionError("expected ValueError for a short descriptor")
    except ValueError as e:
        assert "descriptor is 10 bytes" in str(e)


def test_unit_comes_from_the_channel_query():
    """Query the unit from the selected channel."""
    fake, frames = _fetch(source="C3", sum_frames=1, sequence=False)
    assert ":CHANnel3:UNIT?" in fake.commands
    assert all(f["unit"] == "V" for f in frames)


def test_amps_unit_is_reported_and_rescales_nothing():
    """Report amps without changing the conversion arithmetic."""
    _fake, volts = _fetch(sum_frames=1, sequence=False)
    _fake2, amps = _fetch(sum_frames=1, sequence=False, unit=b"A")
    assert amps[0]["unit"] == "A" and volts[0]["unit"] == "V"
    assert amps[0]["vdiv"] == volts[0]["vdiv"] and amps[0]["probe"] == volts[0]["probe"]
    assert np.array_equal(amps[0]["values"], volts[0]["values"])


def test_every_frame_of_a_sequence_carries_the_unit():
    _fake, frames = _fetch(sum_frames=3, unit=b"A")
    assert [f["unit"] for f in frames] == ["A", "A", "A"]


def test_unknown_unit_is_none_not_guessed_volts():
    """Return None rather than volts when no unit query exists."""
    fake, frames = _fetch(source="F1", sum_frames=1, sequence=False)
    assert frames[0]["unit"] is None
    assert not any("UNIT?" in c.upper() for c in fake.commands)


def test_silent_unit_query_is_not_fatal():
    """Tolerate a unit query that times out without a reply."""
    fake = FakeScope(sum_frames=1, sequence=False, unit=None)
    try:
        f = siglent_lan.fetch("127.0.0.1", "C1", port=fake.port, timeout=0.5)[0]
        assert f["unit"] is None
        assert f["npoints"] == NPOINTS  # the rest of the read still completed
    finally:
        fake.close()


def test_implausible_unit_reply_is_rejected():
    """Reject a unit reply that is not a short alphabetic label."""
    _fake, frames = _fetch(sum_frames=1, sequence=False, unit=b"1.234E+00")
    assert frames[0]["unit"] is None


def test_stride_is_reset_before_every_read():
    """Reset persistent transfer stride before reading the preamble."""
    fake, _ = _fetch(sum_frames=1, sequence=False)
    ups = [c.upper() for c in fake.commands]
    assert ":WAVEFORM:INTERVAL 1" in ups
    first_preamble = next(i for i, c in enumerate(ups) if c.endswith("PREAMBLE?"))
    assert ups.index(":WAVEFORM:INTERVAL 1") < first_preamble


def test_a_stride_that_survives_the_reset_is_fatal():
    """Reject a descriptor that still reports a stride after the reset."""
    fake = FakeScope(sum_frames=1, sequence=False, data_interval=7)
    try:
        try:
            siglent_lan.fetch("127.0.0.1", "C1", port=fake.port, timeout=5)
            raise AssertionError("expected ValueError for a surviving stride")
        except ValueError as e:
            assert "reports 7" in str(e) and "wrong by that factor" in str(e)
    finally:
        fake.close()


def test_unpopulated_stride_field_is_not_an_error():
    """Accept 0 as an unpopulated stride field."""
    _fake, frames = _fetch(sum_frames=1, sequence=False, data_interval=0)
    assert frames[0]["npoints"] == NPOINTS
    assert np.all(np.diff(siglent_bin.time_axis(frames[0])) > 0)


def test_descriptor_audit_checks_the_centre_referred_delay():
    fake = FakeScope()
    try:
        result = siglent_lan.descriptor_audit("127.0.0.1", port=fake.port, timeout=5)
    finally:
        fake.close()
    expected = TIME_DELAY + (0.5 - REF_POS / 100) * TIME_DIV * siglent_lan.GRID
    assert result["grid"] == siglent_lan.GRID
    assert result["reference_position"] == REF_POS
    assert result["reference_strategy"] == "DELay"
    assert np.isclose(result["expected_descriptor_delay"], expected)
    assert np.isclose(result["descriptor_delay"], expected)
    assert abs(result["descriptor_delay_error"]) < 1e-15
    assert result["descriptor_delay_matches"] is True


def test_a_fetched_frame_round_trips_through_a_bin_file():
    """Round-trip a fetched frame through the archive codec bit for bit."""
    import tempfile

    _fake, frames = _fetch(sum_frames=1, sequence=False)
    f = frames[0]
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "f.bin")
        siglent_bin.write(out, f)
        # write() canonicalises ref_position into the stored time_delay.
        r = siglent_bin.read(out)
        assert np.array_equal(r["raw"], f["raw"])
        assert np.array_equal(r["values"], f["values"])
        assert r["vdiv"] == f["vdiv"] and r["probe"] == f["probe"]
        assert r["code_per_div"] == int(f["code_per_div"])
        assert r["unit"] == f["unit"]
        assert np.isclose(r["t0"], f["t0"], rtol=0, atol=1e-15)
        assert np.allclose(siglent_bin.time_axis(r), siglent_bin.time_axis(f), rtol=0, atol=1e-15)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
