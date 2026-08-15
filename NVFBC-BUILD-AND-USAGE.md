# NVFBC Support for x11vnc

## Overview

NVFBC (NVIDIA Frame Buffer Capture) is a high-performance screen capture API that captures frames directly from the GPU framebuffer. This provides significantly faster screen capture compared to traditional X11 methods (XShmGetImage), enabling smooth VNC streaming at 50+ FPS even during video playback.

This fork of x11vnc adds NVFBC support with a patch-free method that works on consumer GeForce GPUs (not just Quadro/Tesla).

## Requirements

### Hardware
- NVIDIA GPU (GeForce, Quadro, or Tesla)
- GPU must be driving the display you want to capture

### Software
- NVIDIA driver version 450+ (tested with 550.163.01)
- `libnvidia-fbc.so` library (included in nvidia driver packages)
- X11 display server (Wayland is not supported)
- Standard build tools (gcc, make, autoconf, automake, libtool)

### Dependencies (Debian/Ubuntu)
```bash
# Build dependencies
sudo apt-get install build-essential autoconf automake libtool pkg-config

# x11vnc dependencies
sudo apt-get install libx11-dev libxext-dev libxfixes-dev libxdamage-dev \
    libxrandr-dev libxinerama-dev libxtst-dev libxkbfile-dev \
    libssl-dev libjpeg-dev libpng-dev libvncserver-dev

# NVFBC library (if not already installed)
sudo apt-get install libnvidia-fbc1
```

## Building

### Clone the Repository
```bash
git clone https://github.com/jfeid/x11vnc.git
cd x11vnc
git checkout feature/nvfbc-capture
```

### Build with NVFBC Support
```bash
autoreconf -fiv
./configure
make -j$(nproc)
```

NVFBC support is automatically enabled if `libdl` (dlopen) is available. The configure script will show:
```
NVFBC support: yes
```

### Verify NVFBC Support
```bash
# Check binary has NVFBC compiled in
strings src/x11vnc | grep -i nvfbc
```

### Install
```bash
sudo make install
# Or manually copy the binary:
sudo cp src/x11vnc /usr/bin/x11vnc
```

## Usage

### Basic Usage
```bash
x11vnc -nvfbc -display :0
```

### Command-Line Options

| Option | Description |
|--------|-------------|
| `-nvfbc` | Enable NVFBC capture (required to activate) |
| `-nonvfbc` | Disable NVFBC capture (use traditional X11 capture) |
| `-nvfbc_cursor` | Include cursor in NVFBC capture (default) |
| `-nvfbc_nocursor` | Exclude cursor from NVFBC capture |
| `-nvfbc_diffmap` | Use the GPU differential map to find changed tiles (default) |
| `-nvfbc_nodiffmap` | Fall back to x11vnc's scanline sampling to find changes |
| `-nvfbc_push` | Generate frames on damage instead of sampling at ~60Hz |
| `-nvfbc_nopush` | Sample at `dwSamplingRateMs` (default) |
| `-nvfbc_direct` | Allow NVFBC to attach directly to a fullscreen app, bypassing the X server. Implies `-nvfbc_push` and `-nvfbc_nocursor` |
| `-nvfbc_nodirect` | Disable direct capture (default) |

`-nvfbc_push` has been measured and is enabled in production: ~10ms lower
median latency and 9-13% more frames at small and medium change areas, at
slightly lower CPU, and neutral at fullscreen under video-like repaints. The one
regression found needs a large area repainted via thousands of small draw calls.
`-nvfbc_direct` is **not** recommended - it matches push on median latency while
adding a ~165ms tail. Full numbers: `bench/results/push-direct.md`.

### Capture region

The capture is automatically narrowed to the region actually served:

- if that region is exactly one connected output, NVFBC tracks that output;
- otherwise the screen is cropped to it (`captureBox` **plus** `frameSize` -
  the driver silently ignores `captureBox` on its own);
- otherwise the full screen is captured.

On a 4480x1440 dual-head screen served with `-clip 2560x1440+0+0`, that is the
difference between a 24.6 MB and a 14.1 MB transfer per frame (5.0 ms vs
2.8 ms). The chosen region is logged at startup:

```
NVFBC: tracking output 473 (DP-4) 2560x1440+0+0
```

`-id`/`-sid` without `-rootshift` captures a window rather than the screen,
which NVFBC cannot do; NVFBC disables itself in that case rather than serving
the wrong pixels.

### Recommended Options
```bash
x11vnc -nvfbc -nvfbc_nocursor -display :0 -rfbport 5900 -forever
```

Note: When using `-nvfbc_nocursor`, x11vnc will draw the cursor via X11 instead, which may be preferable for some use cases.

## Systemd Service Configuration

### Wrapper Script

The wrapper that runs in production is tracked at
[`deploy/x11vnc-wrapper.sh`](deploy/x11vnc-wrapper.sh), kept byte-identical to
the installed copy. [`deploy/README.md`](deploy/README.md) explains what it does,
why each flag is set, and how to install a change.

It loops over the available display rather than exiting, because the right one
changes over the machine's life: `:1` with the user's `Xauthority` once someone
is logged in, `:0` with gdm's before that.

**Important:** the wrapper exports `DISPLAY` and `XAUTHORITY` as well as passing
`-display`/`-auth`. NVFBC reads the environment directly, unlike x11vnc itself.

### Systemd Service
Create `/etc/systemd/system/x11vnc.service`:
```ini
[Unit]
Description=x11vnc VNC Server with NVFBC
After=display-manager.service

[Service]
Type=simple
ExecStart=/usr/local/bin/x11vnc-wrapper.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable x11vnc
sudo systemctl start x11vnc
```

