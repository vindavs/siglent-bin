#!/usr/bin/env python3
"""Codec for Siglent "Binary Format V4.0" oscilloscope waveform files (.bin).

Reading is verified on an SDS814X from the **SDS800X HD** family. The codec
parses the 4 KB header and returns a channel's samples in real units — volts, or
amps for a channel in amps display mode — plus `t0`, the time of sample 0; call
`time_axis()` for the full per-sample time array. Pure numpy.

Why this exists: samples are stored as **offset-binary uint16 centred at 32768**
(16-bit "HD" capture). Reading them as *signed* int16 silently inverts any trace
whose two levels straddle 32768 (e.g. an idle-low level below it and an
active-high level above it). `raw_uint16()` gives the polarity-correct samples
for a threshold/PWM decoder.

Handles analog channels (``..._C1.bin``), math traces (``..._F1.bin``) and zoom
saves (``..._Z1.bin`` etc., one per zoomed channel — a slice of the parent
record; the time axis comes from the stored zoom timebase). Multi-channel: the SDS814X saves each trace of a
multi-channel acquisition to its **own single-trace file** (``..._C1_30.bin``,
``..._C2_30.bin``, ...); use `read_group()` to load a set back onto one time
axis.

Format reference: Siglent, "How to Extract Data from the Binary File of SIGLENT
Oscilloscope" (V4.0). Header offsets and the code->volts formula are documented
there; see SPEC.md for the field table with fact-vs-inference marked.
"""

import contextlib
import glob
import gzip
import math
import os
import re
import secrets
import stat
import struct
import zlib
from collections.abc import Mapping
from numbers import Real
from typing import TypedDict

import numpy as np

HEADER_BYTES = 0x1000
_INT32_MAX = (1 << 31) - 1


class Trace(TypedDict, total=False):
    """Waveform fields shared by the file, LAN and srzip paths.

    ``descriptor_stamp`` is diagnostic data, not an acquisition timestamp.
    """

    source: str
    sample_rate: float
    time_div: float
    time_delay: float
    grid: int
    npoints: int
    data_width: int
    vdiv: float
    voff: float
    code_per_div: int | float
    probe: float
    unit: str | None
    unit_raw: tuple[int, int, int, int, int, int, int]
    zoom: bool
    ref_position: float
    raw: np.ndarray
    values: np.ndarray
    t0: float
    descriptor_stamp: tuple[int, int, int, int, int, float] | None
    frame: int
    frames_total: int
    sequence: bool
    ref_strategy: str
    desc_delay: float


def _open_atomic_temp(destination):
    """Create a sibling temporary using the umask or the destination's mode."""
    try:
        destination_mode = stat.S_IMODE(os.stat(destination).st_mode)
    except FileNotFoundError:
        destination_mode = None
    parent = os.path.dirname(destination)
    basename = os.path.basename(destination)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for _ in range(100):
        temporary = os.path.join(parent, f".{basename}.{secrets.token_hex(16)}")
        try:
            fd = os.open(temporary, flags, 0o666)
        except FileExistsError:
            continue
        try:
            if destination_mode is not None:
                os.fchmod(fd, destination_mode)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
        return fd, temporary
    raise FileExistsError(f"could not allocate a temporary file beside {destination!r}")


def _f64(h, a):
    return struct.unpack("<d", h[a : a + 8])[0]


def _i32(h, a):
    return struct.unpack("<i", h[a : a + 4])[0]


# Named unit types (first int32 of the unit descriptor, per Siglent's doc; type 0
# means the unit is composed of powers of V, A and s instead).
_UNIT_TYPES = {
    1: "dBV",
    2: "dBA",
    3: "dB",
    4: "Vpp",
    5: "Vdc",
    6: "dBm",
    7: "Sa",
    8: "div",
    9: "pts",
    10: "",
    11: "deg",
    12: "%",
}


