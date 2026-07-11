#!/usr/bin/env python3
"""Reader for Siglent "Binary Format V4.0" oscilloscope waveform files (.bin).

Verified against the **SDS800X HD** family (SDS814X). Parses the 4 KB header and
returns a channel's samples in real units — volts, or amps for a channel in amps
display mode — plus a time axis. Pure numpy.

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
import struct
import glob
import os
import re
import numpy as np

HEADER_BYTES = 0x1000


def _f64(h, a): return struct.unpack('<d', h[a:a + 8])[0]
def _i32(h, a): return struct.unpack('<i', h[a:a + 4])[0]


# Named unit types (first int32 of the unit descriptor, per Siglent's doc; type 0
# means the unit is composed of powers of V, A and s instead).
_UNIT_TYPES = {1: 'dBV', 2: 'dBA', 3: 'dB', 4: 'Vpp', 5: 'Vdc', 6: 'dBm', 7: 'Sa',
               8: 'div', 9: 'pts', 10: '', 11: 'deg', 12: '%'}


def _dwu(h, a):
    """Decode a 'Data-With-Unit': float64 value at a, SI-prefix index int32 at a+8.
    Index 8 = base (x1), 7 = milli, 6 = micro, 9 = kilo, 10 = mega, ..."""
    m = _i32(h, a + 8)
    if not 0 <= m <= 16:             # the doc's magnitude table: yocto..yotta
        raise ValueError(f'magnitude index {m} at {a + 8:#x} outside 0..16')
    return _f64(h, a) * 10.0 ** (3 * (m - 8))


def _unit(h, a):
    """(label, raw_descriptor) for a Data-With-Unit at a. Per the format doc the
    descriptor is 7 int32s: [type, V_num, V_den, A_num, A_den, s_num, s_den] —
    type 0 composes the unit from rational powers of V, A and s, so plain volts
    is (0, 1,1, 0,1, 0,1) (spaces grouping the num,den pairs); other types are
    named units (dBV, Vpp, ...)."""
    d = tuple(_i32(h, a + 0x0c + 4 * i) for i in range(7))
    if d[0] != 0:
        return _UNIT_TYPES.get(d[0], 'unknown'), d
    parts = []
    for sym, num, den in (('V', d[1], d[2]), ('A', d[3], d[4]), ('s', d[5], d[6])):
        if num == 0:
            continue
        p = num / (den if den else 1)
        parts.append(sym if p == 1 else f'{sym}^{p:g}')
    return '*'.join(parts), d


def read(path, apply_probe=True):
    """Parse one Siglent V4.0 .bin (a single enabled trace). Returns a dict:

        source        -> which trace this file holds: 'C1'..'C8' or 'F1'..'F4',
        sample_rate, time_div, time_delay, grid, npoints, data_width,
        vdiv, voff, code_per_div, probe,
        unit          -> unit of the values, decoded from the descriptor
                         ('V', 'A', ...),
        unit_raw      -> the raw 7-int unit descriptor,
        zoom          -> True for a zoom (Z) save; the time axis then comes from
                         the stored zoom timebase (held in time_div/time_delay),
        raw           -> samples as offset-binary uint16 (polarity-correct),
        values        -> samples converted to `unit` (volts for 'V', amps for 'A'),
        time          -> seconds, same length as values.

    `values` is `((code-center)*vdiv/cpd - voff) * probe` — the scope's on-screen
    reading. Calibrated against known 0/3/4.5/5 V references across vertical
    settings; the offset term is `- voff` (see SPEC.md).

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
    d, stored, center = _load(path)
    samples = stored.astype(np.float64)

    # value (in `unit`) = ((code-center)*vdiv/cpd - voff) * probe. vdiv is stored PRE-probe.
    # `probe` is the voltage attenuation in V mode (verified 1x/10x), a 1/(V/A) factor in
    # amps mode.
    values = (samples - center) * d['vdiv'] / d['code_per_div'] - d['voff']
    if apply_probe:
        values = values * d['probe']

    half_span = d['time_div'] * d['grid'] / 2
    i_t = np.arange(len(samples)) / d['sample_rate']
    if d['zoom']:
        # A zoom save carries its own timebase; the window centre sits at
        # +time_delay — sign verified against slices at known positions (SPEC.md).
        t = d['time_delay'] - half_span + i_t
    else:
        t = -half_span - d['time_delay'] + i_t

    d.update(raw=stored.astype(np.uint16), values=values, time=t)
    return d


def _load(path):
    """Validated header fields + the stored samples. Reads only the 4 KB header
    and the npoints-long payload, so oversized or junk-trailed files cost
    nothing beyond their samples."""
    with open(path, 'rb') as f:
        h = f.read(HEADER_BYTES)
        size = os.fstat(f.fileno()).st_size
        d = _parse_header(h, size, path)
        f.seek(d.pop('_data_off'))
        payload = f.read(d['npoints'] * d.pop('_itemsize'))
        samples = np.frombuffer(payload, dtype=d.pop('_dt'), count=d['npoints'])
    return d, samples, d.pop('_center')