## Performance

### Performance Logging
The NVFBC implementation logs statistics every 10 seconds:
```
NVFBC stats: 62.0 new fps, 108 grabs/sec, 620 new frames / 1076 total grabs
```

- **new fps:** unique frames captured
- **grabs/sec:** NVFBC grab calls issued
- **new frames / total grabs:** grabs spent per useful frame

**Grabs per frame is the number to watch.** It should sit near 1-2: one grab
per scan cycle, with cycles that find no new frame accounting for the rest.
Before the capture path was reworked this ratio was 93-159, i.e. the same frame
was re-grabbed a hundred-odd times while being scanned.

View stats:
```bash
tail -f /var/log/x11vnc.log | grep "NVFBC stats"
```

### Measuring

`bench/` holds a reproducible harness - a deterministic X11 load generator, a
sampler that records CPU and the server's own NVFBC stats, an NVFBC cost-floor
probe, and an RFB client that verifies delivered pixels. See `bench/README.md`.

```bash
cd bench
./measure.py --label before      # then rebuild/reinstall/restart
./measure.py --label after
./compare.py before after
```

Do not quote fixed FPS figures: achievable frame rate depends on resolution,
how much of the screen changes, the encoding the client negotiates, and how
many clients are attached. On the reference machine (RTX 3060, 2560x1440
served, one client) a 960x540 region changing at 60 Hz was sustained at 60 fps,
while driving the full 2560x1440 at 60 Hz saturated a core and fell to ~38 fps
- that case is limited by encoding, not capture.

## Troubleshooting

### "NVFBC library not found"
```
NVFBC: Failed to load libnvidia-fbc.so.1 or libnvidia-fbc.so
```

**Solution:** Install the NVFBC library:
```bash
sudo apt-get install libnvidia-fbc1
sudo ldconfig
```

### "Unable to open display"
```
NVFBC: Unable to open display
```

**Solution:** Ensure DISPLAY and XAUTHORITY environment variables are exported
before running x11vnc - see [`deploy/x11vnc-wrapper.sh`](deploy/x11vnc-wrapper.sh).

### "Capture not possible on this display"
This can occur if:
- The display is not rendered by an NVIDIA GPU
- You're running on a multi-GPU system and the display is on a different GPU
- Wayland is being used instead of X11

**Solution:** Verify you're using X11 on NVIDIA:
```bash
echo $XDG_SESSION_TYPE  # Should show "x11"
glxinfo | grep "OpenGL renderer"  # Should show NVIDIA GPU
```

### Slow keyboard/mouse input
If input becomes unresponsive, ensure you're using the latest version with non-blocking frame capture (timeout=0).

### NVFBC not initializing on GeForce
The implementation uses the patch-free method that should work on all GeForce GPUs with driver 450+. If it fails:
1. Check driver version: `nvidia-smi`
2. Ensure libnvidia-fbc.so is the correct version matching your driver

## Technical Details

### How It Works
1. NVFBC captures frames directly from the GPU framebuffer
2. Uses "magic" private data to enable capture on consumer GeForce GPUs (normally restricted to Quadro/Tesla)
3. Captures to system memory (NVFBC_CAPTURE_TO_SYS) - no CUDA dependency
4. Only the served region is captured (see above), so no bandwidth is spent on
   pixels that are never sent
5. **One grab per scan cycle.** `scan_for_updates()` takes a single frame up
   front, outside `X_LOCK`, and every `copy_image()` in that cycle is served
   from it. Grabbing inside `copy_image()` meant a fresh full-frame capture per
   scanline and per tile run - and because NVFBC hands back a single buffer it
   overwrites on the next grab, a scan could assemble `main_fb` from several
   different frames and tear.
6. **The differential map drives tile marking.** With
   `dwDiffMapScalingFactor` set to the tile size, one map cell corresponds to
   one x11vnc tile, so `tile_has_diff[]` is filled straight from the GPU's own
   dirty map and the scanline sampling (and its rescan passes) is skipped
   entirely.
7. If the grab reports no new frame, no pixel can have changed and the scan is
   skipped altogether.

The diff map is a delta against the last frame *captured*, not the last frame
generated, so consuming slower than the display generates is safe.
`bench/diffcheck.c` verifies this against an independent per-tile `memcmp`:
0 missed tiles across 1056 frames and ~965k changed tiles, down to a 10 fps
consumer against 60 fps generation. It does over-report by roughly 2x, which
only costs a redundant tile copy.

### Magic Private Data
The patch-free GeForce support uses private data discovered by the [nvidia-patch](https://github.com/keylase/nvidia-patch) project and implemented in [Sunshine](https://github.com/LizardByte/Sunshine):
```c
const unsigned int MAGIC_PRIVATE_DATA[4] = {
    0xAEF57AC5, 0x401D1A39, 0x1B856BBE, 0x9ED0CEBA
};
```

### Source Files
- `src/nvfbc/NvFBC.h` - NVIDIA NVFBC API header
- `src/nvfbc/nvfbc_capture.h` - Public interface
- `src/nvfbc/nvfbc_capture.c` - NVFBC implementation
- `src/xwrappers.c` - Integration with x11vnc capture pipeline

## License

This NVFBC implementation is released under GPL-2.0+, consistent with x11vnc licensing.

## Credits

- [x11vnc](https://github.com/LibVNC/x11vnc) - Original VNC server
- [Sunshine](https://github.com/LizardByte/Sunshine) - Reference NVFBC implementation
- [nvidia-patch](https://github.com/keylase/nvidia-patch) - Discovery of GeForce unlock method
