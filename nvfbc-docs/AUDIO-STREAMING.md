# Audio Streaming for VNC

## Overview

The VNC protocol does not support audio transfer. This document describes methods to stream audio separately from a Linux server to a Windows client alongside your VNC session.

## Methods

### Option 1: RTP Stream with VLC (Easiest Setup)

**Latency:** ~1-2 seconds

**Linux server (PulseAudio):**
```bash
# Multicast (auto-discovery)
pactl load-module module-rtp-send source=@DEFAULT_MONITOR@

# Or unicast to specific Windows IP
pactl load-module module-rtp-send source=@DEFAULT_MONITOR@ destination=192.168.1.100
```

**Windows client:**
1. Open VLC
2. Media → Open Network Stream
3. Enter: `rtp://@:46998` (default port) or `rtp://224.0.0.56:46998` (multicast)

**To stop streaming:**
```bash
pactl unload-module module-rtp-send
```

---

### Option 2: FFmpeg Stream (Lower Latency)

**Latency:** ~200-500ms

**Linux server:**
```bash
# Stream system audio using Opus codec
ffmpeg -f pulse -i default -acodec libopus -f rtp rtp://192.168.1.100:5004

# Alternative with MP3 (more compatible)
ffmpeg -f pulse -i default -acodec mp3 -f rtp rtp://192.168.1.100:5004
```

**Windows client (using ffplay):**
```cmd
ffplay -nodisp -fflags nobuffer -flags low_delay rtp://0.0.0.0:5004
```

Or use VLC with the same RTP URL.

---

### Option 3: Scream (Lowest Latency - Recommended)

**Latency:** ~20-30ms

[Scream](https://github.com/duncanthrax/scream) is a virtual audio device designed for low-latency network audio streaming.

**Linux server setup:**

1. Install scream sender:
```bash
# Build from source
git clone https://github.com/duncanthrax/scream.git
cd scream/Receivers/unix
mkdir build && cd build
cmake ..
make
```

2. Run the sender:
```bash
# Multicast (recommended)
scream-pulse -i eth0

# Or unicast to specific IP
scream-pulse -i eth0 -t 192.168.1.100
```

**Windows client:**
1. Download the latest release from [Scream releases](https://github.com/duncanthrax/scream/releases)
2. Run `scream-receiver.exe`
3. Audio will play through your default Windows audio device

---

## Comparison

| Method | Latency | Setup Complexity | Quality | Notes |
|--------|---------|------------------|---------|-------|
| RTP + VLC | ~1-2s | Easy | Good | High latency, but simple |
| FFmpeg + ffplay | ~200-500ms | Medium | Good | Requires ffmpeg on both ends |
| Scream | ~20-30ms | Medium | Excellent | Best for video sync |

## Recommendation

For use with x11vnc-nvfbc where low latency matters (gaming, video playback), **Scream** is recommended as it provides the lowest latency and best synchronization with the video stream.

## Systemd Service for Audio Streaming

To run audio streaming as a service alongside x11vnc:

### Scream Service

Create `/etc/systemd/system/scream-audio.service`:
```ini
[Unit]
Description=Scream Audio Streaming
After=pulseaudio.service

[Service]
Type=simple
ExecStart=/usr/local/bin/scream-pulse -i eth0
Restart=always
RestartSec=5
User=your-username
Environment=PULSE_SERVER=unix:/run/user/1000/pulse/native

[Install]
WantedBy=multi-user.target
```

### RTP Audio Service

Create `/etc/systemd/system/rtp-audio.service`:
```ini
[Unit]
Description=PulseAudio RTP Streaming
After=pulseaudio.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/pactl load-module module-rtp-send source=@DEFAULT_MONITOR@
ExecStop=/usr/bin/pactl unload-module module-rtp-send
User=your-username
Environment=PULSE_SERVER=unix:/run/user/1000/pulse/native

[Install]
WantedBy=multi-user.target
```

Enable with:
```bash
sudo systemctl daemon-reload
sudo systemctl enable scream-audio  # or rtp-audio
sudo systemctl start scream-audio
```

## Firewall Configuration

Ensure the required ports are open:

```bash
# For RTP (PulseAudio default)
sudo ufw allow 46998/udp

# For FFmpeg RTP
sudo ufw allow 5004/udp

# For Scream (default multicast)
sudo ufw allow 4010/udp
```

## Troubleshooting

### No audio on Windows

1. Verify the Linux audio is working locally
2. Check firewall settings on both ends
3. Ensure the correct network interface is used
4. For Scream, verify the receiver is running before the sender

### Audio crackling or dropouts

- Check network stability
- For Scream, try unicast instead of multicast
- Increase buffer sizes if available

### PulseAudio module fails to load

```bash
# List available sources
pactl list sources short

# Use specific source name instead of @DEFAULT_MONITOR@
pactl load-module module-rtp-send source=alsa_output.pci-0000_01_00.1.hdmi-stereo.monitor
```
