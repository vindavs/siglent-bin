"""Write sigrok srzip v2 sessions from canonical waveform traces.

Logic channels are thresholded and packed before analog float32 channels in a
shared index space. srzip has no trigger-position or per-channel-unit fields,
so those values are recorded in metadata comments and non-volt channel names.
"""

import math
import os
import sys
import zipfile
from numbers import Integral

import numpy as np

SRZIP_VERSION = 2

DEFAULT_MAX_POINTS = 20_000_000


def _as_list(traces):
    """Normalise one trace, a source mapping, or an iterable to a list."""
    if isinstance(traces, dict):
        if traces and all(isinstance(v, dict) for v in traces.values()):
            return list(traces.values())  # a read_group() {source: trace} mapping
        return [traces]  # a single trace dict
    out = list(traces)
    if not out:
        raise ValueError("no traces to write")
    return out


def _metadata_text(logic_names, analog_names, sample_rate, comments):
    """Build metadata with channel totals before channel names."""
    # libsigrok version used to validate this layout.
    lines = ["[global]", "sigrok version=0.5.2", "", "[device 1]"]
    if logic_names:
        lines.append("capturefile=logic-1")
        lines.append(f"unitsize={_unitsize(len(logic_names))}")
        lines.append(f"total probes={len(logic_names)}")
    if analog_names:
        lines.append(f"total analog={len(analog_names)}")
    rounded_rate = round(sample_rate)
    lines.append(f"samplerate={rounded_rate}")
    if rounded_rate != sample_rate:
        # samplerate is integral, so retain the exact decimated rate in a comment.
        lines.append(f"# siglent_sr: samplerate_exact = {sample_rate!r}")
    for i, name in enumerate(logic_names, start=1):
        lines.append(f"probe{i}={name}")
    for i, name in enumerate(analog_names, start=len(logic_names) + 1):
        lines.append(f"analog{i}={name}")
    for c in comments:
        lines.append(f"# siglent_sr: {c}")
    return "\n".join(lines) + "\n"


def _carries_unit(unit):
    """Return whether a unit must be carried outside srzip's volt assumption."""
    return bool(unit) and unit != "V"


def _analog_name(t):
    """Append a non-volt unit to the srzip analog channel name."""
    unit = t.get("unit")
    return f"{t['source']}[{unit}]" if _carries_unit(unit) else t["source"]


def _unitsize(n_logic):
    """Bytes per logic sample for n_logic channels."""
    return max(1, math.ceil(n_logic / 8))


def _bytes_per_sample(n_logic, n_analog):
    """Bytes per sample column: float32 analog plus packed logic bits."""
    logic_bytes = math.ceil(n_logic / 8) if n_logic else 0
    return 4 * n_analog + logic_bytes


def auto_threshold(values):
    """Use the midpoint of the 1st and 99th percentiles as a logic threshold."""
    lo, hi = np.percentile(np.asarray(values, dtype=np.float64), [1, 99])
    return float((lo + hi) / 2.0)


def _finite_float(path, label, value):
    try:
        value = float(value)
    except (OverflowError, TypeError, ValueError) as e:
        raise ValueError(f"{path}: {label} must be a finite number, got {value!r}") from e
    if not math.isfinite(value):
        raise ValueError(f"{path}: {label} must be a finite number, got {value!r}")
    return value


def _resolve_threshold(path, trace, threshold):
    """Return (threshold, was_auto) for one trace."""
    if threshold is None or (isinstance(threshold, dict) and trace["source"] not in threshold):
        value, was_auto = auto_threshold(trace["values"]), True
    else:
        value = threshold[trace["source"]] if isinstance(threshold, dict) else threshold
        was_auto = False
    return _finite_float(path, f"threshold for {trace['source']}", value), was_auto


def _to_bits(values, threshold, hysteresis):
    """Apply a comparator or Schmitt trigger and return boolean samples."""
    v = np.asarray(values, dtype=np.float64)
    if hysteresis <= 0:
        return v > threshold
    hi = threshold + hysteresis / 2.0
    lo = threshold - hysteresis / 2.0
    decides = np.flatnonzero((v > hi) | (v < lo))
    if decides.size == 0:
        return np.zeros(v.size, dtype=bool)
    decision = v[decides] > hi
    prev = np.searchsorted(decides, np.arange(v.size), side="right") - 1
    return np.where(prev >= 0, decision[np.clip(prev, 0, None)], False)