def _dwu(h, a):
    """Decode a 'Data-With-Unit': float64 value at a, SI-prefix index int32 at a+8.
    Index 8 = base (x1), 7 = milli, 6 = micro, 9 = kilo, 10 = mega, ..."""
    m = _i32(h, a + 8)
    if not 0 <= m <= 16:  # the doc's magnitude table: yocto..yotta
        raise ValueError(f"magnitude index {m} at {a + 8:#x} outside 0..16")
    return _f64(h, a) * 10.0 ** (3 * (m - 8))


def _unit(h, a):
    """(label, raw_descriptor) for a Data-With-Unit at a. Per the format doc the
    descriptor is 7 int32s: [type, V_num, V_den, A_num, A_den, s_num, s_den] —
    type 0 composes the unit from rational powers of V, A and s, so plain volts
    is (0, 1,1, 0,1, 0,1) (spaces grouping the num,den pairs); other types are
    named units (dBV, Vpp, ...)."""
    d = tuple(_i32(h, a + 0x0C + 4 * i) for i in range(7))
    if d[0] != 0:
        return _UNIT_TYPES.get(d[0], "unknown"), d
    parts = []
    for sym, num, den in (("V", d[1], d[2]), ("A", d[3], d[4]), ("s", d[5], d[6])):
        if num == 0:
            continue
        p = num / (den if den else 1)
        parts.append(sym if p == 1 else f"{sym}^{p:g}")
    return "*".join(parts), d


# Unit descriptors are [type, V_num, V_den, A_num, A_den, s_num, s_den].
# Type 0 composes powers of V, A and s.
_UNIT_DESCRIPTORS = {
    "V": (0, 1, 1, 0, 1, 0, 1),
    "A": (0, 0, 1, 1, 1, 0, 1),
    "s": (0, 0, 1, 0, 1, 1, 1),
}
_SAMPLES_DESCRIPTOR = (7, 0, 1, 0, 1, 0, 1)  # named unit "Sa"


def _pack_dwu(h, a, value, descriptor):
    """Write an unscaled Data-With-Unit with magnitude index 8 (x1)."""
    struct.pack_into("<d", h, a, float(value))
    struct.pack_into("<i", h, a + 8, 8)
    struct.pack_into("<7i", h, a + 0x0C, *descriptor)


def _as_finite(trace, key, path, *, positive=False):
    value = trace.get(key)
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{path}: {key} must be a finite number, got {value!r}")
    try:
        value = float(value)
    except (OverflowError, ValueError) as e:
        raise ValueError(f"{path}: {key} must be a finite number, got {value!r}") from e
    if not math.isfinite(value) or (positive and value <= 0):
        condition = "finite and > 0" if positive else "finite"
        raise ValueError(f"{path}: {key} must be {condition}, got {value!r}")
    return value


def _as_int32(value, label, path, *, positive=False):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{path}: {label} must be an integer, got {value!r}")
    try:
        f = float(value)
    except (OverflowError, ValueError) as e:
        raise ValueError(f"{path}: {label} must be an integer, got {value!r}") from e
    if not math.isfinite(f) or not f.is_integer():
        raise ValueError(f"{path}: {label} must be an integer, got {value!r}")
    value = int(f)
    low = 1 if positive else -(1 << 31)
    if not low <= value <= _INT32_MAX:
        condition = "a positive int32" if positive else "an int32"
        raise ValueError(f"{path}: {label} must be {condition}, got {value!r}")
    return value


def _unit_descriptor(trace, unit, path):
    """Return a validated 7-int descriptor from ``unit_raw`` or a known label.

    Unknown units are rejected because the format has no unknown-unit value.
    """
    if unit is None:
        raw = trace.get("unit_raw")
        if raw is not None:
            try:
                raw = tuple(raw)
            except TypeError as e:
                raise ValueError(f"{path}: unit_raw must be an iterable of 7 ints") from e
            if len(raw) != 7:
                raise ValueError(f"{path}: unit_raw must be 7 ints, got {raw!r}")
            return tuple(_as_int32(v, f"unit_raw[{i}]", path) for i, v in enumerate(raw))
        unit = trace.get("unit")
    if unit in _UNIT_DESCRIPTORS:
        return _UNIT_DESCRIPTORS[unit]
    raise ValueError(
        f"{path}: cannot write a unit descriptor for unit {unit!r}: pass unit= as one of "
        f"{sorted(_UNIT_DESCRIPTORS)}, or supply the trace's 7-int `unit_raw`. "
        "Volts is not assumed for an unknown unit."
    )


