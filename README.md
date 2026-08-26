# siglent-bin

A dependency-light (numpy-only) Python toolkit for **Siglent "Binary Format
V4.0"** waveform files and **SDS800X HD** live capture. It reads scope `.bin`
files, writes canonical archives, acquires segmented SCPI/TCP waveforms, and
exports traces to sigrok srzip.

The reader handles two format traps: 16-bit samples are **offset-binary uint16**
(a signed-int16 read inverts some traces), and Siglent's conversion document has
the offset-term sign wrong. [SPEC.md](SPEC.md) records field offsets, the
conversion formula, and every claim's documented or observed basis.

## Install

The package requires Python 3.11+ and numpy. Install it directly from GitHub:

```
pip install git+https://github.com/vindavs/siglent-bin
```

## Usage

```python
import siglent_bin

# One file = one trace
d = siglent_bin.read("SDS814X_HD_Binary_C1_30.bin")
d["values"]  # numpy float32 array — volts (amps if the channel was in amps display)
d["unit"]  # 'V' or 'A'
d["t0"]  # seconds, time of sample 0 relative to the trigger
siglent_bin.time_axis(d)  # seconds, float64, the full per-sample time axis
d["sample_rate"]  # Sa/s
d["source"]  # 'C1'..'C8', or 'F1'..'F4' for a math trace

# Just the raw samples for a threshold/PWM decoder (offset-binary uint16, correct polarity)
samples, fs = siglent_bin.raw_uint16("cap.bin")

# A multi-channel acquisition is saved as several files sharing an index.
paths = siglent_bin.find_group("/Volumes/SCOPE", 30)  # ..._C1_30.bin, ..._C2_30.bin, ...
chans = siglent_bin.read_group(paths)  # {'C1': {...}, 'C2': {...}, ...}, aligned
```

### Live capture over LAN

`siglent_lan` reads waveforms over a raw socket (port 5025) and returns the same
dict shape as `read()`. For sequence (segmented) acquisition, `fetch()` returns
every segment of a run in one call.

```python
import siglent_lan

frames = siglent_lan.fetch("192.168.1.50", "C1")  # list, one dict per frame
frames[0]["values"], frames[0]["t0"]
frames[0]["descriptor_stamp"]  # clock-like diagnostic; not acquisition time

chans = siglent_lan.fetch_group("192.168.1.50")  # {'C1': [...], 'C2': [...]}
```

```
$ siglent-lan 192.168.1.50 C1 C2
```

On the tested SDS814X firmware, a completed sequence at `Ready` returned a
zeroed, unaddressable descriptor. `:TRIGger:STOP` made the buffer readable, so
`fetch()` stops first by default. `stop=False` preserves a running acquisition,
but a completed sequence may then be unaddressable.

Siglent documents this family as having no RTC. `siglent_lan.sync_clock(host)`
sets the session clock from the host, and `siglent-lan HOST --sync-clock` does
the same from the CLI. It is opt-in: `fetch()` never changes the clock. Clock
state is capture provenance and does not affect trigger-relative waveform time.

The channel unit (`'V'` or `'A'`) comes from `:CHANnel<n>:UNIT?`; unlike the
saved-file header, the tested firmware's 346-byte live descriptor has no stable
unit field. Math and zoom traces have no such query, so their `unit` is None
rather than an assumed `'V'`.

The descriptor's clock-like record is exposed as `descriptor_stamp` for
diagnostics. On the tested firmware it is not acquisition time and its writer is
unknown. A live fetch and saved file from the same stopped acquisition had
identical raw samples; converted values and axes agreed within float precision.
Live samples are signed, not offset-binary, and the descriptor timebase index is
model-dependent, so the live path normalises codes and queries timebase over
SCPI. See [SPEC.md](SPEC.md).

Or as a CLI summary:

```
$ siglent-bin *.bin        # pip-installed; `python -m siglent_bin` also works
cap_C1_30.bin: C1  20 kSa/s  100 kpts  0.5 V/div  probe 10x  -0.12..3.31 V
```

### Writing a .bin

`siglent_bin.write()` creates a canonical V4-compatible archive from a 16-bit
analog `read()` result or live `fetch()` frame.

```python
frames = siglent_lan.fetch("192.168.1.50", "C1")
siglent_bin.write("cap_C1.bin", frames[0])
back = siglent_bin.read("cap_C1.bin")
```

Samples come from `raw`, so float32 `values` rounding does not reach the file.
The writer validates every stored field and refuses unsupported 8-bit, math,
zoom, fractional-scale, or unknown-unit input instead of guessing.

The format cannot store the horizontal reference position. The writer
canonicalises the stored delay to a 50% reference, preserving the input trace's
`t0` on a default read but discarding the original front-panel delay/reference
pair. The calling experiment must also record live-sequence bookkeeping and
other capture provenance.