def _parse_header(h, file_size, path):
    """Validate a .bin's header bytes `h` against the file's total size and
    decode it: the read() result minus the sample/values/time arrays, plus the
    private '_dt' / '_center' / '_itemsize' / '_data_off' needed to load the
    samples."""
    if len(h) < HEADER_BYTES:
        raise ValueError(
            f'{path}: {len(h)} bytes, shorter than the {HEADER_BYTES}-byte header')
    version = _i32(h, 0x00)
    if version != 4:
        raise ValueError(f'{path}: header version {version}, expected 4 (V4.0)')
    data_off = _i32(h, 0x04)
    if not HEADER_BYTES <= data_off <= file_size:
        raise ValueError(
            f'{path}: data offset {data_off:#x} not between header end and EOF')
    data_width = h[0x264]            # 0 = 8-bit, 1 = 16-bit
    byte_order = h[0x265]            # 0 = little-endian, 1 = big-endian
    if data_width not in (0, 1) or byte_order not in (0, 1):
        raise ValueError(
            f'{path}: data_width {data_width} / byte_order {byte_order}, '
            'each must be 0 or 1')
    ch_on = [_i32(h, 0x08 + 4 * c) for c in range(4)] + \
            [_i32(h, 0x404 + 4 * c) for c in range(4)]   # CH5-8 (8ch models)
    math_on = [_i32(h, 0x280 + 4 * m) for m in range(4)]
    bad = [v for v in ch_on + math_on if v not in (0, 1)]
    if bad:
        raise ValueError(
            f'{path}: channel/math enable flags must be 0 or 1, got {bad}')
    on = [f'C{c + 1}' for c in range(8) if ch_on[c]] + \
         [f'F{m + 1}' for m in range(4) if math_on[m]]
    if len(on) != 1:
        raise NotImplementedError(
            f'{path}: {len(on)} traces enabled {on}. The SDS800X HD writes one '
            'trace per .bin — a multi-channel acquisition is several files; use '
            'read_group(). (The format also defines sequential multi-trace '
            'files, unobserved on this scope and not implemented.)')
    source = on[0]

    if source.startswith('C'):
        c = int(source[1:]) - 1
        # CH1-4 and CH5-8 live in two header banks of identical shape.
        vd_a, vo_a, pr_a, cpd_a, b = ((0x18, 0xb8, 0x244, 0x270, c) if c < 4 else
                                      (0x414, 0x4b4, 0x554, 0x574, c - 4))
        vdiv = _dwu(h, vd_a + 0x28 * b)
        voff = _dwu(h, vo_a + 0x28 * b)
        cpd = _i32(h, cpd_a + 4 * b)         # code_per_div, SIGNED
        probe = _f64(h, pr_a + 8 * b)
        unit, unit_raw = _unit(h, vd_a + 0x28 * b)
        npoints = _i32(h, 0x1ec)
        fs = _dwu(h, 0x1f0)
    else:                                    # math trace F1-F4
        m = int(source[1:]) - 1
        vdiv = _dwu(h, 0x290 + 0x28 * m)     # math_vdiv_val
        voff = _dwu(h, 0x330 + 0x28 * m)     # math_vpos_val
        cpd = _i32(h, 0x400)                 # math_vert_code_per_div (shared)
        probe = 1.0                          # math has no probe field
        unit, unit_raw = _unit(h, 0x290 + 0x28 * m)
        npoints = _i32(h, 0x3d0 + 4 * m)     # math_store_len
        interval = _f64(h, 0x3e0 + 8 * m)    # math_f_time, s between samples
        fs = 1.0 / interval if interval > 0 else 0.0

    if cpd == 0:
        raise ValueError(f'{path}: code_per_div is 0 for {source}')
    if not (np.isfinite(fs) and fs > 0):
        raise ValueError(f'{path}: sample rate {fs}, expected finite and > 0')
    if not (np.isfinite(vdiv) and vdiv > 0):
        raise ValueError(f'{path}: vdiv {vdiv} for {source}, expected positive')
    if not (np.isfinite(probe) and probe > 0):
        raise ValueError(f'{path}: probe {probe} for {source}, expected positive')
    if not np.isfinite(voff):
        raise ValueError(f'{path}: voff {voff} for {source}')
    if data_width == 1:
        dt = '<u2' if byte_order == 0 else '>u2'
        center, itemsize = 32768, 2
    else:
        dt = 'u1'
        center, itemsize = 128, 1
    if npoints <= 0:
        raise ValueError(f'{path}: wave_length {npoints}, expected a positive count')
    # wave_length matched the stored data on every genuine capture checked; less
    # data than it claims means a truncated file (see SPEC.md).
    if file_size - data_off < npoints * itemsize:
        raise ValueError(
            f'{path}: {file_size - data_off} data bytes for {npoints} '
            f'{itemsize}-byte samples — truncated file?')

    grid = _i32(h, 0x26c)
    if grid <= 0:
        raise ValueError(f'{path}: hori_div_num {grid}, expected positive')
    zoom_sw = _i32(h, 0xaf4)         # zoom_switch: this file is the zoom trace
    if zoom_sw not in (0, 1):
        raise ValueError(f'{path}: zoom_switch {zoom_sw}, expected 0 or 1')
    zoom = zoom_sw == 1
    if zoom:
        time_div = _dwu(h, 0xaf8)    # zoom_td_val
        time_delay = _dwu(h, 0xb20)  # zoom_trig_delay_val
    else:
        time_div = _dwu(h, 0x19c)
        time_delay = _dwu(h, 0x1c4)
    if not (np.isfinite(time_div) and time_div > 0 and np.isfinite(time_delay)):
        raise ValueError(
            f'{path}: time_div {time_div} / time_delay {time_delay}')

    return {
        'source': source, 'sample_rate': fs, 'time_div': time_div,
        'time_delay': time_delay, 'grid': grid, 'npoints': npoints,
        'data_width': data_width, 'vdiv': vdiv, 'voff': voff,
        'code_per_div': cpd, 'probe': probe, 'unit': unit, 'unit_raw': unit_raw,
        'zoom': zoom, '_dt': dt, '_center': center, '_itemsize': itemsize,
        '_data_off': data_off,
    }


