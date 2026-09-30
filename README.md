# x11vnc-nvfbc

A fork of [x11vnc](https://github.com/LibVNC/x11vnc) for Linux desktops
running on an NVIDIA GPU. It adds two things, and each can be used without the
other:

- **NVFBC capture.** Reads the screen straight from the GPU instead of asking
  the X server for it, and uses the GPU's own map of what changed. It works on
  ordinary GeForce cards without patching the driver.
- **H.264 while the screen moves.** When you scroll, drag a window or play a
  video, the server switches to an H.264 video stream encoded on the GPU
  (NVENC), and switches back to normal sharp-text encoding when things settle.
  Clients see it as RFB encoding 50, which TigerVNC supports.

The result is a VNC server that stays usable over an ordinary internet
connection while the screen is busy, without taking over a CPU core.

## What it changes, in numbers

2560x1440 desktop, one TigerVNC client, RTX 3060:

| | before | now |
|---|---|---|
| network while the whole screen moves | ~26 MB/s (Tight, x11vnc's usual encoding) | ~0.4 MB/s (H.264) |
| CPU, moderate motion (960x540 area at 60 fps), i7-3770 | 78% (first NVFBC version) | 33% |
| same, Ryzen 9 9900X | | 9% |
| NVFBC grabs per captured frame | 93-159 (first NVFBC version) | 1-2 |

100% CPU is one core. NVFBC on its own buys frame rate rather than CPU: against
x11vnc's standard X11 capture it delivers 9-36% more frames, at a higher CPU
cost per frame. Details, and the raw data behind every number, are in
[`nvfbc-docs/H264.md`](nvfbc-docs/H264.md) and [`nvfbc-docs/bench/results/NOTES.md`](nvfbc-docs/bench/results/NOTES.md).

## Requirements

- Linux with **X11**. Wayland isn't supported.
- An **NVIDIA GPU driving the display** you want to share, driver 450 or
  newer, and `libnvidia-fbc.so` (on Debian/Ubuntu: `libnvidia-fbc1`).
- For H.264: FFmpeg's libavcodec built with `h264_nvenc` (the distribution
  packages are), and a VNC viewer that supports encoding 50. Tested with
  **TigerVNC 1.16.2 on Windows**.

If NVFBC isn't available at runtime, x11vnc falls back to normal X11 capture.

## Build

```bash
# Debian/Ubuntu build dependencies
sudo apt-get install build-essential autoconf automake libtool pkg-config \
    libx11-dev libxext-dev libxfixes-dev libxdamage-dev libxrandr-dev \
    libxinerama-dev libxtst-dev libxkbfile-dev libssl-dev libjpeg-dev \
    libpng-dev libvncserver-dev \
    libavcodec-dev libavutil-dev libswscale-dev   # only for H.264

autoreconf -fiv
./configure            # --without-nvfbc / --without-ffmpeg to leave either out
make -j"$(nproc)"
```

The binary is `src/x11vnc`.

## Run

```bash
x11vnc -display :0 -forever -shared -rfbauth ~/.vnc/passwd \
       -nvfbc -nvfbc_nocursor -nvfbc_push \
       -h264 -h264_enter 8
```

- `-rfbauth` sets the password file; create it once with
  `x11vnc -storepasswd ~/.vnc/passwd`.
- `-nvfbc` turns on GPU capture. The log line
  `NVFBC: Capture initialized successfully` confirms it engaged.
- `-h264` turns on H.264 output. `-h264_enter 8` keeps small moving areas,
  such as a video in a corner, on normal encoding.
- `-clip WxH+X+Y` limits sharing to one monitor. If the area matches a
  monitor exactly, NVFBC captures only that monitor.

In the **TigerVNC viewer**, open Options and choose **H.264** as the
preferred encoding. The server only uses H.264 for viewers that prefer it.

All options: [`nvfbc-docs/NVFBC-BUILD-AND-USAGE.md`](nvfbc-docs/NVFBC-BUILD-AND-USAGE.md)
for capture, [`nvfbc-docs/H264.md`](nvfbc-docs/H264.md) for H.264. An example systemd
setup is in [`nvfbc-docs/deploy/`](nvfbc-docs/deploy/).

## Known limitations

- **Text is a little soft while the screen is moving.** H.264 as TigerVNC
  decodes it keeps colour at half resolution. When motion stops, the server
  repaints with full-quality encoding, so text you're reading is sharp.
- **TigerVNC's Windows viewer can't show H.264 areas larger than 2048x1152.**
  It shows nothing and reports no error. The server works around this by
  splitting the screen into bands. A fix is pending in TigerVNC
  ([#2153](https://github.com/TigerVNC/tigervnc/pull/2153)); its test build displays 2560x1440 from this server.
- **Only tested with one viewer and one machine** (TigerVNC on Windows, an
  RTX 3060). Reports from other setups are welcome, but support is best
  effort.

## Repository layout

| path | contents |
|---|---|
| `src/` | x11vnc source. The additions are in `src/nvfbc/` and `src/h264/`, plus hooks in `src/xwrappers.c`, `src/scan.c`, `src/remote.c`, `src/x11vnc.c` and a few smaller files |
| `nvfbc-docs/` | documentation for this fork: [`H264.md`](nvfbc-docs/H264.md), [`NVFBC-BUILD-AND-USAGE.md`](nvfbc-docs/NVFBC-BUILD-AND-USAGE.md), deployment example |
| `nvfbc-docs/bench/` | the benchmark tools and recorded results used to measure every change |
| `doc/`, `README` | upstream x11vnc's own documentation, unchanged |

## Credits

- [x11vnc](https://github.com/LibVNC/x11vnc), the server this is built on.
- [Sunshine](https://github.com/LizardByte/Sunshine), the reference for
  using NVFBC on GeForce cards.
- [nvidia-patch](https://github.com/keylase/nvidia-patch), who discovered the
  method.

## License

GPL-2.0-or-later, the same as x11vnc. See [`COPYING`](COPYING).
`src/nvfbc/NvFBC.h` is NVIDIA's header, under the MIT licence.
