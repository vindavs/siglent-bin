#!/usr/bin/env python3
"""Live SCPI waveform capture for Siglent SDS800X HD oscilloscopes.

Returns the same trace mapping as ``siglent_bin.read()`` and reads sequence
acquisitions one frame at a time. Live signed samples are converted to the
file codec's offset-binary convention; unit and time geometry come from SCPI
queries. Model- and firmware-specific behavior is documented in SPEC.md.
"""

import datetime
import math
import socket
import struct

import numpy as np

PORT = 5025
DESC_BYTES = 346  # descriptor length; clock-like diagnostic records may follow
STAMP_BYTES = 16  # one clock-like record, not an acquisition time
GRID = 10  # horizontal divisions; not carried in the descriptor


class Scope:
    """A SCPI connection to the scope. Use as a context manager."""

    def __init__(self, host, port=PORT, timeout=15.0):
        self.host, self.port = host, port
        self._buf = bytearray()
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _fill(self):
        chunk = self._sock.recv(1 << 20)
        if not chunk:
            raise ConnectionError(f"{self.host}: connection closed by scope")
        self._buf += chunk

    def _take(self, n):
        while len(self._buf) < n:
            self._fill()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def write(self, cmd):
        self._sock.sendall(cmd.encode("ascii") + b"\n")

    def query(self, cmd):
        """Return the next non-empty text response to ``cmd``."""
        self.write(cmd)
        while True:
            while b"\n" not in self._buf:
                self._fill()
            i = self._buf.index(b"\n")
            line = bytes(self._buf[:i]).strip(b"\x00\r \t")
            del self._buf[: i + 1]
            if line:
                return line.decode("ascii", "replace").strip()

    def query_block(self, cmd, max_prefix=64):
        """Return an IEEE 488.2 definite-length block, skipping any text prefix."""
        self.write(cmd)
        prefix = bytearray()
        while True:
            b = self._take(1)
            if b == b"#":
                break
            prefix += b
            if len(prefix) > max_prefix:
                raise ValueError(
                    f"{self.host}: no block header within {max_prefix} bytes of "
                    f"the reply to {cmd!r}; got {bytes(prefix[:40])!r}"
                )
        ndigits = self._take(1)
        if not ndigits.isdigit():
            raise ValueError(f"{self.host}: bad block length-of-length {ndigits!r}")
        length = int(self._take(int(ndigits)))
        data = self._take(length)
        while self._buf[:1] in (b"\n", b"\r"):  # trailing terminator
            del self._buf[:1]
        return data


def _desc(d):
    """Decode the waveform descriptor fields used by this module."""
    if len(d) < DESC_BYTES:
        raise ValueError(f"descriptor is {len(d)} bytes, expected >= {DESC_BYTES}")

    def g(fmt, a):
        return struct.unpack_from("<" + fmt, d, a)[0]

    cpd = g("f", 0xA4)
    npoints = g("i", 0x74)
    if cpd == 0 and npoints == 0 and g("h", 0xAC) == 0:
        # A zeroed descriptor means that no waveform is addressable.
        raise ValueError(
            "the scope reports no addressable waveform (descriptor is zeroed: "
            "no points, no code_per_div, no adc_bit). Either no acquisition has "
            "completed -- :TRIGger:MODE NORMal with nothing on the trigger source "
            "sits at `Ready` forever -- or, in sequence mode, the segment buffer "
            "holds nothing to read."
        )
    if cpd == 0:
        raise ValueError("code_per_div is 0")
    return {
        "data_width": g("h", 0x20),  # 1 = 16-bit, 0 = 8-bit
        "data_order": g("h", 0x22),  # 1 = MSB, 0 = LSB
        "npoints": npoints,  # points per frame
        # Requested transfer stride; 0 if this firmware leaves it unset.
        "data_interval": g("i", 0x88),
        "read_frames": g("i", 0x90),  # frames in this transfer
        "sum_frames": g("i", 0x94),  # frames the acquisition holds
        "vdiv": g("f", 0x9C),  # pre-probe
        "voff": g("f", 0xA0),  # pre-probe
        "code_per_div": cpd,
        "adc_bit": g("h", 0xAC),
        # Sample-rate fallback when :ACQuire:SRATe? is unavailable.
        "interval": g("f", 0xB0),
        # Diagnostic fields; the timebase index is model-dependent.
        "desc_delay": g("d", 0xB4),
        "desc_tdiv_index": g("h", 0x144),
        "probe": g("f", 0x148),
        "stamps": d[DESC_BYTES:],
    }


def _descriptor_stamp_or_none(b):
    """Decode the leading clock-like descriptor record, or None.

    On SDS814X HD fw 4.8.12.1.1.6.5 the fields decode as a date/time but do not
    represent acquisition time. Its update mechanism remains unresolved; keep
    it only as diagnostic descriptor data (see SPEC.md)."""
    return _decode_descriptor_stamp(b[:STAMP_BYTES]) if len(b) >= STAMP_BYTES else None