def _pack_logic(bit_arrays, unitsize):
    """Pack boolean channel arrays into srzip logic bytes: bit i of each
    sample is channel index i, `unitsize` bytes per sample."""
    n = len(bit_arrays[0])
    packed = np.zeros((n, unitsize), dtype=np.uint8)
    for i, bits in enumerate(bit_arrays):
        packed[:, i // 8] |= np.asarray(bits, dtype=np.uint8) << (i % 8)
    return packed.tobytes()


def trigger_sample(trace, decimate=1):
    """Return the trigger index in the written, possibly decimated coordinates."""
    return round(-float(trace["t0"]) * float(trace["sample_rate"]) / decimate)


def _source_of(items, i):
    """Return a source name or positional label for errors."""
    return items[i].get("source") or f"trace {i}"


def _check_uniform(path, items):
    """Validate required fields and one shared axis; return ``(rate, length)``."""
    lengths = []
    rates = []
    time_origins = []
    for i, t in enumerate(items):
        if not isinstance(t, dict):
            raise ValueError(
                f"{path}: trace {i} is not a trace dict (got "
                f"{type(t).__name__!r}) -- pass the read_group() mapping "
                f"itself, or its .values(), not its keys"
            )
        values = t.get("values")
        if values is None or len(values) == 0:
            raise ValueError(f"{path}: trace {_source_of(items, i)} has no values")
        lengths.append(len(values))
        try:
            rate = float(t.get("sample_rate"))
        except (OverflowError, TypeError, ValueError):
            rate = float("nan")
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(
                f"{path}: trace {_source_of(items, i)} has no valid "
                f"sample_rate (must be a positive, finite number)"
            )
        rates.append(rate)
        if t.get("source") is None:
            raise ValueError(f"{path}: {_source_of(items, i)} has no source")
        try:
            time_origin = float(t.get("t0"))
        except (OverflowError, TypeError, ValueError):
            time_origin = float("nan")
        if not math.isfinite(time_origin):
            raise ValueError(
                f"{path}: trace {_source_of(items, i)} has no valid t0 (must be a finite number)"
            )
        time_origins.append(time_origin)

    rate, n = rates[0], lengths[0]
    for i in range(1, len(items)):
        if rates[i] != rate:
            raise ValueError(
                f"{path}: sample_rate mismatch, {_source_of(items, 0)} is "
                f"{rate:g} Sa/s but {_source_of(items, i)} is "
                f"{rates[i]:g} Sa/s"
            )
        if lengths[i] != n:
            raise ValueError(
                f"{path}: length mismatch, {_source_of(items, 0)} has {n} "
                f"samples but {_source_of(items, i)} has {lengths[i]}"
            )
        if time_origins[i] != time_origins[0]:
            raise ValueError(
                f"{path}: t0 mismatch, {_source_of(items, 0)} is "
                f"{time_origins[0]:g} s but {_source_of(items, i)} is "
                f"{time_origins[i]:g} s"
            )
    return rate, n


def min_run_length(bits):
    """Return the shortest complete run, excluding capture-edge runs.

    If there is no interior run, return the full array length.
    """
    b = np.asarray(bits, dtype=bool)
    if b.size == 0:
        return 0
    edges = np.flatnonzero(np.diff(b)) + 1
    bounds = np.concatenate(([0], edges, [b.size]))
    runs = np.diff(bounds)
    if runs.size < 3:
        return int(b.size)
    return int(runs[1:-1].min())


def _format_size(n_bytes):
    """Format a byte count using B, KB, MB or GB."""
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n_bytes >= scale:
            return f"{n_bytes / scale:.2f} {unit}"
    return f"{n_bytes} B"


def _enforce_size_guard(path, n, n_logic, n_analog, decimate, max_points):
    """Check the decimated total before opening the file; return points per channel."""
    n_chan = n_logic + n_analog
    written_per_channel = -(-n // decimate)  # ceil(n / decimate)
    total = written_per_channel * n_chan
    if total <= max_points:
        return written_per_channel
    cap = max_points // n_chan  # max samples per channel
    if cap < 1:
        raise ValueError(
            f"{path}: max_points={max_points} is smaller than the "
            f"{n_chan}-channel count; raise max_points to at least "
            f"{n_chan}."
        )
    factor = max(1, -(-n // cap))
    # Logic channels share packed bytes; analog channels use float32.
    size_bytes = n * _bytes_per_sample(n_logic, n_analog)
    raise ValueError(
        f"{path}: {n_chan} ch x {n} pts = {n * n_chan} samples, "
        f"{_format_size(size_bytes)} uncompressed, over the "
        f"{max_points}-sample limit. Pass --decimate {factor} to fit, "
        f"or raise max_points."
    )


def _build_logic_channels(path, items, threshold, hysteresis, decimate):
    """Build thresholded channels and their metadata comments."""
    bit_arrays = []
    comments = []
    for t in items:
        th, was_auto = _resolve_threshold(path, t, threshold)
        bits = _to_bits(t["values"], th, hysteresis)
        if decimate > 1:
            narrow = min_run_length(bits)
            if narrow < 2 * decimate:
                print(
                    f"{path}: {t['source']} narrowest pulse is "
                    f"{narrow} samples, below 2x the decimate "
                    f"factor {decimate} -- edges have been lost",
                    file=sys.stderr,
                )
            bits = bits[::decimate]
        bit_arrays.append(bits)
        how = "auto, p1..p99 midpoint" if was_auto else "given"
        edges = int(np.count_nonzero(np.diff(bits)))
        unit = t.get("unit")
        # Use the trace's unit without guessing one for unitless input.
        shown = f"{th:.4f} {unit}" if unit else f"{th:.4f}"
        comments.append(f"threshold_{t['source']} = {shown} ({how}), {edges} edges")
    return bit_arrays, comments


class _WriteResult(str):
    """Output path with written geometry and CLI status."""

    def __new__(cls, path, *, decimate, n_written, sample_rate, threshold_summary, trigger_index):
        self = super().__new__(cls, path)
        self.decimate = decimate
        self.n_written = n_written
        self.sample_rate = sample_rate
        self.threshold_summary = threshold_summary
        self.trigger_index = trigger_index
        return self


def write(
    path,
    traces,
    *,
    logic=True,
    analog=True,
    threshold=None,
    hysteresis=0.0,
    decimate=1,
    max_points=DEFAULT_MAX_POINTS,
    source_file=None,
):
    """Write traces to srzip and return a path-like result with output geometry.

    ``source_file`` is stored as a metadata comment when provided.
    """
    try:
        items = _as_list(traces)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from e
    if not logic and not analog:
        raise ValueError(f"{path}: nothing to write with logic=False and analog=False")
    if isinstance(decimate, (bool, np.bool_)) or not isinstance(decimate, Integral) or decimate < 1:
        raise ValueError(f"{path}: decimate must be a positive integer, got {decimate!r}")
    decimate = int(decimate)
    if logic:
        hysteresis = _finite_float(path, "hysteresis", hysteresis)
        if hysteresis < 0:
            raise ValueError(f"{path}: hysteresis must be >= 0, got {hysteresis!r}")
    sample_rate, n = _check_uniform(path, items)
    logic_names = [f"{t['source']}_d" for t in items] if logic else []
    analog_names = [_analog_name(t) for t in items] if analog else []

    if analog:
        # Warn only for analog data, where srzip assumes volts.
        for t in items:
            if _carries_unit(t.get("unit")):
                print(
                    f"{path}: {t['source']} is in {t['unit']}, which srzip cannot "
                    f"store -- sigrok reads every analog channel as volts, so the "
                    f"unit survives only in the channel name and a comment",
                    file=sys.stderr,
                )

    n_written = _enforce_size_guard(
        path, n, len(logic_names), len(analog_names), decimate, max_points
    )
    sample_rate = sample_rate / decimate

    # Resolve thresholds once so metadata and the logic chunk agree.
    trigger_index = trigger_sample(items[0], decimate)
    comments = [
        f"sources = {', '.join(t['source'] for t in items)}",
        f"trigger_sample = {trigger_index}",
        f"trigger_time_s = {float(items[0]['t0']):.6e}",
        *(f"unit_{t['source']} = {t['unit']}" for t in items if t.get("unit")),
    ]
    if source_file is not None:
        comments.append(f"source_file = {source_file}")
    bit_arrays = []
    threshold_summary = "logic skipped (--no-logic)"
    if logic_names:
        bit_arrays, logic_comments = _build_logic_channels(
            path, items, threshold, hysteresis, decimate
        )
        comments.extend(logic_comments)
        threshold_summary = logic_comments[0]

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("version", f"{SRZIP_VERSION}\n")
        zf.writestr("metadata", _metadata_text(logic_names, analog_names, sample_rate, comments))
        if logic_names:
            zf.writestr("logic-1-1", _pack_logic(bit_arrays, _unitsize(len(logic_names))))
        if analog_names:
            for i, t in enumerate(items, start=len(logic_names) + 1):
                # srzip analog payload is always float32.
                vals = np.asarray(t["values"], dtype=np.float32)[::decimate]
                assert len(vals) == n_written, (
                    "decimated length disagrees with the size guard "
                    "that already approved this write"
                )
                zf.writestr(f"analog-1-{i}-1", vals.tobytes())

    return _WriteResult(
        path,
        decimate=decimate,
        n_written=n_written,
        sample_rate=sample_rate,
        threshold_summary=threshold_summary,
        trigger_index=trigger_index,
    )


def write_frames(path_stem, frames, **kw):
    """Write one lexically ordered ``.sr`` file per sequence frame.

    Frames are separate because inter-segment dead time is unknown.
    """
    stem = path_stem[:-3] if path_stem.endswith(".sr") else path_stem
    try:
        items = _as_list(frames)
    except ValueError as e:
        raise ValueError(f"{stem}: {e}") from e
    width = max(2, len(str(len(items))))
    out = []
    for i, frame in enumerate(items, start=1):
        out.append(write(f"{stem}-{i:0{width}d}.sr", frame, **kw))
    return out


def _main(argv=None):
    import argparse
    import glob

    import siglent_bin

    ap = argparse.ArgumentParser(
        prog="siglent-sr", description="Convert Siglent V4.0 .bin captures to sigrok .sr files."
    )
    ap.add_argument(
        "inputs",
        nargs="*",
        default=None,
        help="capture files (default: *.bin and *.bin.gz)",
    )
    ap.add_argument("--no-logic", action="store_true", help="skip the thresholded logic channels")
    ap.add_argument("--no-analog", action="store_true", help="skip the float32 analog channels")
    ap.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="logic threshold in each trace's unit (default: auto per channel)",
    )
    ap.add_argument(
        "--hysteresis",
        type=float,
        default=0.0,
        help="Schmitt trigger band in each trace's unit (default: 0)",
    )
    ap.add_argument(
        "--decimate", type=int, default=1, help="keep every Nth sample (default: 1, no decimation)"
    )
    ap.add_argument(
        "--max-points",
        type=int,
        default=DEFAULT_MAX_POINTS,
        help=f"refuse above this total sample count (default: {DEFAULT_MAX_POINTS})",
    )
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    failed = 0
    default_scan = not args.inputs
    patterns = args.inputs or ["*.bin", "*.bin.gz"]
    matched = False
    for pat in patterns:
        paths = sorted(glob.glob(pat)) or ([pat] if os.path.exists(pat) else [])
        if not paths:
            if not default_scan:
                print(f"{pat}: no files match", file=sys.stderr)
                failed += 1
            continue
        matched = True
        for p in paths:
            out = (p[:-7] if p.endswith(".bin.gz") else os.path.splitext(p)[0]) + ".sr"
            try:
                d = siglent_bin.read(p)
                result = write(
                    out,
                    d,
                    logic=not args.no_logic,
                    analog=not args.no_analog,
                    threshold=args.threshold,
                    hysteresis=args.hysteresis,
                    decimate=args.decimate,
                    max_points=args.max_points,
                    source_file=p,
                )
            except Exception as e:  # noqa: BLE001 - one failure per input must not abort the rest
                print(f"{p}: {e}", file=sys.stderr)
                failed += 1
                continue
            print(
                f"{out}: {d['source']}  {result.n_written} pts  "
                f"{result.sample_rate:g} Sa/s  "
                f"{result.threshold_summary}  trigger_sample = {result.trigger_index}"
            )
    if default_scan and not matched:
        print("*.bin[.gz]: no files match", file=sys.stderr)
        failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