def write(path, trace, unit=None):
    """Write a canonical V4 archive from one analog ``read()`` or live frame.

    Output is a 4096-byte header followed by 16-bit little-endian ``raw``
    samples. Math, zoom, 8-bit, fractional ``code_per_div`` and unknown-unit
    traces are rejected. Only fields understood by this package are preserved;
    scope import has not been verified.

    The format has no horizontal ``ref_position`` field, so ``time_delay`` is
    stored at a 50% reference to preserve ``t0`` on a default read.
    """
    path_text = os.fsdecode(path)
    if not isinstance(trace, Mapping):
        raise ValueError(f"{path_text}: trace must be a mapping, got {type(trace).__name__}")
    source = trace.get("source")
    if not (isinstance(source, str) and re.fullmatch(r"[Cc]([1-8])", source)):
        raise ValueError(
            f"{path_text}: can only write analog channels C1-C8, got source {source!r} "
            "(math and zoom traces are not supported)"
        )
    if trace.get("zoom"):
        raise ValueError(
            f"{path_text}: {source} is a zoom save; writing one back needs the zoom "
            "timebase fields, which this writer does not fill in"
        )
    unit_raw = _unit_descriptor(trace, unit, path_text)

    width = _as_int32(trace.get("data_width", 1), "data_width", path_text)
    if width != 1:
        raise ValueError(
            f"{path_text}: write() supports only 16-bit traces (data_width=1); "
            f"got data_width={width}. An 8-bit read uses codes centred on 128 "
            "and cannot be promoted by copying them."
        )

    if "raw" not in trace:
        raise ValueError(f"{path_text}: trace is missing required field 'raw'")
    raw = np.asarray(trace["raw"])
    if raw.ndim != 1 or raw.size == 0:
        raise ValueError(f"{path_text}: `raw` must be a non-empty 1-D array, got shape {raw.shape}")
    if not np.issubdtype(raw.dtype, np.integer):
        # Do not silently truncate calibrated values into raw codes.
        raise ValueError(
            f"{path_text}: `raw` must hold integer codes, got dtype {raw.dtype} -- pass the "
            "trace's `raw`, not its `values`"
        )
    if raw.min() < 0 or raw.max() > 0xFFFF:
        raise ValueError(f"{path_text}: `raw` values {raw.min()}..{raw.max()} do not fit uint16")
    npoints = int(raw.size)
    if npoints > _INT32_MAX:
        raise ValueError(f"{path_text}: raw holds {npoints} samples, beyond the int32 format")
    if (
        "npoints" in trace
        and _as_int32(trace["npoints"], "npoints", path_text, positive=True) != npoints
    ):
        raise ValueError(
            f"{path_text}: npoints says {trace['npoints']} but `raw` holds {npoints} samples"
        )

    cpd = _as_int32(trace.get("code_per_div"), "code_per_div", path_text, positive=True)
    grid = _as_int32(trace.get("grid"), "grid", path_text, positive=True)
    sample_rate = _as_finite(trace, "sample_rate", path_text, positive=True)
    time_div = _as_finite(trace, "time_div", path_text, positive=True)
    time_delay = _as_finite(trace, "time_delay", path_text)
    t0 = _as_finite(trace, "t0", path_text)
    vdiv = _as_finite(trace, "vdiv", path_text, positive=True)
    voff = _as_finite(trace, "voff", path_text)
    probe = _as_finite(trace, "probe", path_text, positive=True)
    ref_position = None
    if "ref_position" in trace:
        ref_position = _as_finite(trace, "ref_position", path_text)
        if not 0 <= ref_position <= 100:
            raise ValueError(
                f"{path_text}: ref_position must be between 0 and 100, got {ref_position}"
            )
        expected_t0 = time_delay - (ref_position / 100.0) * time_div * grid
        if not math.isclose(t0, expected_t0, rel_tol=1e-12, abs_tol=1e-15):
            raise ValueError(
                f"{path_text}: t0 {t0!r} is inconsistent with time_delay {time_delay!r}, "
                f"ref_position {ref_position!r}, time_div {time_div!r}, and grid {grid}"
            )
    # Preserve an already-centred delay; canonicalise all other inputs.
    stored_time_delay = time_delay if ref_position == 50 else t0 + 0.5 * time_div * grid
    if not math.isfinite(stored_time_delay):
        raise ValueError(f"{path_text}: canonical time_delay is not finite")

    c = int(source[1:]) - 1
    # CH1-4 and CH5-8 use separate header banks of the same shape.
    vd_a, vo_a, pr_a, cpd_a, b = (
        (0x18, 0xB8, 0x244, 0x270, c) if c < 4 else (0x414, 0x4B4, 0x554, 0x574, c - 4)
    )

    h = bytearray(HEADER_BYTES)
    struct.pack_into("<i", h, 0x00, 4)  # version: V4.0
    struct.pack_into("<i", h, 0x04, HEADER_BYTES)  # data offset
    on_a = 0x08 + 4 * c if c < 4 else 0x404 + 4 * (c - 4)
    struct.pack_into("<i", h, on_a, 1)  # this channel enabled, all others 0

    _pack_dwu(h, vd_a + 0x28 * b, vdiv, unit_raw)  # pre-probe, as read() returns it
    _pack_dwu(h, vo_a + 0x28 * b, voff, unit_raw)
    struct.pack_into("<d", h, pr_a + 8 * b, probe)
    struct.pack_into("<i", h, cpd_a + 4 * b, cpd)

    struct.pack_into("<i", h, 0x1EC, npoints)
    _pack_dwu(h, 0x1F0, sample_rate, _SAMPLES_DESCRIPTOR)
    _pack_dwu(h, 0x19C, time_div, _UNIT_DESCRIPTORS["s"])
    _pack_dwu(h, 0x1C4, stored_time_delay, _UNIT_DESCRIPTORS["s"])
    h[0x264] = 1  # data_width: 16-bit
    h[0x265] = 0  # byte_order: little-endian
    struct.pack_into("<i", h, 0x26C, grid)
    struct.pack_into("<i", h, 0xAF4, 0)  # zoom_switch: an ordinary save

    # Replace only after finalising and syncing the deterministic gzip stream.
    destination = os.path.abspath(path_text)
    fd, temporary = _open_atomic_temp(destination)
    try:
        with os.fdopen(fd, "wb") as base:
            if path_text.endswith(".gz"):
                with gzip.GzipFile(filename="", mode="wb", fileobj=base, mtime=0) as f:
                    f.write(h)
                    f.write(np.ascontiguousarray(raw, dtype="<u2").tobytes())
            else:
                base.write(h)
                base.write(np.ascontiguousarray(raw, dtype="<u2").tobytes())
            base.flush()
            os.fsync(base.fileno())
        os.replace(temporary, destination)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return path