def _decode_descriptor_stamp(b):
    """Decode a 16-byte descriptor record to a date/time-shaped tuple."""
    seconds = struct.unpack_from("<d", b, 0)[0]
    minute, hour, day, month = b[8], b[9], b[10], b[11]
    year = struct.unpack_from("<h", b, 12)[0]
    return (year, month, day, hour, minute, seconds)


def _query_unit(s, source):
    """Return a channel's displayed unit, or None if it cannot be queried."""
    if not (len(source) > 1 and source[0] in "Cc" and source[1:].isdigit()):
        return None
    try:
        resp = s.query(f":CHANnel{source[1:]}:UNIT?")
    except OSError:
        return None
    resp = resp.strip()
    return resp if resp.isalpha() and len(resp) <= 4 else None


def fetch(host, source="C1", port=PORT, timeout=25.0, apply_probe=True, grid=GRID, stop=True):
    """Return every frame of the current acquisition as canonical trace mappings.

    ``stop`` sends ``:TRIGger:STOP`` before reading. This makes completed
    sequence buffers addressable on the tested firmware; use ``stop=False`` to
    avoid disturbing a running acquisition.
    """
    with Scope(host, port, timeout) as s:
        if stop:
            s.write(":TRIGger:STOP")
        sequence = s.query(":ACQuire:SEQuence?").upper().startswith("ON")
        s.write(f":WAVeform:SOURce {source}")
        s.write(":WAVeform:STARt 0")
        s.write(":WAVeform:POINt 0")
        # Reset persistent transfer stride before every read.
        s.write(":WAVeform:INTerval 1")

        # Scaling fields depend on transfer width, so settle width before reuse.
        head = _desc(s.query_block(":WAVeform:PREamble?"))
        # Reject a stride that survived the reset; 0 means the field is unset.
        if head["data_interval"] > 1:
            raise ValueError(
                f"{host}: asked for :WAVeform:INTerval 1 but the descriptor "
                f"reports {head['data_interval']} -- the scope is decimating the "
                f"transfer and the time axis would be wrong by that factor. If "
                f"this scope is known not to decimate, then 0x88 is not the "
                f"stride on this firmware (see SPEC.md)."
            )
        s.write(":WAVeform:WIDTh " + ("WORD" if head["adc_bit"] > 8 else "BYTE"))

        # Unit is per channel, not per frame.
        unit = _query_unit(s, source)

        # The descriptor's timebase enum is model-dependent; query the scale.
        time_div = float(s.query(":TIMebase:SCALe?"))
        time_delay = float(s.query(":TIMebase:DELay?"))
        # Horizontal reference position places t=0 within the record.
        try:
            ref_pos = float(s.query(":TIMebase:REFerence:POSition?"))
        except (ValueError, OSError):
            ref_pos = 50.0
        if not (math.isfinite(ref_pos) and 0.0 <= ref_pos <= 100.0):
            ref_pos = 50.0
        try:
            ref_strategy = s.query(":TIMebase:REFerence?")
        except OSError:
            ref_strategy = ""
        try:
            fs = float(s.query(":ACQuire:SRATe?"))
        except (ValueError, OSError):
            fs = 0.0
        if not (math.isfinite(fs) and fs > 0):
            d_int = head["interval"]
            if not (math.isfinite(d_int) and d_int > 0):
                raise ValueError(
                    f"{host}: no usable sample rate -- :ACQuire:SRATe? gave {fs!r} "
                    f"and the descriptor interval is {d_int!r}"
                )
            fs = 1.0 / d_int
        # t=0 is ref_pos% of the screen span, offset by time_delay.
        t_zero = (ref_pos / 100.0) * time_div * grid - time_delay
        total = max(head["sum_frames"], 1) if sequence else 1

        out = []
        for n in range(1, total + 1):
            if sequence:
                # The tested firmware supports only one-at-a-time <frame>,1 reads.
                s.write(f":WAVeform:SEQuence {n},1")
            d = _desc(s.query_block(":WAVeform:PREamble?"))
            payload = s.query_block(":WAVeform:DATA?")
            wide = d["adc_bit"] > 8
            dt = (">i2" if d["data_order"] else "<i2") if wide else "i1"
            need = d["npoints"] * (2 if wide else 1)
            if len(payload) < need:
                raise ValueError(
                    f"{host}: {len(payload)} data bytes for {d['npoints']} "
                    f"samples -- short transfer"
                )

            # vdiv/voff are pre-probe; apply the probe only to values.
            vdiv, voff = d["vdiv"], d["voff"]
            scale = d["probe"] if apply_probe else 1.0
            # Match siglent_bin.read()'s float32 expression order for exact archives.
            code = np.frombuffer(payload, dtype=dt, count=d["npoints"]).astype(np.float32)
            out.append(
                {
                    "source": source,
                    "frame": n,
                    "frames_total": total,
                    "sequence": sequence,
                    "descriptor_stamp": _descriptor_stamp_or_none(d["stamps"]),
                    "sample_rate": fs,
                    "time_div": time_div,
                    "time_delay": time_delay,
                    "grid": grid,
                    "unit": unit,
                    "npoints": d["npoints"],
                    "ref_position": ref_pos,
                    "ref_strategy": ref_strategy,
                    "data_width": d["data_width"],
                    "vdiv": vdiv,
                    "voff": voff,
                    "code_per_div": d["code_per_div"],
                    "probe": d["probe"],
                    "desc_delay": d["desc_delay"],
                    "raw": (code + 32768).astype(np.uint16),
                    "values": (code * vdiv / d["code_per_div"] - voff) * scale,
                    "t0": -t_zero,
                }
            )
        return out