The output preserves the trace fields understood by this package, not every
byte of the scope's original header. The tested SDS814X HD has no Binary recall
option; its Reference recall rejected an untouched scope-written `.bin` as an
illegal format. There is therefore no valid front-panel import control for a
generated `.bin` on this model/firmware. Third-party reader compatibility has
not yet been verified.

Gzip is transparent in both directions: give `write()` a `.gz` path and it
compresses; `read()` and `raw_uint16()` sniff the magic bytes, so a `.bin.gz`
(or one renamed without the suffix) opens like any other capture.

### Export to sigrok (.sr) for protocol decoding

`siglent_sr` writes sigrok session files for libsigrokdecode protocol decoders.
Analog channels are float32 for PulseView; logic channels are thresholded because
`sigrok-cli` feeds decoders only logic channels.

```python
import siglent_bin, siglent_lan, siglent_sr

d = siglent_bin.read("SDS814X_HD_Binary_C1_30.bin")
siglent_sr.write("cap.sr", d)  # analog + thresholded logic
siglent_sr.write("cap.sr", d, threshold=1.65)  # explicit threshold, in the trace's unit
siglent_sr.write("cap.sr", d, decimate=10)  # explicit sample reduction

frames = siglent_lan.fetch("192.168.1.50", "C1")  # one dict per sequence frame
siglent_sr.write_frames("seq", frames)  # one .sr file per frame
```

```
siglent-sr *.bin
sigrok-cli -i SDS814X_HD_Binary_C1_30.sr -P uart:baudrate=9600 -A uart
```

The per-channel threshold defaults to the midpoint of the trace's 1st and 99th
percentiles. `write()` records it and the edge count in `metadata`; `siglent-sr`
also prints the threshold so a poor auto-pick is visible before decoding.

srzip v2 has no trigger-position field (nor does libsigrok's writer expose an
`SR_DF_TRIGGER` case), so the trigger is recorded in `metadata`. Oversized
captures are refused with the `--decimate` factor that would fit. srzip also has
no **per-channel unit**.

libsigrok reports every analog channel as volts, regardless of file contents.
Non-volt traces therefore append their unit to the channel name (`C1[A]`), the
only per-channel field viewers see; `write()` warns on stderr. Volts remain
unsuffixed. As on the `.bin` path, `[A]` means *the scope's amps reading*: amps
mode stores the V/A factor but not physical probe attenuation, so values are
calibrated only as far as the scope display (see SPEC.md).

Decimation is explicit. If a capture exceeds the srzip point/size guard, rerun
with the reported `--decimate N` factor after checking that narrow pulses survive.

## Scope & limitations

- Reads analog channels (`C1`–`C4`, plus format-documented `C5`–`C8`), math traces
  (`F1`–`F4`), and zoom (`Z1`–`Z4`) saves. Zoom saves slice the parent record and use
  the stored zoom timebase. Digital (D0–D15) channels and reference waveforms are not
  parsed.
- Absolute time needs the scope's horizontal reference position, which the header
  does not record: pass `ref_position=` (default 50 = screen centre) to `read()`
  if the scope was set elsewhere. Relative timing is unaffected either way, and
  `siglent_lan` queries the value. Files produced by `write()` are canonicalised
  to 50% so their default-read `t0` is self-contained. See SPEC.md.
- Amps display mode returns amps (`unit` is `'A'`), but only as calibrated by the
  scope: physical probe attenuation is absent from the file (see SPEC.md).
- Most acquisition/display state is absent: interpolation and peak detect leave no
  header trace (peak-detect samples interleave a min/max envelope); sequence exports
  ordinary files without timestamps; and the header stores save-time, not
  acquisition-time, horizontal delay (see SPEC.md).
- The SDS814X HD writes 16-bit little-endian. The format-defined 8-bit
  (`data_width=0`) and big-endian paths are implemented but not produced by this scope.
- For digital/threshold decoding, use `raw`/`raw_uint16` and skip the volts
  conversion entirely.

## Verification

The conversion math is calibrated against unmodified SDS814X captures of known
levels (0 / 3 / 4.5 / 5 V), probe 1×/10×, varied vertical/horizontal settings
(including vernier V/div and a window excluding 0 V), channel invert, amps mode
across V/A factors, and math/zoom saves against their parent records. They are the
repository fixtures: `python3 tests/test_siglent_bin.py` (or `pytest`). C5–C8,
8-bit, and big-endian paths have synthetic tests only; captures from other SDS
models are welcome.

For a general multi-format parser, see [RigolWFM](https://github.com/scottprahl/RigolWFM).
As of 1.5.0, its Siglent V4.0 conversion follows the vendor document (`+ vert_offset`,
no probe factor) and disagrees with these captures on both counts.

## License

MIT — see [LICENSE](LICENSE).

## Credit

Format distilled from Siglent's [*How to Extract Data from the Binary File of SIGLENT
Oscilloscope*](https://www.siglenteu.com/wp-content/uploads/dlm_uploads/2025/10/How-to-Extract-Data-from-the-Binary-FileEN03B.pdf)
(V4.0), corrected and extended by reverse-engineering real captures.
