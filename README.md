# siglent-bin

A lightweight toolkit for working with Siglent SDS800X HD scopes:

- Read Siglent Binary Format V4.0 waveform files
- Capture SDS800X HD waveforms over SCPI/TCP
- Write canonical archives
- Export traces to sigrok

The package has one runtime dependency: NumPy.

The saved-file reader is built around three details that are easy to get wrong:

- SDS800X HD 16-bit samples are offset-binary `uint16`, centred at `32768`.
- The voltage conversion uses `- vert_offset` and applies the probe factor to the whole expression.
- The time axis adds `time_delay`; its absolute position also needs the horizontal reference position, which the file does not store.

The format fields, conversion formula, and evidence behind the corrections are in [SPEC.md](SPEC.md).

## Install

```sh
pip install git+https://github.com/vindavs/siglent-bin
```

Python 3.11 or newer is required.

## Read a saved waveform

```python
import siglent_bin

d = siglent_bin.read("SDS814X_HD_Binary_C1_30.bin")

values = d["values"]              # float32, in the trace's unit
unit = d["unit"]                  # usually "V" or "A"
t0 = d["t0"]                      # time of sample 0, in seconds
axis = siglent_bin.time_axis(d)   # float64 time axis
raw = d["raw"]                    # stored integer codes
rate = d["sample_rate"]           # samples per second
source = d["source"]              # C1-C8 or F1-F4
```

`values` uses this conversion:

```text
((code - center) * vdiv / code_per_div - voff) * probe
```

`center` is `32768` for a 16-bit file and `128` for an 8-bit file.
Use `raw` or `raw_uint16()` for threshold and PWM decoding when you do not need calibrated values.

```python
samples, rate = siglent_bin.raw_uint16("cap.bin")
```

A multi-channel SDS814X acquisition is saved as one file per channel.
Load those files together and check that their timing fields match:

```python
paths = siglent_bin.find_group("/Volumes/SCOPE", 30)
channels = siglent_bin.read_group(paths)
# {"C1": {...}, "C2": {...}, ...}
```

`read_group()` does not prove that files came from the same acquisition because the V4 header has no acquisition identifier.
Use `strict=False` only when you want to bypass its axis-compatibility check.

### Absolute time

The V4 header does not store the horizontal reference position.
`read()` therefore accepts `ref_position` in percent and defaults to `50`, the screen centre.

```python
d = siglent_bin.read("cap.bin", ref_position=30)
```

The time axis is:

```text
t0  = time_delay - (ref_position / 100) * time_div * grid
t[i] = t0 + i / sample_rate
```

Relative timing remains correct when the reference position is unknown.
The `read()` argument is also applied to zoom records, but non-centre zoom references have not been tested.

The scope stores the horizontal settings that existed when the file was saved.
If the delay or reference controls changed after acquisition and before saving, the stored absolute timing can be wrong.

## Capture over LAN

`siglent_lan` reads waveforms from port `5025` and returns the same fields as `siglent_bin.read()` where they apply.

```python
import siglent_lan

frames = siglent_lan.fetch("192.168.1.50", "C1")
frame = frames[0]
frame["values"], frame["t0"]

channels = siglent_lan.fetch_group("192.168.1.50")
# {"C1": [frame, ...], "C2": [frame, ...]}
```

Sequence acquisitions return one frame per list item.
`fetch()` reads frames individually because the tested SDS814X firmware does not reliably support bulk sequence reads.
It sends `:TRIGger:STOP` before reading by default so a completed sequence buffer becomes addressable.
Pass `stop=False` when a running acquisition must not be disturbed.

Live samples arrive as signed integers, unlike saved-file samples.
The LAN reader normalises them to the saved-file `raw` convention, so live and saved traces can be compared directly.

The live descriptor has no stable unit field on the tested firmware.
For channel sources, the reader queries `:CHANnel<n>:UNIT?` and returns the result as `unit`.
Math and zoom sources have no equivalent query and return `unit=None`.

`descriptor_stamp` is exposed for diagnostics only.
It resembles a date/time on the tested firmware but is not acquisition time.

Siglent documents this scope family as having no RTC.
Synchronise its session clock explicitly when needed:

```python
siglent_lan.sync_clock("192.168.1.50")
```

`fetch()` never changes the scope clock.

The command-line equivalent is:

```sh
siglent-lan 192.168.1.50 C1 C2
siglent-lan 192.168.1.50 --sync-clock
```

## Write a canonical `.bin`

`siglent_bin.write()` accepts one analog `read()` result or one live `fetch()` frame.
It writes a 16-bit V4 archive using the trace's integer `raw` samples, so float32 rounding in `values` is not written back.

