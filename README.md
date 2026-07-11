# siglent-bin

A small, dependency-light (numpy-only) Python reader for **Siglent "Binary Format V4.0"**
oscilloscope waveform files — the `.bin` a **Siglent SDS800X HD** (e.g. SDS814X) writes
via *Save → Binary*. Parses the header and returns a trace (channel, math or zoom
save) as volts + a time axis.

The format has two sharp edges this reader gets right: the 16-bit samples are
**offset-binary uint16** (a signed-int16 read silently inverts some traces), and
Siglent's own conversion document shows the wrong sign for the offset term. Field
offsets, the conversion formula, and everything else learned about the format are in
[SPEC.md](SPEC.md), with each fact tagged documented-vs-observed.

## Install

It's a single module with numpy as its only dependency (Python 3.9+). Either
vendor it — drop `siglent_bin.py` next to your code — or install it:

```
pip install git+https://github.com/vindavs/siglent-bin
```

## Usage

```python
import siglent_bin

# One file = one trace
d = siglent_bin.read("SDS814X_HD_Binary_C1_30.bin")
d["values"]       # numpy float64 array — volts (amps if the channel was in amps display)
d["unit"]         # 'V' or 'A'
d["time"]         # seconds
d["sample_rate"]  # Sa/s
d["source"]       # 'C1'..'C8', or 'F1'..'F4' for a math trace

# Just the raw samples for a threshold/PWM decoder (offset-binary uint16, correct polarity)
samples, fs = siglent_bin.raw_uint16("cap.bin")

# A multi-channel acquisition is saved as several files sharing an index.
paths = siglent_bin.find_group("/Volumes/SCOPE", 30)   # ..._C1_30.bin, ..._C2_30.bin, ...
chans = siglent_bin.read_group(paths)                  # {'C1': {...}, 'C2': {...}, ...}, aligned
```

Or as a CLI summary:

```
$ siglent-bin *.bin        # pip-installed; `python -m siglent_bin` also works
cap_C1_30.bin: C1  20 kSa/s  100 kpts  0.5 V/div  probe 10x  -0.12..3.31 V
```

## Scope & limitations

- Reads analog channels (`C1`–`C4`, plus `C5`–`C8` per the format doc), math traces
  (`F1`–`F4`) and zoom (`Z1`–`Z4`) saves. Zoom saves hold a slice of the parent record
  and get their time axis from the stored zoom timebase. Digital (D0–D15) channels and
  reference waveforms aren't parsed.
- A channel in amps display mode comes back in amps (`unit` is `'A'`) — but that is the
  scope's reading: in amps mode the physical probe attenuation isn't recorded in the
  file (see SPEC.md).
- Mostly, acquisition/display state does not reach the file: interpolation and peak
  detect leave no header trace (peak-detect samples interleave a min/max envelope),
  sequence mode exports segments as ordinary files with no timestamps, and the header
  records the save-time horizontal delay, not the acquisition-time one (see SPEC.md).
- The SDS814X HD always writes 16-bit little-endian; the 8-bit (`data_width=0`) and
  big-endian paths exist because the format defines them, but this scope doesn't
  produce them.
- For digital/threshold decoding, use `raw`/`raw_uint16` and skip the volts
  conversion entirely.

## Verification

The conversion math is calibrated against SDS814X captures of known levels
(0 / 3 / 4.5 / 5 V) across probe 1×/10×, several vertical and horizontal settings
(including a vernier V/div and a window that excludes 0 V), channel invert, amps
display across V/A factors, and math/zoom saves checked against their parent records.
Those captures, unmodified, are the test fixtures in this repo:
`python3 tests/test_siglent_bin.py` (or `pytest`). The C5–C8, 8-bit and big-endian
paths are exercised only by synthetic tests — sample files from other SDS models
welcome.

For a general multi-format parser see
[RigolWFM](https://github.com/scottprahl/RigolWFM); note that as of 1.5.0 its Siglent
V4.0 conversion follows the vendor document as written (`+ vert_offset`, no probe
factor) and disagrees with these captures on both counts.

## License

MIT — see [LICENSE](LICENSE).

## Credit

Format distilled from Siglent's [*How to Extract Data from the Binary File of SIGLENT
Oscilloscope*](https://www.siglenteu.com/wp-content/uploads/dlm_uploads/2025/10/How-to-Extract-Data-from-the-Binary-FileEN03B.pdf)
(V4.0), corrected and extended by reverse-engineering real captures.
