# Notes for coding agents

Read these before changing the NVFBC or H.264 code:

- [`nvfbc-docs/NVENC-H264-PLAN.md`](nvfbc-docs/NVENC-H264-PLAN.md): the engineering log
  for the H.264 path. Every design decision, measurement, refuted hypothesis
  and code anchor, in order. Later sections correct earlier ones; read to the
  end before relying on a section.
- [`nvfbc-docs/NVFBC-BUILD-AND-USAGE.md`](nvfbc-docs/NVFBC-BUILD-AND-USAGE.md): how the
  capture path works.
- [`nvfbc-docs/bench/README.md`](nvfbc-docs/bench/README.md) and
  [`nvfbc-docs/bench/results/NOTES.md`](nvfbc-docs/bench/results/NOTES.md): how to measure a change,
  and what each recorded run was.

Rules that past mistakes established:

- **Measure; don't estimate.** Several CPU attributions in the log were wrong
  by up to 10x until measured per thread (`nvfbc-docs/bench/threadcpu.py`).
- **`new_fps` in the NVFBC stats is not the delivered frame rate.** Use
  `nvfbc-docs/bench/ab.sh` for delivery comparisons.
- **Compare CPU only against runs on the same hardware.** Figures up to plan
  §27 are from an i7-3770; later ones are from a Ryzen 9 9900X.
- **Test H.264 changes against the real client**, not only ffmpeg. The
  Windows viewer's decoder is strict and fails silently. `nvfbc-docs/bench/h264serve.py`
  isolates the client from the server.
- **Never start a test x11vnc without `-repeat`.** Otherwise it turns off the
  X server's key auto-repeat globally and only restores it on a clean exit.
- **x11vnc remote control (`-R`, `-Q`) reaches whichever server is on the
  display**; it can't target one of several.
- **Upstream merges must stay easy**: keep additions in `src/nvfbc/`,
  `src/h264/` and small hooks elsewhere, and don't reorganise upstream files.