def raw_uint16(path):
    """(samples, sample_rate) for the trace — offset-binary uint16, the
    polarity-correct input for a threshold decoder. Skips the float conversion
    and time axis entirely, so it stays cheap on deep-memory captures."""
    d, stored, _ = _load(path)
    return stored.astype(np.uint16), d['sample_rate']


def read_group(paths, apply_probe=True, strict=True):
    """Load several per-trace files from one acquisition and return
    {source: read()-result}, e.g. {'C1': ..., 'C2': ...}. All traces share the
    time base, so with `strict` everything the time axis is built from
    (sample_rate, npoints, time_delay, time_div, grid, and the zoom flag —
    zoom traces use a different axis formula) must match across files (it
    will, for files from one acquisition); set strict=False to bypass.
    """
    out = {}
    for p in paths:
        d = read(p, apply_probe=apply_probe)
        if d['source'] in out:
            raise ValueError(f'{d["source"]} appears twice in group')
        out[d['source']] = d
    if strict and out:
        ref = next(iter(out.values()))
        for s, d in out.items():
            for k in ('sample_rate', 'npoints', 'time_delay', 'time_div', 'grid',
                      'zoom'):
                if d[k] != ref[k]:
                    raise ValueError(
                        f'{s} {k}={d[k]} != {ref[k]}; not one acquisition '
                        '(pass strict=False to override)')
    return dict(sorted(out.items()))


def find_group(directory, index, pattern=r'_C(\d)_%d\.bin$'):
    """Return the per-channel file paths for a capture `index` in `directory`,
    sorted by channel. Matches the default SDS814X naming
    ``..._C<ch>_<index>.bin``. Override `pattern` for other models (must capture
    the channel number as group 1 and contain %d for the index)."""
    rx = re.compile(pattern % index)
    hits = []
    for p in glob.glob(os.path.join(directory, '*.bin')):
        m = rx.search(os.path.basename(p))
        if m:
            hits.append((int(m.group(1)), p))
    return [p for _, p in sorted(hits)]


def _si(x):
    """Scale to an SI prefix: 2.5e6 -> (2.5, 'M'), 20000 -> (20, 'k')."""
    for div, prefix in ((1e9, 'G'), (1e6, 'M'), (1e3, 'k')):
        if abs(x) >= div:
            return x / div, prefix
    return x, ''


def _main(argv=None):
    import sys
    args = sys.argv[1:] if argv is None else argv
    failed = 0
    for pat in (args or ['*.bin']):
        paths = sorted(glob.glob(pat))
        if not paths:
            print(f'{pat}: no files match', file=sys.stderr)
            failed += 1
        for p in paths:
            try:
                d = read(p)
            except Exception as e:
                print(e, file=sys.stderr)   # read()'s errors already name the file
                failed += 1
                continue
            v = d['values']
            u = d['unit']
            warn = '' if u != 'unknown' else f'  [unrecognised unit {d["unit_raw"]}]'
            fs, fsp = _si(d['sample_rate'])
            n, np_ = _si(len(v))
            print(f'{p}: {d["source"]}  {fs:g} {fsp}Sa/s  '
                  f'{n:g} {np_}pts  {d["vdiv"]:g} {u}/div  probe {d["probe"]:g}x  '
                  f'{v.min():.2f}..{v.max():.2f} {u}'
                  f'{"  zoom" if d["zoom"] else ""}{warn}')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    _main()