def read(path, apply_probe=True, ref_position=50.0):
    """Parse one Siglent V4.0 .bin (a single enabled trace). Returns a dict:

        source        -> which trace this file holds: 'C1'..'C8' or 'F1'..'F4',
        sample_rate, time_div, time_delay, grid, npoints, data_width,
        vdiv, voff, code_per_div, probe,
        unit          -> unit of the values, decoded from the descriptor
                         ('V', 'A', ...),
        unit_raw      -> the raw 7-int unit descriptor,
        zoom          -> True for a zoom (Z) save; the time axis then comes from
                         the stored zoom timebase (held in time_div/time_delay),
        ref_position  -> the value used to place t = 0 (echoed back),
        raw           -> samples as offset-binary uint16 (polarity-correct),
        values        -> samples converted to `unit` (volts for 'V', amps for 'A'),
                         as float32 (see below),
        t0            -> seconds, the time of sample 0 (float64). Call
                         `time_axis(d)` for the full per-sample array.

    `values` is `((code-center)*vdiv/cpd - voff) * probe` — the scope's on-screen
    reading. Calibrated against known 0/3/4.5/5 V references across vertical
    settings; the offset term is `- voff` (see SPEC.md).
    Returned as float32; representative SDS814X voltage and current captures
    round-trip to their original 12-bit codes.

    ref_position is the scope's horizontal reference position in percent
    (`:TIMebase:REFerence:POSition`, 0-100), which fixes where the trigger sits
    in the record: `t = 0` lands at `ref_position%` of the screen span.
    ⚠️ The header does NOT store it, so absolute time is only right if the value
    passed matches what the scope was set to; the default 50 assumes screen centre.
    Sample spacing, and therefore every relative measurement, is unaffected.
    See SPEC.md.

    apply_probe (default True) multiplies by `probe` — the stored vdiv does NOT
    already include it (the swing only comes out right with it applied). In V
    mode `probe` is the probe attenuation (verified 1x/10x); in amps display
    mode it is the 1/(V/A) factor, and the physical probe attenuation is not
    separately recorded, so the value tracks the scope's reading, not
    necessarily the real circuit current. Math traces (F1-F4) have no probe
    field, so probe is 1.0; their values were verified against a math of known
    inputs to ~1 mV. For a pure threshold/PWM decode skip `values` and use
    `raw` / `raw_uint16()`.

    Raises ValueError for a file that isn't a parseable V4.0 capture (short or
    truncated file, wrong version, out-of-range header fields).
    """
    invalid_ref = f"ref_position must be a finite number from 0 to 100, got {ref_position!r}"
    if isinstance(ref_position, (bool, np.bool_)) or not isinstance(ref_position, Real):
        raise ValueError(invalid_ref)
    try:
        ref_position = float(ref_position)
    except (OverflowError, TypeError, ValueError) as e:
        raise ValueError(invalid_ref) from e
    if not math.isfinite(ref_position) or not 0 <= ref_position <= 100:
        raise ValueError(invalid_ref)

    d, stored, center = _load(path)
    samples = stored.astype(np.float32)

    # value (in `unit`) = ((code-center)*vdiv/cpd - voff) * probe. vdiv is stored PRE-probe.
    # `probe` is the voltage attenuation in V mode (verified 1x/10x), a 1/(V/A) factor in
    # amps mode.
    values = (samples - center) * d["vdiv"] / d["code_per_div"] - d["voff"]
    if apply_probe:
        values = values * d["probe"]

    # Keep only the float64 origin here; time_axis() builds t0 + i/sample_rate on demand.
    span = d["time_div"] * d["grid"]
    t0 = -(ref_position / 100.0) * span + d["time_delay"]

    d.update(raw=stored.astype(np.uint16), values=values, t0=t0, ref_position=ref_position)
    return d


