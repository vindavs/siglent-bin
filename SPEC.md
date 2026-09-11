# Siglent "Binary Format V4.0" — header notes

Field offsets and the conversion formula for the `.bin` files a **Siglent SDS800X HD**
(e.g. SDS814X) writes via *Save → Binary*. Distilled from Siglent's own document
[*How to Extract Data from the Binary File of SIGLENT Oscilloscope*][doc] (V4.0), then
checked against real captures. Each field below is tagged **[doc]** (stated in Siglent's
document) or **[obs]** (established by observation here — treat as inference).

[doc]: https://www.siglenteu.com/wp-content/uploads/dlm_uploads/2025/10/How-to-Extract-Data-from-the-Binary-FileEN03B.pdf

## Layout

- Header is **4096 bytes (0x1000)**; sample data follows at `data_offset_byte`. **[doc]**
- All multi-byte fields are little-endian. **[doc]**
- The SDS814X writes **exactly one trace per `.bin`**. A multi-channel acquisition is
  saved as several files sharing an index (`..._C1_30.bin`, `..._C2_30.bin`, …), each a
  complete single-trace file. **[obs — verified on a 4-channel acquisition: four files,
  each with one `ch_on` bit set and one channel's worth of samples.]** The format itself
  also defines sequential multi-trace files (per-channel blocks, then math, then
  digital) **[doc]** — never observed from this scope, and not parsed here.
- `wave_length` matched the stored sample count — `(file size − data_offset_byte) /
  sample width` — on every genuine capture checked (88 files at 2 k / 100 k / 10 M
  points, several timebases, single- and multi-channel). **[obs]** A file holding less
  data than `wave_length` claims is truncated, not a known save mode.

## Header fields

`c` = 0-based channel index (0–3).

| Field | Offset | Type | Tag | Notes |
|---|---|---|---|---|
| `version` | 0x00 | int32 | [doc] | `4` for V4.0 |
| `data_offset_byte` | 0x04 | int32 | [doc] | start of sample data (= 0x1000 on HD) |
| `ch_on[c]` | 0x08 + 4·c | int32 | [doc] | 1 = channel enabled |
| `volt_div_val[c]` | 0x18 + 0x28·c | Data-With-Unit | [doc] | V/div |
| `vert_offset[c]` | 0xb8 + 0x28·c | Data-With-Unit | [doc] | vertical offset (V) |
| `time_div` | 0x19c | Data-With-Unit | [doc] | s/div |
| `time_delay` | 0x1c4 | Data-With-Unit | [doc] | trigger delay (s) |
| `wave_length` | 0x1ec | int32 | [doc] | sample count (matches the stored data — see Layout) |
| `sample_rate` | 0x1f0 | Data-With-Unit | [doc] | Sa/s (verified: 1e7, 5e5, 2e4 …) |
| `digital_sample_rate` | 0x21c | Data-With-Unit | [doc] | digital chans; often equals analog |
| `probe[c]` | 0x244 + 8·c | float64 | [doc] | probe attenuation (e.g. 10.0) |
| `data_width` | 0x264 | uint8 | [doc] | 0 = 8-bit, 1 = 16-bit |
| `byte_order` | 0x265 | uint8 | [doc] | 0 = little-endian, 1 = big-endian |
| `hori_div_num` | 0x26c | int32 | [doc] | horizontal grid divisions (≈10) |
| `code_per_div[c]` | 0x270 + 4·c | int32 (signed) | [doc] | ADC codes per vertical division |
| `math_switch[m]` | 0x280 + 4·m | int32 | [doc] | 1 = math trace m enabled (m = 0–3) |
| `math_vdiv_val[m]` | 0x290 + 0x28·m | Data-With-Unit | [doc] | math V/div |
| `math_vpos_val[m]` | 0x330 + 0x28·m | Data-With-Unit | [doc] | math vertical position |
| `math_store_len[m]` | 0x3d0 + 4·m | int32 | [doc] | math sample count (its wave_length) |
| `math_f_time[m]` | 0x3e0 + 8·m | float64 | [doc] | math sample interval, s (= 1/rate) |
| `math_vert_code_per_div` | 0x400 | int32 | [doc] | codes per division, shared by all math traces |
| CH5–CH8 bank | 0x404–0x583 | — | [doc] | same shape as CH1–4: `ch_on` 0x404+4·c, V/div 0x414+0x28·c, offset 0x4b4+0x28·c, probe 0x554+8·c, code/div 0x574+4·c. Untested here (4-channel scope) |
| memory (REF) fields | 0x664–0xaf3 | — | [doc] | reference-waveform switches/scales; not parsed here |
| `zoom_switch` | 0xaf4 | int32 | [doc] | 1 = this file is the zoom trace (see Zoom saves) |
| `zoom_td_val` | 0xaf8 | Data-With-Unit | [doc] | zoom window s/div |
| `zoom_trig_delay_val` | 0xb20 | Data-With-Unit | [doc] | zoom window delay (centre position) |
| `zoom_vdiv_val` / `zoom_vpos_val` | 0xb48 / 0xc88 | 8× Data-With-Unit | [doc] | zoom vertical display scale — the stored codes stay in channel units |
| sample data | 0x1000 | uint16[] / uint8[] | [doc] | offset-binary — see encoding below |

The digital (LA) enables sit at `digital_on` 0x158 and `d0_d15_on[i]` 0x15c + 4·i, with
`digital_wave_length` at 0x218 **[doc]**. Digital payload encoding is not implemented or
verified. A mixed file returns its supported analog/math trace, warns, and exposes the
omitted D-channel names in `unsupported_sources`; a digital-only file is rejected. Gzip
input drains an omitted payload to verify the stream CRC without interpreting its bytes.

## Trace contract

The file and LAN readers return ordinary dictionaries described by
`siglent_bin.Trace`. Their shared waveform fields are:

```text
source, sample_rate, time_div, time_delay, grid, npoints, data_width,
vdiv, voff, code_per_div, probe, unit, raw, values, t0
```

`raw` holds the stored integer codes: 16-bit traces are offset-binary around
32768 and 8-bit file reads around 128. `values` is float32 in the channel's
reported unit. `t0` is the time of sample zero; `time_axis(trace)` constructs
the float64 per-sample axis on demand.

File reads additionally expose `unit_raw`, `zoom`, the `ref_position` used to
interpret an original save, and `digital_enabled` / `unsupported_sources` for native
digital data detected but not returned; no digital sample encoding is inferred from
these fields. Live frames add sequence bookkeeping,
`ref_position`/`ref_strategy`, and diagnostic `descriptor_stamp`. The latter is
not acquisition time.

Adapters validate the subset they consume. In particular, `write()` requires a
finite `t0` and a 16-bit analog trace, while `siglent_sr.write()` requires all
traces in one session to share `sample_rate`, length, and `t0`. This is the
canonical schema; bench orchestration may wrap it with experiment provenance
but should not redefine it.

### Data-With-Unit (the scalar fields above)

40-byte structure:

- `value` — float64 at +0x00
- `magnitude` — int32 at +0x08 (SI-prefix index)
- `unit descriptor` — 7× int32 at +0x0c (see Units below)

Effective value = `value × 10^(3·(magnitude − 8))` **[doc]**: index 8 = ×1, 7 = milli,
6 = micro, 9 = kilo, 10 = mega, … If you read only the raw float64 you may be off by a
power of 1000 (a V/div can be stored as `2.48` with magnitude = milli). Sample-rate
values here were stored at magnitude 8, which is why a naive float64 read of 0x1f0
already looked right.

### Units

**[doc]** The 7×int32 descriptor at Data-With-Unit + 0x0c is
`[type, V_num, V_den, A_num, A_den, s_num, s_den]`. Type 0 composes the unit from
rational powers of V, A and s; other types name a unit directly (1 dBV, 2 dBA, 3 dB,
4 Vpp, 5 Vdc, 6 dBm, 7 Sa, 8 div, 9 pts, 10 none, 11 degree, 12 percent). Observed on
real captures:

| descriptor | unit |
|---|---|
| `(0, 1, 1, 0, 1, 0, 1)` | volts — V¹ (channel `volt_div_val`, `vert_offset`) |
| `(0, 0, 1, 1, 1, 0, 1)` | amps — A¹ (channel in a current-display mode) |
| `(0, 0, 1, 0, 1, 1, 1)` | seconds — s¹ (`time_div`) |

`volt_div_val` is stored *pre-probe*, so `probe` is part of the conversion. What `probe`
denotes depends on the display mode:

- **V mode:** the voltage attenuation (verified at 1× and 10×).
- **Amps mode:** a **1/(V/A)** factor. Verified across a sweep with a fixed 3 V source:
  filenames `1va`/`10va`/`0.1va`/`0.01va` stored `probe` = 1 / 0.1 / 10 / 100, and
  `base·probe` reproduced the scope's amps reading (0.3 / 0.03 / 3 / 30 A) each time.

⚠️ In amps mode the **physical probe attenuation is not separately recorded** — the `probe`
field is reused for the V/A factor. So a capture taken with a 10× probe in amps mode yields
the scope's *displayed* current (computed from the scope-input voltage), which is not the
real circuit current unless the probe ratio is 1× or otherwise accounted for externally.
This was a voltage source shown in amps mode, not a real current clamp — though to the
scope input a clamp is just another voltage source plus the same V/A setting, so no format
difference is expected. Read the unit descriptor and treat non-`V` values as "the scope's
reading in that unit," not calibrated physical quantities.

## Sample encoding — the one that bites

**[doc]** 16-bit samples (`data_width = 1`) are **offset-binary**, mid-scale (0-code
reference) at **32768**. 8-bit samples are centred at 128.

**[obs]** Read them as **unsigned** uint16. Reading as *signed* int16 inverts the trace
whenever its two logic levels sit on opposite sides of 32768 — codes above 32768 wrap to
negative and their order flips relative to codes below. A trace with both levels on the
same side survives a signed read unscathed, which makes the bug intermittent and easy to
miss. This library reads uint16 to avoid it.

**[obs]** 12-bit ADC codes are scaled to fill the 16-bit range (mid-scale 32768, not
2048), i.e. left-justified; `code_per_div` already reflects that scaling, so no shift is
applied by hand.

## Volts

**[obs]** `volts = ((code − center) · volt_div_val / code_per_div − vert_offset) · probe`,
with `center = 2^(data_width·8 − 1)` (32768 for 16-bit).

Calibrated against known references: a **0 V / 4.5 V** logic capture (both levels
reproduced to ~6 mV), a 3 V PSU line at probe 1× and 10×, a flat **4.5 V DC at
20 mV/div** with 0 V far outside the screen window, and a 0 → **5 V** step at a
**0.315 V/div vernier** setting (each to a few tens of mV, limited by source accuracy).
Two departures from a naive reading of the source doc:

- **Offset is `− vert_offset`, not `+`.** Siglent's document shows `+ vert_offset` for
  saved files (its live-SCPI path uses `−`); on these V4.0 HD files the `−` sign is what
  matches ground truth. Confirmed by fitting all four ±/probe variants against the two
  known levels — only this one landed on 0 V and 4.5 V. The same PDF's `.slg`
  sample-logger chapter likewise *subtracts* its offset term, so the vendor's sign
  conventions differ even between its own formats.
- **Probe multiplies the whole expression** (offset included). On the verified captures
  `volt_div_val` did *not* already include the 10× probe — only ×probe gives both the
  right swing and the right absolute levels.

The formula holds across vertical settings from 20 mV/div to a 3.15 V/div-effective
vernier, including a window that excludes 0 V entirely (large offset term). For
threshold/PWM decoding of an analog trace you don't need volts — threshold the raw
uint16.

## Channel invert & channel name

**[obs]** **Channel invert leaves no header flag.** Two captures of one signal that differ
only in the front-panel Invert toggle have *identical* headers (the only differing bytes
are uninitialised pointer-like values near 0x200–0x23f that vary save-to-save). The
inversion is baked into the stored samples — an inverted 0 V/+3 V signal is stored as, and
reads back as, a 0 V/−3 V trace. So a reader needs no invert handling: it faithfully
reproduces whatever was captured. (It also cannot *warn* that invert was on — there's
nothing to read. Corollary: an unexpectedly inverted trace is a scope-setting story, not a
file-parsing one — distinct from the signed-vs-uint16 issue above, which *is* a parsing bug.)

**[obs]** **The channel name/label is not stored** in the `.bin`. A capture whose channel
was named "custom" contains that string nowhere (ASCII or UTF-16LE). Don't expect to
recover channel labels from the file.

## Zoom (Z1–Z4) saves

Saving a zoom trace (`..._Z<n>.bin`, one per zoomed source channel) produces a
structurally normal single-trace file: same layout, `ch_on` still marks the source
channel, and the samples are a contiguous slice of the parent record — byte-identical
to the corresponding range of the `..._C<n>.bin` saved from the same acquisition.
**[obs]** The zoom window's own timebase is stored: `zoom_switch` (0xaf4) marks the
file as the zoom trace, and the doc directs readers to `zoom_td_val` /
`zoom_trig_delay_val` for its time stamps **[doc]** — those two are single fields (the
zoom window is horizontal, shared by all channels) while `zoom_vdiv_val` /
`zoom_vpos_val` are per-channel arrays. Only C1's zoom trace has been exercised here.

- **[obs]** The window centre sits at **+`zoom_trig_delay_val`**:
  `t0 = delay − td·grid/2`. Verified against byte-located slices of three captures with
  window centres read off the screen (+15 ms and +20 ms at 2 ms/div, +20 ms at
  5 ms/div). This is the 50% reference-position case of the general time-axis
  expression below.
- **[obs]** Vertical conversion still uses the source channel's vdiv/offset/code-per-div
  — the stored codes are unchanged from the parent record. (`zoom_vdiv_val` /
  `zoom_vpos_val` describe the zoom display only.)

## Math (F1–F4) saves

A math trace saves like a channel: `ch_on` all zero, the slot's `math_switch` set, and
samples in the same offset-binary encoding. Conversion uses the math header fields —
`math_vdiv_val`, `math_vpos_val`, the shared `math_vert_code_per_div`, and
`math_store_len` / `math_f_time` in place of the analog wave_length / sample_rate.
Same formula, same `− vpos` sign, and **no probe factor**. **[obs — an
F1 = invert(C1+C1) trace positioned at −20 V reproduced −2× the companion C1 save to
<1 mV across the whole record.]**

## Time axis

⚠️ **[obs]** The time axis needs the scope's horizontal reference position `P`
in addition to the stored header fields:

```text
t0 = -(P/100) * time_div * hori_div_num + time_delay
t[i] = t0 + i / sample_rate
```

The expression was verified on three trigger-synchronised captures at `P = 30%`:

| capture | P | span | delay | predicts | trigger observed at |
|---|---|---|---|---|---|
| `trigsync_neg100ms.bin` | 30 % | 1000 ms | -100 ms | sample 4000 | 3996 |
| live, 20 ms record | 30 % | 20 ms | -8 ms | sample 7000 | 7003 |
| live, 100 ms record | 30 % | 100 ms | 0 | sample 3000 | 3000 |

⚠️ **[obs]** **The delay term adds.** Siglent's document and RigolWFM place the
record at `-time_div*grid/2 - time_delay`; the sign was settled against a
trigger-locked feature. Five captures of the 1 kHz calibration square
(20 µs/div, 10 kpt at 50 MSa/s), each a **fresh single acquisition**, put the
triggering edge exactly where `+time_delay` predicts:

| capture | P | `time_delay` | edge at | `+delay` predicts | `-delay` predicts |
|---|---|---|---|---|---|
| `tdelay-ref50-d0` | 50 % | 0 | 5000 | 5000 | 5000 |
| `tdelay-ref50-dp50us` | 50 % | +50 µs | 2500 | 2500 | 7500 |
| `tdelay-ref50-dn50us` | 50 % | -50 µs | 7500 | 7500 | 2500 |
| `tdelay-ref20-d0` | 20 % | 0 | 2000 | 2000 | 2000 |
| `tdelay-ref20-dp20us` | 20 % | +20 µs | 1000 | 1000 | 3000 |

Each file's samples are byte-identical to the LAN fetch of the same
acquisition, and reading it at the reference position then in force placed
t = 0 on the edge to within one sample. The `ref20` rows are also what rules
out `-time_div*grid/2` on its own: at P = 20 % with no delay the trigger is at
sample 2000, three divisions from centre.

The V4 header does not store `P`: a scan of every int32 in the 4 KB header found
only `0x26c = 10`, the horizontal division count. `read()` therefore takes
`ref_position=` and defaults to screen centre (`50%`), while `fetch()` queries
the current value. Sample spacing remains valid if the position is unknown, but
the absolute placement of zero does not. Neither function materialises the full
axis; `siglent_bin.time_axis()` returns the float64 `t[i]` values.

`write()` canonicalises the stored delay to 50% so a default read reproduces the
input trace's `t0`; it does not preserve the original front-panel delay/reference
pair. `:TIMebase:REFerence` selects the strategy (`DELay` or `POSition`); only
`DELay` has been tested. `siglent_lan` reports the queried strategy as
`ref_strategy`.

⚠️ **[obs]** The header stores the **save-time** horizontal settings, not the
acquisition-time ones — verified directly: re-saving a stopped acquisition after turning
the delay knob produced byte-identical samples with only the stored `time_delay` changed
(0.1 → 0.2). The samples keep their acquisition-time alignment (the zoom captures above
showed a 0.2 s offset from exactly this), so treat absolute time as reliable only if the
horizontal controls weren't touched between stop and save. `tdelay-ref50-dp50us-stale`
is that case as a fixture: panned from the -50 µs acquisition to +50 µs without
re-arming, it carries the samples of `tdelay-ref50-dn50us` under the header of
`tdelay-ref50-dp50us`, and its stored axis misplaces the trigger by 100 µs under
either sign convention. This is why a delay pair taken from one stopped
acquisition cannot settle the sign above; only a fresh acquisition at each delay
moves the samples.

## Acquisition & display state — what reaches the file

A/B-verified on the SDS814X by re-saving controlled acquisitions: **[obs]**

- **Interpolation (x vs sinc):** the two tested saves were byte-identical; no
  interpolation marker or sample change was observed.
- **Peak detect:** no header marker (byte-identical header to a normal-mode save at the
  same settings), but the samples interleave a min/max envelope — strong lag-1
  anticorrelation and an even/odd level split on flat regions. Take pairwise min/max of
  consecutive samples for the envelope; timing resolution is half the claimed rate.
- **Sequence (segmented) acquisition:** a plain binary save exports only the *displayed*
  segment as an ordinary file — no header marker, no stitching (a square wave in the
  export had perfectly uniform periods, and the record equalled the last/displayed
  segment of the run). The save-all path writes a folder of `seg0000N.bin` — one
  ordinary single-trace file per segment, plus a text `.awg` replay file. Per-segment
  headers differ only in two undocumented float64s at 0xe00/0xe08 holding the segment's
  min/max value (zero in normal saves); **no per-segment trigger timestamps are
  stored**, so inter-segment timing is not recoverable from a binary export.
- **Reference (REF) traces:** displaying one changes nothing — the memory_* fields
  stayed zero. REFs export only to the separate `.ref` format (not parsed here).
  In one same-acquisition comparison, the V4 `.bin` and live SCPI transfer held
  1,000,000 identical raw samples, while the 7,464-byte `.ref` contained a
  3,000-value waveform region whose transition was at the same relative horizontal
  position. On the scope, the reference retained vertical scale and offset and
  could be scaled or moved vertically. It retained the 100 ms horizontal span,
  but displayed it as -50 to +50 ms around screen centre. The matched live trace
  used a 30% horizontal reference and ran from -30 to +70 ms; its edge remained
  near 30% in the reference overlay rather than moving to the displayed zero.
  Acquisition pan and zoom did not move the overlay. This establishes that
  `.ref` is a reduced display-oriented representation with vertical and span
  metadata, not a V4 wrapper or a trigger-relative archive; it does not establish
  the raw-coordinate amplitude conversion or full field layout.
- **Binary recall:** the tested SDS814X HD Save/Recall menu offered Setup,
  Reference, Factory Default, and Security Erase, but no Binary recall. Reference
  recall rejected an untouched scope-written V4 `.bin` with “File format is
  illegal.” A generated file was not tried because the native control already
  established that this is not a `.bin` import path. This says nothing about
  other models or third-party readers.
- **XY mode / "Save All Channels":** ordinary per-channel time-domain files, identical
  to saving each channel individually. No UI path produced the doc's sequential
  multi-trace single file.

## Scope of verification

Everything tagged **[obs]** was confirmed on SDS814X HD captures at several timebases and
sample rates, single- and (as file groups) four-channel, at probe 1× and 10×, with volts
calibrated against known 0/3/4.5/5 V references — including a vernier (non-1-2-5) V/div
and a window excluding 0 V — and channel-invert, zoom (Z1), math (F1), sequence-segment,
peak-detect and XY saves exercised. Still **not exercised** by any file on hand, so implemented-from-doc-only or
untested: 8-bit (`data_width = 0`), big-endian (`byte_order = 1`), CH5–CH8 (8-channel
models), digital (D0–D15) payload decoding, reference/memory waveforms, Average/ERES
traces (math operators on this model, so expected to save as ordinary F-trace files), and a real
current-clamp probe (the amps case here was a voltage source in amps-display mode). The 8-bit,
big-endian and CH5–CH8 code paths are locked by synthetic derivations of real captures
in the test suite — not the same as real files. Digital detection/warning behavior is
also tested synthetically; its payload remains opaque. Corrections/captures from other SDS
models welcome.

## Live SCPI path (`:WAVeform:DATA?`) vs saved files

Pulling a waveform over LAN returns the same physical trace through a different
descriptor with **different conventions**. Sources: the SDS800X HD programming
guide (EN11F), including its "Read Sequence Waveform Data Example" (p. 782).

- **[doc]** The preamble block is a **346-byte descriptor**, with its own offsets
  (nothing in common with the 4 KB file header): `data_width` 0x20, `data_order`
  0x22, points-per-frame 0x74, `data_interval` 0x88 (the echoed
  `:WAVeform:INTerval` stride), `read_frames` 0x90, `sum_frames` 0x94, `vdiv`
  0x9c, `voff` 0xa0, `code_per_div` 0xa4 (**float**, not the file's int32),
  `adc_bit` 0xac, sample `interval` 0xb0 (float seconds), `delay` 0xb4 (double),
  timebase *index* 0x144 (into an enum, not a value), `probe` 0x148.
- ⚠️ **[obs]** **`:WAVeform:INTerval` is persistent session state.** It sets a
  stride (return every Nth point) and persists across connections, so `siglent_lan` writes
  `1` on every read alongside `STARt`/`POINt`. Measured on the SDS814X HD (fw
  4.8.12.1.1.6.5) across strides 1/2/7/10:
  - the descriptor **echoes the stride back at 0x88** (int32) exactly — 1, 2, 7, 10;
  - the query form `:WAVeform:INTerval?` exists and answers;
  - the point count at 0x74 keeps reporting the **full record** (1000000) at every stride,
    while the payload carries `ceil(record/stride)` samples;
  - the sample `interval` at 0xb0 stays the acquisition spacing (1.0e-07 at 10 MSa/s) and
    does **not** track the stride.

  On this scope a leftover stride makes the point count and payload disagree, so
  the short-transfer check trips (`285714 data bytes for 1000000 samples`).
  Verified by disabling both the write and the echo check with a stride of 7
  set. The 0x88 check is kept to identify the cause, and
  because a firmware reporting the *delivered* count at 0x74 would be self-consistent and
  detectable no other way. A 0 at 0x88 means the field is not populated, not a stride.

  These point-count and stride semantics are specific to the tested SDS814X HD
  firmware; validate them before applying the checks to another model or release.
- ⚠️ **[obs]** **`code_per_div` does not change with `:WAVeform:WIDTh`** on the
  tested firmware: BYTE and WORD both report `code_per_div=7680` and `adc_bit=16`,
  differing only in `data_width` (0 vs 1) and payload (1 vs 2 bytes per point). A BYTE read
  scaled by a WORD `code_per_div` is 256x too small. `fetch()` prevents that
  mismatch by forcing WORD whenever `adc_bit > 8` before reading the scaling
  preamble.
- ⚠️ **[obs]** **A zeroed descriptor is not a sequence-mode condition.** It reads all zeroes
  whenever **no acquisition has completed** — `:TRIGger:MODE NORMal` with nothing on the
  trigger source sits at `Ready` indefinitely and nothing is addressable, sequence off or
  on. `:TRIGger:MODE AUTO` force-triggers and makes a record available.
- ⚠️ **[obs]** **`:TRIGger:STATus?` leaving `Ready` does not mean a record exists.** It
  reads `Auto` as soon as the mode takes effect, well before a 1 Mpt acquisition has
  landed; stopping on that reads a zeroed descriptor. Wait for the acquisition (or retry
  the preamble until it parses) rather than polling the status.
- ⚠️ **[obs]** **No stable unit field was identified in the live descriptor on
  this firmware.** Diffing a transfer
  in amps display mode against one in volts shows a single differing region,
  `0x0d8..0x0d9` — and two *identical* runs (nothing touched between them) differ
  in that same region. The bytes around it read as `0x7f...` x86-64 addresses,
  consistent with uninitialised firmware memory. That region is not treated as
  stable descriptor data. The unit
  comes from `:CHANnel<n>:UNIT?` instead, which answers `V` / `A` cleanly (short
  form `:CHAN<n>:UNIT?` works). On this firmware,
  `:CHANnel<n>:PROBe:UNIT?` is answered with silence, so the unsupported query
  costs a socket timeout rather than returning an error.
- **[obs]** **Changing the unit rescales nothing.** `:CHANnel<n>:SCALe?`,
  `:OFFSet?` and `:PROBe?` were byte-identical between an amps run and a volts
  run, so the unit is a label and the vdiv/code_per_div conversion is unaffected
  by it.
- ⚠️ **[obs]** On SDS814X HD firmware 4.8.12.1.1.6.5, the 16-byte
  preamble tail decodes as clock-like date/time fields (seconds float64, then
  minute/hour/day/month bytes and year int16) but does **not** represent
  acquisition time. Acquisitions did not move it, it did not continuously
  follow the running clock, and other operations could update it. Its writer
  remains unresolved. `siglent_lan` therefore exposes it only as diagnostic
  `descriptor_stamp`; consumers must not use it as a capture or trigger time.
  Per-segment timing is not recovered from this field, and `.bin` exports do not
  contain it.
- **[doc]** **The manual documents no RTC.** Section 30.2.5 says: *"The SDS800X HD does not have RTC clock, which can be
  synchronized through the NTP protocol or manually set for Date/Time."* The clock resets
  across boots on the tested unit. `:SYSTem:DATE` / `:SYSTem:TIME` are writable over SCPI;
  `siglent_lan.sync_clock()` exposes that operation explicitly and `fetch()` never calls it.
- **[obs]** **This HD tail differs from scopehal's classic LeCroy TRIGTIME
  layout.** scopehal interprets the 16 bytes as two float64 values; the tested HD
  descriptor instead yields the date/time-shaped fields described above. Treat
  these as distinct descriptor layouts rather than interchangeable records.
- **[doc]** Samples arrive as **signed** integers centred on 0 (the vendor example
  unpacks `h`), where a saved file stores offset-binary uint16 centred on 32768.
  `siglent_lan` shifts by +32768 so both paths hand back the same convention.
- ⚠️ **[obs]** `:WAVeform:SEQuence` does **not** behave as documented here. The doc
  says `value1 = 0` returns every frame that fits one transfer, with `value2`
  walking the remainder. On this scope:
  - `value1 = 0` **collapses `sum_frames` to 1** and returns a single frame -- it
    discards the run's addressability rather than bulk-transferring it;
  - a `value2` other than `1` (e.g. the documented `2,2` for a second frame)
    **wedges the instrument** -- it stops answering and needs `*CLS` to recover;
  - `value1 = <n>, value2 = 1` selects frame *n* correctly and leaves
    `sum_frames` intact; this is the form `siglent_lan` uses.

  So a sequence run is read frame-by-frame, not in one bulk transfer. Repeated
  reads were non-destructive in the tested runs.
- ⚠️ **[obs]** **A completed sequence run leaves the scope at `Ready`, not `Stop`,
  and its segment buffer is unaddressable in that state** -- the whole descriptor
  reads zero (no points, no `code_per_div`, no `adc_bit`) and `:HISTORy:FRAMe?`
  reads 0. On the tested model/firmware, sending `:TRIGger:STOP` before transfer
  made the completed buffer addressable; `siglent_lan.fetch()` therefore does so
  by default. Without that step, re-arming and refilling were observed instead.
- **[obs]** Block responses carry a **response header in sequence mode** --
  `:WAVeform:PREamble?` answers `C1:WF #9000000362...` with sequence on and a bare
  `#9...` with it off. Anything before the `#` must be skipped, not treated as an
  error. Its optional terminator may arrive in a later TCP packet; text reads
  discard an empty framing line so it cannot shift subsequent responses.
- ⚠️ **[obs]** **The delay field at 0xb4 is referred to screen centre.** Measured
  on the SDS814X HD (fw 4.8.12.1.1.6.5) over 4 reference
  positions x 3 delays, 12/12 exact:

      desc_delay = :TIMebase:DELay? + (0.5 - ref_position/100) * time_div * grid

  `:TIMebase:DELay?` is referred to the *reference position*; 0xb4 is referred to centre. The
  two axes are therefore identical, and both of these give the same `t0` at every setting:

      ours:   t0 = delay - (ref_position/100) * time_div * grid
      centre: t0 = desc_delay - time_div * grid / 2

- ⚠️ **[obs]** **The timebase index at 0x144 was not portable on the tested model.** It read 20 — whose enum
  entry is 500 us — while the scope reported 1 ms/div. The guide itself says the enumeration
  is model-dependent (p.756, Table 2: "Different models have different time base
  enumeration"). `siglent_lan` therefore takes the timebase from
  `:TIMebase:SCALe?` rather than applying a cross-model enum mapping.
- **[obs]** The vendor's live axis formula (`t = -tdiv*grid/2 + i*interval + delay`) *adds*
  the delay term where the saved-file formula subtracts it. With the centre-referred reading
  above, the vendor's form is the consistent one for 0xb4 and the file's form is the
  consistent one for `:TIMebase:DELay?`. `descriptor_audit()` reports the queried
  reference position, the expected centre-referred delay, their difference and
  match result, along with `data_interval`.
- **[obs]** The descriptor's sample `interval` was finite in the tested normal and
  sequence captures. `fetch()` nevertheless prefers `:ACQuire:SRATe?` and accepts
  the descriptor value only as a validated fallback.
- **[obs]** `code_per_div` is a **float32** here (0xa4); the file header stores it
  as a signed int32. Sample `interval` (0xb0) is float32 too, so deriving the rate
  from it lands ~2.5 ppb off the file header's float64 value -- hence the
  `:ACQuire:SRATe?` query.
- **[obs]** **End-to-end checked against a saved file.** With the scope stopped on
  one acquisition, live C1/C2/C3 transfers and saved files had identical raw
  samples. Converted values agreed within float32 rounding and time axes within
  float64 roundoff. This validates the signed-to-offset-binary conversion and
  axis convention on the tested model/firmware.

## Saving files over SCPI

The scope can write its own `.bin` to a USB stick without front-panel work, which
is how the delay-sign fixtures below were made.

- **[doc]** `:SAVE:BINary <path>,<src>` saves the on-screen trace of `<src>`
  (`C<x>` | `F<x>` | `M<x>` | `D0_D15`) to a quoted path — `"U-disk0/name.bin"`,
  `"local/SIGLENT/name.bin"`, or `"net_storage/name.bin"`. `:SAVE:CSV`,
  `:SAVE:MATLab`, `:SAVE:IMAGe`, `:SAVE:SETup` and `:SAVE:REFerence` are the
  siblings. The extension is not what selects the format (EN11F p. 342).
- ⚠️ **[obs]** **The `:SAVE:*` headers are command-only.** Querying one
  (`:SAVE:TYPE?`, `:SAVE:WAVeform?`) answers `-113,"Undefined header"`, which
  reads exactly like the whole subsystem being absent from the firmware. Probe
  with the command form and the error queue instead.
- **[obs]** There is no directory listing, so the error queue is the only
  confirmation: a bad path or `<src>` gives `-101,"Invalid character"`, a missing
  `<src>` gives `-200,"Execution error"`, and a good save leaves the queue clean.
  Existence of a file can be inferred from `:RECall:SETup EXTernal,"<path>"` —
  an existing file of the wrong format answers `-200`, an absent path `-101`.
- ⚠️ **[obs]** **`:SYSTem:ERRor?` pops one entry per query.** Drain it in a loop
  before using it as a pass/fail signal, or an error from an earlier command
  reads as the current one failing.
- ⚠️ **[obs]** **`:ACQuire:MDEPth` does not stick when written alongside a
  timebase change.** Write it last, then read it back; a depth written in the
  same burst as `:TIMebase:SCALe` was silently dropped.

Model- and firmware-specific capture setup and measurement practice are kept in
the bench SDS800X HD note,
not in this file-format specification.