def sync_clock(host, when=None, port=PORT, timeout=15.0):
    """Set the scope's session clock, defaulting to the host's local time.

    Returns the ``before`` and ``after`` readbacks as ``(YYYYMMDD, HHMMSS)``.
    """
    when = when or datetime.datetime.now()
    with Scope(host, port, timeout) as s:
        before = (s.query(":SYSTem:DATE?"), s.query(":SYSTem:TIME?"))
        s.write(f":SYSTem:DATE {when:%Y%m%d}")
        s.write(f":SYSTem:TIME {when:%H%M%S}")
        after = (s.query(":SYSTem:DATE?"), s.query(":SYSTem:TIME?"))
    return {"before": before, "after": after, "set_to": when.strftime("%Y%m%d %H%M%S")}


def fetch_group(host, sources=("C1", "C2", "C3", "C4"), **kw):
    """Fetch each enabled source and return ``{source: [frames]}``."""
    out = {}
    with Scope(host, kw.get("port", PORT), kw.get("timeout", 15.0)) as s:
        on = [c for c in sources if s.query(f":CHANnel{c[1:]}:SWITch?").upper().startswith("ON")]
    for c in on:
        out[c] = fetch(host, c, **kw)
    return out


def descriptor_audit(host, port=PORT, timeout=15.0):
    """Compare the descriptor's centre-referred delay with queried settings."""
    with Scope(host, port, timeout) as s:
        tdiv = float(s.query(":TIMebase:SCALe?"))
        delay = float(s.query(":TIMebase:DELay?"))
        ref_position = float(s.query(":TIMebase:REFerence:POSition?"))
        ref_strategy = s.query(":TIMebase:REFerence?")
        s.write(":WAVeform:STARt 0")
        s.write(":WAVeform:POINt 0")
        s.write(":WAVeform:INTerval 1")
        d = _desc(s.query_block(":WAVeform:PREamble?"))
    expected = delay + (0.5 - ref_position / 100.0) * tdiv * GRID
    error = d["desc_delay"] - expected
    tol = max(1e-12, abs(expected) * 1e-6)
    return {
        "timebase_scale": tdiv,
        "timebase_delay": delay,
        "grid": GRID,
        "reference_position": ref_position,
        "reference_strategy": ref_strategy,
        "descriptor_delay": d["desc_delay"],
        "expected_descriptor_delay": expected,
        "descriptor_delay_error": error,
        "descriptor_delay_matches": abs(error) <= tol,
        "descriptor_tdiv_index": d["desc_tdiv_index"],
        "sample_interval": d["interval"],
        # 1 means no stride; 0 means the field is unset.
        "data_interval": d["data_interval"],
    }


def _main(argv=None):
    import sys

    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: siglent-lan HOST [SOURCE ...] [--sync-clock]", file=sys.stderr)
        sys.exit(2)
    # Clock synchronisation is opt-in.
    sync = "--sync-clock" in args
    args = [a for a in args if a != "--sync-clock"]
    host, sources = args[0], args[1:] or ["C1"]
    if sync:
        r = sync_clock(host)
        print(f"{host} clock {' '.join(r['before'])} -> {' '.join(r['after'])}")
    for src in sources:
        try:
            frames = fetch(host, src)
        except Exception as e:  # noqa: BLE001 - name the host/source that failed, then exit
            print(f"{host} {src}: {e}", file=sys.stderr)
            sys.exit(1)
        for f in frames:
            v = f["values"]
            u = f["unit"] or "?"  # Keep unknown units distinct from volts.
            print(
                f"{host} {f['source']} frame {f['frame']}/{len(frames)}  "
                f"{f['sample_rate'] / 1e3:g} kSa/s  {f['npoints']} pts  "
                f"{f['vdiv']:g} {u}/div  probe {f['probe']:g}x  "
                f"{v.min():+.3f}..{v.max():+.3f} {u}  stamp={f['descriptor_stamp']}"
            )


if __name__ == "__main__":
    _main()