```python
import siglent_bin
import siglent_lan

frames = siglent_lan.fetch("192.168.1.50", "C1")
siglent_bin.write("cap_C1.bin", frames[0])
back = siglent_bin.read("cap_C1.bin")
```

The writer rejects math and zoom traces, 8-bit data, fractional `code_per_div`, and unknown units.
It validates the stored fields instead of guessing missing values.

The format has no horizontal reference-position field.
The writer canonicalises the delay to a 50% reference, preserving `t0` for a default read but discarding the original delay/reference pair.
It cannot store sequence bookkeeping or other experiment context, so record that provenance separately when needed.

The output preserves the fields understood by this package, not every byte from the original scope header.
The tested SDS814X HD has no Binary recall control, and its Reference recall rejected an untouched scope-written `.bin` as an illegal format.
Importing generated files into the scope or a third-party reader has not been verified.

Gzip is supported in both directions.
`write("cap.bin.gz", trace)` compresses the output, and `read()` plus `raw_uint16()` detect gzip by its magic bytes rather than by the filename suffix.

The summary CLI reads `.bin` and `.bin.gz` files:

```sh
siglent-bin *.bin
python -m siglent_bin
```

## Export to sigrok

`siglent_sr.write()` creates a sigrok srzip v2 session for PulseView and libsigrokdecode.
It writes float32 analog channels and thresholded logic channels in the same sample index space.

```python
import siglent_bin
import siglent_lan
import siglent_sr

d = siglent_bin.read("cap.bin")
siglent_sr.write("cap.sr", d)
siglent_sr.write("cap.sr", d, threshold=1.65)
siglent_sr.write("cap.sr", d, hysteresis=0.1, decimate=10)

frames = siglent_lan.fetch("192.168.1.50", "C1")
siglent_sr.write_frames("sequence", frames)
```

The default threshold is the midpoint of the trace's 1st and 99th percentiles.
Pass a scalar threshold for all traces or a `{source: threshold}` mapping for per-trace thresholds.
`siglent_sr.write()` records the threshold and edge count in metadata, and `siglent-sr` prints the selected threshold so a poor automatic choice is visible.

The CLI exposes the same controls:

```sh
siglent-sr cap.bin
siglent-sr cap.bin --threshold 1.65 --hysteresis 0.1
siglent-sr cap.bin --decimate 10
siglent-sr cap.bin --no-logic
siglent-sr cap.bin --no-analog
```

The resulting session can be passed directly to a protocol decoder:

```sh
sigrok-cli -i cap.sr -P uart:baudrate=9600 -A uart
```

The size guard refuses oversized sessions and reports a decimation factor that will fit.
Check that narrow pulses survive before using that factor.

Srzip has no trigger-position or per-channel-unit field.
The writer records trigger timing and units in metadata, and appends non-volt units to channel names such as `C1[A]`.
Sigrok still treats those analog values as volts.

## Supported data and limits

- Reads analog channels `C1`-`C8`, math traces `F1`-`F4`, and zoom saves `Z1`-`Z4`.
- Native digital data is not decoded.
  A mixed file returns its supported trace, emits `UnsupportedTraceWarning`, and lists omitted channels in `unsupported_sources`.
  A digital-only file raises `NotImplementedError`.
- Reference waveforms are not parsed.
- Peak-detect samples contain a min/max envelope, but the file has no peak-detect marker.
- A normal binary save from a sequence acquisition contains the displayed segment only.
  Save-all exports do not contain per-segment trigger timestamps, so inter-segment timing cannot be reconstructed.
- Interpolation leaves no marker in the tested files.
- The SDS814X writes 16-bit little-endian samples.
  8-bit, big-endian, and C5-C8 paths have synthetic tests but no corresponding real capture in this repository.
- The amps unit represents the scope's displayed current.
  The file does not separately store physical probe attenuation, so it may not represent circuit current.

## Verification

The fixtures are unmodified SDS814X HD captures.
They cover known levels (0, 3, 4.5, and 5 V), 1×/10× probes, vernier scale, non-centred timing, channel invert, amps mode across V/A factors, and math/zoom saves checked against their parent records.
Sequence behavior, gzip, and sigrok export are also covered by the test suite.
C5-C8, 8-bit, and big-endian paths have synthetic tests only; captures from other SDS models are welcome.

Run the tests with:

```sh
pytest
```

For a general multi-format parser, see [RigolWFM](https://github.com/scottprahl/RigolWFM).
Its Siglent V4.0 conversion differs from the formula verified here.

## License

MIT. See [LICENSE](LICENSE).

## Credit

The format was distilled from Siglent's [*How to Extract Data from the Binary File of SIGLENT Oscilloscope*](https://www.siglenteu.com/wp-content/uploads/dlm_uploads/2025/10/How-to-Extract-Data-from-the-Binary-FileEN03B.pdf) (V4.0), then corrected and extended against real captures.
