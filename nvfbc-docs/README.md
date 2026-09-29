# x11vnc-nvfbc documentation

Notes for a fork of [x11vnc](https://github.com/LibVNC/x11vnc) that captures
the screen with NVIDIA's NVFBC instead of `XShmGetImage`.

| document | what it covers |
|---|---|
| [NVFBC-BUILD-AND-USAGE.md](NVFBC-BUILD-AND-USAGE.md) | building, options, capture-region selection, how the capture path works, troubleshooting. **Start here.** |
| [x11vnc-nvfbc-requirements.md](x11vnc-nvfbc-requirements.md) | the original planning document: goals, hardware/software requirements, references to the Sunshine implementation. Partly historical - see the note at the top about the test system. |
| [deploy/](deploy/) | the wrapper script actually running in production, kept byte-identical to the installed copy, plus why each flag is set and how to install a change. |
| [H264.md](H264.md) | H.264 output in plain terms: how the Tight/H.264 switching works, results, options, known limitations, and the findings that apply beyond this project. **Start here for H.264.** |
| [NVENC-H264-PLAN.md](NVENC-H264-PLAN.md) | the engineering log behind H264.md, kept for whoever changes the code next (including coding agents): every measurement, refuted hypothesis and code anchor, in order. Wrong turns and their corrections are kept deliberately. |
| [keyboard-issues-and-future-work.md](keyboard-issues-and-future-work.md) | why a non-English client locale breaks Shift+letter, what was tried and reverted, and the QEMU Extended Key Event fix that would solve it properly. |
| [AUDIO-STREAMING.md](AUDIO-STREAMING.md) | streaming audio alongside VNC, which the RFB protocol does not carry. |

## Related repositories

These are separate checkouts alongside this one, not submodules:

- `../x11vnc` - the fork itself, NVFBC work on branch `feature/nvfbc-capture`
- `../bench` - benchmark harness and recorded measurements

Performance claims here are backed by runs in `../bench/results/`; see
`../bench/results/NOTES.md` for what each run was. Do not quote fixed frame
rates - they depend on resolution, how much of the screen changes, the
encoding the client negotiates, and how many clients are attached.