def time_axis(d):
    """Build the float64 axis as ``t0 + arange(n) / sample_rate``.

    This avoids accumulating error from a pre-rounded ``dt``.
    """
    return d["t0"] + np.arange(d["npoints"], dtype=np.float64) / d["sample_rate"]


def _is_gzip(path):
    """Return whether ``path`` starts with the gzip magic bytes."""
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def _load(path):
    """Read a validated header and its declared sample payload.

    Gzip input is consumed sequentially without buffering the whole member.
    """
    if _is_gzip(path):
        try:
            with gzip.open(path, "rb") as f:
                h = f.read(HEADER_BYTES)
                d = _parse_header(h, None, path)
                off = d.pop("_data_off")
                gap = off - HEADER_BYTES
                while gap:
                    chunk = f.read(min(gap, 64 * 1024))
                    if not chunk:
                        raise ValueError(f"{path}: compressed file ends before data offset")
                    gap -= len(chunk)
                n = d["npoints"] * d.pop("_itemsize")
                payload = f.read(n)
                if len(payload) != n:
                    raise ValueError(
                        f"{path}: {len(payload)} data bytes for {d['npoints']} samples "
                        "— truncated compressed file?"
                    )
                if f.read(1):
                    raise ValueError(f"{path}: unexpected decompressed data after sample payload")
        except (gzip.BadGzipFile, EOFError, zlib.error) as e:
            raise ValueError(f"{path}: corrupt or truncated gzip stream: {e}") from e
        samples = np.frombuffer(payload, dtype=d.pop("_dt"), count=d["npoints"])
        return d, samples, d.pop("_center")
    with open(path, "rb") as f:
        h = f.read(HEADER_BYTES)
        size = os.fstat(f.fileno()).st_size
        d = _parse_header(h, size, path)
        f.seek(d.pop("_data_off"))
        payload = f.read(d["npoints"] * d.pop("_itemsize"))
        samples = np.frombuffer(payload, dtype=d.pop("_dt"), count=d["npoints"])
    return d, samples, d.pop("_center")


