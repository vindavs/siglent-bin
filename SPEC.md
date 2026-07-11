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
`digital_wave_length` at 0x218 **[doc]** — digital data isn't parsed here.

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
  known levels — only this one landed on 0 V and 4.5 V.
- **Probe multiplies the whole expression** (offset included). On the verified captures
  `volt_div_val` did *not* already include the 10× probe — only ×probe gives both the
  right swing and the right absolute levels.

The formula holds across vertical settings from 20 mV/div to a 3.15 V/div-effective
vernier, including a window that excludes 0 V entirely (large offset term). For
digital/threshold decoding you don't need volts — threshold the raw uint16.

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
  5 ms/div) — only the `+delay` sign fits all three. The main axis uses `− time_delay`
  (verified — see Time axis), so the opposite zoom sign is a real quirk of the format,
  not a doc error.
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

**[doc]** `t[i] = −(time_div · hori_div_num / 2) − time_delay + i / sample_rate`.

**[obs]** The `− time_delay` sign is verified: three trigger-synced sequence segments
(rising-edge trigger at ~2 V, delay at −100 ms) put the trigger-level crossing at
samples 4027–4029 of 10 000 — i.e. t = 0 at sample 4000 exactly as the formula places
it, with the same ~2.05 V at that sample in every segment. A flipped sign would have
put t = 0 at sample 6000, 200 ms after the edge completed.

⚠️ **[obs]** The header stores the **save-time** horizontal settings, not the
acquisition-time ones — verified directly: re-saving a stopped acquisition after turning
the delay knob produced byte-identical samples with only the stored `time_delay` changed
(0.1 → 0.2). The samples keep their acquisition-time alignment (the zoom captures above
showed a 0.2 s offset from exactly this), so treat absolute time as reliable only if the
horizontal controls weren't touched between stop and save.

## Acquisition & display state — what reaches the file

A/B-verified on the SDS814X by re-saving controlled acquisitions: **[obs]**

- **Interpolation (x vs sinc):** nothing — the two saves were byte-identical. Stored
  samples are always raw, never interpolated.
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
  stored**, so inter-segment timing is not recoverable from a binary export. (The
  scope's History panel shows per-segment timestamps at µs resolution, and SCPI
  `:HISTORy:TIME?` reads them out — they just never reach the .bin.)
- **Reference (REF) traces:** displaying one changes nothing — the memory_* fields
  stayed zero. REFs export only to the separate `.ref` format (not parsed here).
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
models), digital (D0–D15) data, reference/memory waveforms, Average/ERES traces (math
operators on this model, so expected to save as ordinary F-trace files), and a real
current-clamp probe (the amps case here was a voltage source in amps-display mode). The 8-bit,
big-endian and CH5–CH8 code paths are locked by synthetic derivations of real captures
in the test suite — not the same as real files. Corrections/captures from other SDS
models welcome.