def _parse_header(h, file_size, path):
    """Validate a .bin's header bytes `h` against the file's total size and
    decode it: the read() result minus raw/values/t0/ref_position, plus the
    private '_dt' / '_center' / '_itemsize' / '_data_off' needed to load the
    samples."""
    if len(h) < HEADER_BYTES:
        raise ValueError(f"{path}: {len(h)} bytes, shorter than the {HEADER_BYTES}-byte header")
    version = _i32(h, 0x00)
    if version != 4:
        raise ValueError(f"{path}: header version {version}, expected 4 (V4.0)")
    data_off = _i32(h, 0x04)
    if data_off < HEADER_BYTES:
        raise ValueError(
            f"{path}: data offset {data_off:#x} is before the {HEADER_BYTES:#x}-byte header end"
        )
    if file_size is not None and data_off > file_size:
        raise ValueError(f"{path}: data offset {data_off:#x} is beyond EOF at {file_size:#x}")
    data_width = h[0x264]  # 0 = 8-bit, 1 = 16-bit
    byte_order = h[0x265]  # 0 = little-endian, 1 = big-endian
    if data_width not in (0, 1) or byte_order not in (0, 1):
        raise ValueError(
            f"{path}: data_width {data_width} / byte_order {byte_order}, each must be 0 or 1"
        )
    ch_on = [_i32(h, 0x08 + 4 * c) for c in range(4)] + [
        _i32(h, 0x404 + 4 * c) for c in range(4)
    ]  # CH5-8 (8ch models)
    math_on = [_i32(h, 0x280 + 4 * m) for m in range(4)]
    bad = [v for v in ch_on + math_on if v not in (0, 1)]
    if bad:
        raise ValueError(f"{path}: channel/math enable flags must be 0 or 1, got {bad}")
    on = [f"C{c + 1}" for c in range(8) if ch_on[c]] + [f"F{m + 1}" for m in range(4) if math_on[m]]
    if len(on) != 1:
        raise NotImplementedError(
            f"{path}: {len(on)} traces enabled {on}. The SDS800X HD writes one "
            "trace per .bin — a multi-channel acquisition is several files; use "
            "read_group(). (The format also defines sequential multi-trace "
            "files, unobserved on this scope and not implemented.)"
        )
    source = on[0]

    if source.startswith("C"):
        c = int(source[1:]) - 1
        # CH1-4 and CH5-8 live in two header banks of identical shape.
        vd_a, vo_a, pr_a, cpd_a, b = (
            (0x18, 0xB8, 0x244, 0x270, c) if c < 4 else (0x414, 0x4B4, 0x554, 0x574, c - 4)
        )
        vdiv = _dwu(h, vd_a + 0x28 * b)
        voff = _dwu(h, vo_a + 0x28 * b)
        cpd = _i32(h, cpd_a + 4 * b)  # code_per_div, SIGNED
        probe = _f64(h, pr_a + 8 * b)
        unit, unit_raw = _unit(h, vd_a + 0x28 * b)
        npoints = _i32(h, 0x1EC)
        fs = _dwu(h, 0x1F0)
    else:  # math trace F1-F4
        m = int(source[1:]) - 1
        vdiv = _dwu(h, 0x290 + 0x28 * m)  # math_vdiv_val
        voff = _dwu(h, 0x330 + 0x28 * m)  # math_vpos_val
        cpd = _i32(h, 0x400)  # math_vert_code_per_div (shared)
        probe = 1.0  # math has no probe field
        unit, unit_raw = _unit(h, 0x290 + 0x28 * m)
        npoints = _i32(h, 0x3D0 + 4 * m)  # math_store_len
        interval = _f64(h, 0x3E0 + 8 * m)  # math_f_time, s between samples
        fs = 1.0 / interval if interval > 0 else 0.0

    if cpd == 0:
        raise ValueError(f"{path}: code_per_div is 0 for {source}")
    if not (np.isfinite(fs) and fs > 0):
        raise ValueError(f"{path}: sample rate {fs}, expected finite and > 0")
    if not (np.isfinite(vdiv) and vdiv > 0):
        raise ValueError(f"{path}: vdiv {vdiv} for {source}, expected positive")
    if not (np.isfinite(probe) and probe > 0):
        raise ValueError(f"{path}: probe {probe} for {source}, expected positive")
    if not np.isfinite(voff):
        raise ValueError(f"{path}: voff {voff} for {source}")
    if data_width == 1:
        dt = "<u2" if byte_order == 0 else ">u2"
        center, itemsize = 32768, 2
    else:
        dt = "u1"
        center, itemsize = 128, 1
    if npoints <= 0:
        raise ValueError(f"{path}: wave_length {npoints}, expected a positive count")
    # wave_length matched the stored data on every genuine capture checked; less
    # data than it claims means a truncated file (see SPEC.md).
    if file_size is not None and file_size - data_off < npoints * itemsize:
        raise ValueError(
            f"{path}: {file_size - data_off} data bytes for {npoints} "
            f"{itemsize}-byte samples — truncated file?"
        )

    grid = _i32(h, 0x26C)
    if grid <= 0:
        raise ValueError(f"{path}: hori_div_num {grid}, expected positive")
    zoom_sw = _i32(h, 0xAF4)  # zoom_switch: this file is the zoom trace
    if zoom_sw not in (0, 1):
        raise ValueError(f"{path}: zoom_switch {zoom_sw}, expected 0 or 1")
    zoom = zoom_sw == 1
    if zoom:
        time_div = _dwu(h, 0xAF8)  # zoom_td_val
        time_delay = _dwu(h, 0xB20)  # zoom_trig_delay_val
    else:
        time_div = _dwu(h, 0x19C)
        time_delay = _dwu(h, 0x1C4)
    if not (np.isfinite(time_div) and time_div > 0 and np.isfinite(time_delay)):
        raise ValueError(f"{path}: time_div {time_div} / time_delay {time_delay}")

    return {
        "source": source,
        "sample_rate": fs,
        "time_div": time_div,
        "time_delay": time_delay,
        "grid": grid,
        "npoints": npoints,
        "data_width": data_width,
        "vdiv": vdiv,
        "voff": voff,
        "code_per_div": cpd,
        "probe": probe,
        "unit": unit,
        "unit_raw": unit_raw,
        "zoom": zoom,
        "_dt": dt,
        "_center": center,
        "_itemsize": itemsize,
        "_data_off": data_off,
    }


def raw_uint16(path):
    """(samples, sample_rate) for the trace — offset-binary uint16, the
    polarity-correct input for a threshold decoder. Skips the float conversion
    and time axis entirely, so it stays cheap on deep-memory captures."""
    d, stored, _ = _load(path)
    return stored.astype(np.uint16), d["sample_rate"]


def read_group(paths, apply_probe=True, strict=True, ref_position=50.0):
    """Load per-trace files and return ``{source: read()-result}``.

    With ``strict``, everything the shared time axis is built from
    (sample_rate, npoints, time_delay, time_div, grid, and the zoom flag) must
    match. Passing this check makes the traces axis-compatible; the V4 format
    carries no acquisition identifier, so it does not prove common provenance.
    Set ``strict=False`` to bypass the compatibility check.
    """
    out = {}
    for p in paths:
        d = read(p, apply_probe=apply_probe, ref_position=ref_position)
        if d["source"] in out:
            raise ValueError(f"{d['source']} appears twice in group")
        out[d["source"]] = d
    if strict and out:
        ref = next(iter(out.values()))
        for s, d in out.items():
            for k in ("sample_rate", "npoints", "time_delay", "time_div", "grid", "zoom"):
                if d[k] != ref[k]:
                    raise ValueError(
                        f"{s} {k}={d[k]} != {ref[k]}; traces are axis-incompatible "
                        "(pass strict=False to override)"
                    )
    return dict(sorted(out.items()))


def find_group(directory, index, pattern=r"_C(\d)_%d\.bin(?:\.gz)?$"):
    """Return the per-channel file paths for a capture `index` in `directory`,
    sorted by channel. Matches the default SDS814X naming
    ``..._C<ch>_<index>.bin[.gz]``. Override `pattern` for other models (must
    capture the channel number as group 1 and contain %d for the index).
    Duplicate compressed/plain channels are rejected."""
    rx = re.compile(pattern % index)
    hits = {}
    candidates = glob.glob(os.path.join(directory, "*.bin")) + glob.glob(
        os.path.join(directory, "*.bin.gz")
    )
    for p in candidates:
        m = rx.search(os.path.basename(p))
        if m:
            channel = int(m.group(1))
            if channel in hits:
                raise ValueError(
                    f"capture {index} channel C{channel} appears in both "
                    f"{hits[channel]!r} and {p!r}"
                )
            hits[channel] = p
    return [p for _, p in sorted(hits.items())]


def _si(x):
    """Scale to an SI prefix: 2.5e6 -> (2.5, 'M'), 20000 -> (20, 'k')."""
    for div, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return x / div, prefix
    return x, ""


def _main(argv=None):
    import sys

    args = sys.argv[1:] if argv is None else argv
    failed = 0
    default_scan = not args
    patterns = args or ["*.bin", "*.bin.gz"]
    matched = False
    for pat in patterns:
        paths = sorted(glob.glob(pat))
        if not paths:
            if not default_scan:
                print(f"{pat}: no files match", file=sys.stderr)
                failed += 1
            continue
        matched = True
        for p in paths:
            try:
                d = read(p)
            except Exception as e:  # noqa: BLE001 - one failure per input must not abort the rest
                print(e, file=sys.stderr)  # read()'s errors already name the file
                failed += 1
                continue
            v = d["values"]
            u = d["unit"]
            warn = "" if u != "unknown" else f"  [unrecognised unit {d['unit_raw']}]"
            fs, fsp = _si(d["sample_rate"])
            n, np_ = _si(len(v))
            print(
                f"{p}: {d['source']}  {fs:g} {fsp}Sa/s  "
                f"{n:g} {np_}pts  {d['vdiv']:g} {u}/div  probe {d['probe']:g}x  "
                f"{v.min():.2f}..{v.max():.2f} {u}"
                f"{'  zoom' if d['zoom'] else ''}{warn}"
            )
    if default_scan and not matched:
        print("*.bin[.gz]: no files match", file=sys.stderr)
        failed += 1
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
