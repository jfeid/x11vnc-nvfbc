# x11vnc-nvfbc: Development Environment Setup

## Project Overview

**Goal:** Create a custom fork of x11vnc that uses NVIDIA's NVFBC (Frame Buffer Capture) API instead of XShmGetImage for significantly faster screen capture on NVIDIA GPUs.

**Expected Performance Gain:** ~2-4x faster capture for typical workloads (capture phase improvement from ~50-150ms down to ~1-2ms).

**Target Platform:** Linux x86_64 with X11 and NVIDIA GPU (GeForce GTX 600+, Quadro, Tesla, GRID)

**Compatibility:** Must maintain compatibility with standard VNC clients (TigerVNC, RealVNC, etc.)

**Repository:** Public fork on GitHub (to be created from https://github.com/LibVNC/x11vnc)

---

## Development/Test System

> **Note:** this section records the *original* planning system. The machine
> now used is an **NVIDIA GeForce RTX 3060 (Ampere)** driving a 4480x1440 X
> screen across two outputs, driver 550.163.01. Current measured numbers live
> in `bench/results/` - see `bench/README.md`.

| Component | Value |
|-----------|-------|
| **OS** | Debian 13 (Trixie) |
| **Kernel** | 6.12.57+deb13-amd64 |
| **GPU** | NVIDIA GeForce GTX 1050 Ti (Pascal, GP107) |
| **GPU UUID** | GPU-16399588-f8da-6a7d-53e1-15e0b16e8c14 |
| **Driver** | 550.163.01 |
| **Architecture** | x86_64 |

### GPU Notes
- GTX 1050 Ti is **Pascal architecture** (6th gen NVENC)
- CUDA Compute Capability: **6.1**
- NVFBC: Supported with driver patch OR Sunshine's patch-free method
- 4GB VRAM - sufficient for capture operations

---

## Reference Implementation

### Sunshine (LizardByte)
- **Repository:** https://github.com/LizardByte/Sunshine
- **License:** GPL-3.0
- **Relevance:** Production-quality NVFBC implementation that recently eliminated the need for driver patching

### Key Source Files to Study

```
Sunshine/
├── third-party/nvfbc/
│   └── NvFBC.h                    # NVFBC API header (copy this)
├── src/platform/linux/
│   ├── cuda.cpp                   # CUDA initialization
│   ├── cuda.h
│   ├── nvfbc.cpp                  # NVFBC capture implementation ← PRIMARY REFERENCE
│   └── x11grab.cpp                # X11 capture (fallback, compare with x11vnc)
├── src/
│   ├── video.cpp                  # Frame handling pipeline
│   └── platform/common.h          # Platform abstraction
└── CMakeLists.txt                 # Build configuration for CUDA/NVFBC
```

### Key NVFBC Functions (from Sunshine)

```c
// Session management
NvFBCCreateInstance()              // Load NVFBC library
NvFBCCreateHandle()                // Create capture handle
NvFBCCreateCaptureSession()        // Initialize capture session
NvFBCDestroyCaptureSession()       // Cleanup

// Capture methods (choose one)
NvFBCToSysGrabFrame()              // Capture to system RAM (~1.8ms)
NvFBCToCudaGrabFrame()             // Capture to CUDA memory (~50μs, requires CUDA)
NvFBCToGLGrabFrame()               // Capture to OpenGL texture
```

---

## Hardware Requirements

| Component | Minimum | Test System |
|-----------|---------|-------------|
| GPU | GeForce GTX 600 (Kepler) | **GTX 1050 Ti (Pascal)** ✓ |
| VRAM | 2GB | **4GB** ✓ |
| Driver | 418.x+ | **550.163.01** ✓ |
| Display | X11 session active | X11 on NVIDIA GPU |

### GPU Compatibility Notes

- **GRID/Tesla/Quadro X2000+**: NVFBC works natively
- **GeForce**: Requires driver patch OR use Sunshine's patch-free method
- **Wayland**: NOT SUPPORTED (NVFBC is X11 only)
- **Optimus laptops**: May have issues if display is on Intel iGPU

---

## Software Requirements

### Operating System
- **Debian 13 (Trixie)** ← Test system
- Ubuntu 22.04 LTS or newer
- Fedora 38+
- Arch Linux (rolling)

### Required Packages (Debian 13 Trixie)

```bash
# Debian 13 (Trixie) - Primary development system
sudo apt update
sudo apt install -y \
    build-essential \
    cmake \
    ninja-build \
    git \
    pkg-config \
    autoconf \
    automake \
    libtool \
    libx11-dev \
    libxext-dev \
    libxtst-dev \
    libxinerama-dev \
    libxdamage-dev \
    libxfixes-dev \
    libxrandr-dev \
    libxcursor-dev \
    libssl-dev \
    libjpeg-dev \
    libpng-dev \
    zlib1g-dev \
    libvncserver-dev \
    libavcodec-dev \
    libavutil-dev \
    libswscale-dev
```

### NVIDIA Driver

**Current:** 550.163.01 (already installed on test system)

The 550.x driver series is well-supported for NVFBC. No driver change needed.

```bash
# Verify driver (should show 550.163.01)
nvidia-smi --query-gpu=driver_version --format=csv,noheader
```

### CUDA Toolkit (Optional but recommended)

CUDA enables `NvFBCToCudaGrabFrame()` which is ~36x faster than `NvFBCToSysGrabFrame()`.

**GTX 1050 Ti Compute Capability:** 6.1 (Pascal)

```bash
# Debian 13 (Trixie)
sudo apt install nvidia-cuda-toolkit

# Verify CUDA
nvcc --version
nvidia-smi --query-gpu=compute_cap --format=csv,noheader
# Expected: 6.1
```

**Note:** CUDA 12.x requires compute capability 5.0+. GTX 1050 Ti (6.1) is fully supported.

---

## Source Code Setup

### 1. Fork x11vnc on GitHub

```bash
# 1. Go to https://github.com/LibVNC/x11vnc
# 2. Click "Fork" button
# 3. Create fork under your account (e.g., github.com/YOUR_USERNAME/x11vnc)
```

### 2. Clone Your Fork

```bash
mkdir -p ~/dev/x11vnc-nvfbc
cd ~/dev/x11vnc-nvfbc

# Clone YOUR fork (replace YOUR_USERNAME)
git clone git@github.com:YOUR_USERNAME/x11vnc.git
cd x11vnc

# Add upstream remote for syncing
git remote add upstream https://github.com/LibVNC/x11vnc.git

# Create feature branch
git checkout -b feature/nvfbc-capture

# Verify remotes
git remote -v
# origin    git@github.com:YOUR_USERNAME/x11vnc.git (fetch)
# origin    git@github.com:YOUR_USERNAME/x11vnc.git (push)
# upstream  https://github.com/LibVNC/x11vnc.git (fetch)
# upstream  https://github.com/LibVNC/x11vnc.git (push)
```

### 3. Clone Sunshine (Reference)

```bash
cd ~/dev/x11vnc-nvfbc
git clone --depth 1 https://github.com/LizardByte/Sunshine.git sunshine-reference
```

### 4. Copy NVFBC Header

```bash
mkdir -p ~/dev/x11vnc-nvfbc/x11vnc/src/nvfbc
cp ~/dev/x11vnc-nvfbc/sunshine-reference/third-party/nvfbc/NvFBC.h \
   ~/dev/x11vnc-nvfbc/x11vnc/src/nvfbc/
```

### 5. Git Workflow

```bash
# Regular development cycle
git add -A
git commit -m "feat(nvfbc): implement NVFBC capture backend"
git push origin feature/nvfbc-capture

# Sync with upstream periodically
git fetch upstream
git rebase upstream/master

# Create PR when ready via GitHub web UI
```

---

## Project Structure (Proposed)

```
x11vnc/
├── src/
│   ├── nvfbc/
│   │   ├── NvFBC.h              # NVIDIA header (from Sunshine)
│   │   ├── nvfbc_capture.c      # NEW: NVFBC capture implementation
│   │   └── nvfbc_capture.h      # NEW: Public interface
│   ├── x11vnc.c                 # MODIFY: Add NVFBC backend selection
│   └── screen.c                 # MODIFY: Abstract capture interface
├── configure.ac                 # MODIFY: Add NVFBC/CUDA detection
├── Makefile.am                  # MODIFY: Add nvfbc sources
└── README.nvfbc.md              # NEW: NVFBC-specific documentation
```

---

## Implementation Tasks

### Phase 1: Environment Setup
- [ ] Install all dependencies
- [ ] Verify NVIDIA driver installation (`nvidia-smi`)
- [ ] Verify X11 session on NVIDIA GPU (`xrandr --listproviders`)
- [ ] Build original x11vnc successfully
- [ ] Study Sunshine's NVFBC implementation

### Phase 2: NVFBC Integration
- [ ] Create capture abstraction layer in x11vnc
- [ ] Implement NVFBC initialization (load `libnvidia-fbc.so`)
- [ ] Implement `NvFBCToSysGrabFrame()` capture path
- [ ] Add runtime fallback to XShmGetImage if NVFBC fails
- [ ] Add command-line option: `--nvfbc` / `--no-nvfbc`

### Phase 3: Optimization (Optional)
- [ ] Implement CUDA capture path (`NvFBCToCudaGrabFrame()`)
- [ ] Add difference map support for partial updates
- [ ] Profile and benchmark against original x11vnc

### Phase 4: Testing
- [ ] Test with TigerVNC viewer
- [ ] Test with RealVNC viewer
- [ ] Test multi-monitor setups
- [ ] Test resolution changes
- [ ] Benchmark capture performance

---

## Key Code Snippets (From Sunshine)

### NVFBC Library Loading

```c
// Reference: Sunshine/src/platform/linux/nvfbc.cpp

#include <dlfcn.h>
#include "NvFBC.h"

static void *nvfbc_lib = NULL;
static NVFBC_API_FUNCTION_LIST nvfbc = {0};

int nvfbc_init(void) {
    nvfbc_lib = dlopen("libnvidia-fbc.so.1", RTLD_NOW);
    if (!nvfbc_lib) {
        // Mutantform using nvidia-patch method
        nvfbc_lib = dlopen("libnvidia-fbc.so", RTLD_NOW);
    }
    if (!nvfbc_lib) {
        return -1;  // NVFBC not available
    }

    // Get function pointers
    PNVFBCCREATEINSTANCE NvFBCCreateInstance = 
        (PNVFBCCREATEINSTANCE)dlsym(nvfbc_lib, "NvFBCCreateInstance");
    
    if (!NvFBCCreateInstance) {
        dlclose(nvfbc_lib);
        return -1;
    }

    nvfbc.dwVersion = NVFBC_VERSION;
    NVFBCSTATUS status = NvFBCCreateInstance(&nvfbc);
    
    return (status == NVFBC_SUCCESS) ? 0 : -1;
}
```

### Creating Capture Session

```c
// Reference: Sunshine/src/platform/linux/nvfbc.cpp

static NVFBC_SESSION_HANDLE session_handle = 0;

int nvfbc_create_session(int display_id) {
    NVFBC_CREATE_HANDLE_PARAMS create_params = {0};
    create_params.dwVersion = NVFBC_CREATE_HANDLE_PARAMS_VER;
    
    // For GeForce cards without driver patch (Sunshine's method)
    // This magic enables NVFBC on consumer GPUs
    create_params.privateData = /* see Sunshine source */;
    create_params.privateDataSize = /* see Sunshine source */;
    
    NVFBCSTATUS status = nvfbc.nvFBCCreateHandle(&session_handle, &create_params);
    if (status != NVFBC_SUCCESS) {
        return -1;
    }
    
    // Create capture session
    NVFBC_CREATE_CAPTURE_SESSION_PARAMS session_params = {0};
    session_params.dwVersion = NVFBC_CREATE_CAPTURE_SESSION_PARAMS_VER;
    session_params.eCaptureType = NVFBC_CAPTURE_TO_SYS;  // or NVFBC_CAPTURE_SHARED_CUDA
    session_params.eTrackingType = NVFBC_TRACKING_OUTPUT;
    session_params.dwOutputId = display_id;
    session_params.frameSize = /* set based on resolution */;
    
    status = nvfbc.nvFBCCreateCaptureSession(session_handle, &session_params);
    return (status == NVFBC_SUCCESS) ? 0 : -1;
}
```

### Frame Capture

```c
// Reference: Sunshine/src/platform/linux/nvfbc.cpp

int nvfbc_grab_frame(uint8_t **buffer, int *width, int *height) {
    NVFBC_TOSYS_GRAB_FRAME_PARAMS grab_params = {0};
    grab_params.dwVersion = NVFBC_TOSYS_GRAB_FRAME_PARAMS_VER;
    grab_params.dwFlags = NVFBC_TOSYS_GRAB_FLAGS_NOWAIT;
    
    NVFBC_FRAME_GRAB_INFO frame_info = {0};
    grab_params.pFrameGrabInfo = &frame_info;
    
    NVFBCSTATUS status = nvfbc.nvFBCToSysGrabFrame(session_handle, &grab_params);
    
    if (status == NVFBC_SUCCESS) {
        *buffer = /* pointer to captured data */;
        *width = frame_info.dwWidth;
        *height = frame_info.dwHeight;
        return 0;
    }
    
    return -1;
}
```

---

## Build Commands

### Configure with NVFBC Support

```bash
cd ~/dev/x11vnc-nvfbc/x11vnc

# Generate configure if needed
autoreconf -fiv

# Configure with NVFBC
./configure \
    --prefix=/usr/local \
    --with-nvfbc \
    CFLAGS="-O2 -I/usr/local/cuda/include" \
    LDFLAGS="-L/usr/local/cuda/lib64"

# Build
make -j$(nproc)

# Install (optional)
sudo make install
```

### CMake Alternative (If Refactoring Build)

```cmake
# CMakeLists.txt snippet
option(ENABLE_NVFBC "Enable NVFBC capture support" ON)

if(ENABLE_NVFBC)
    find_library(NVFBC_LIBRARY nvidia-fbc PATHS /usr/lib/x86_64-linux-gnu)
    if(NVFBC_LIBRARY)
        add_definitions(-DHAVE_NVFBC)
        target_link_libraries(x11vnc ${NVFBC_LIBRARY} dl)
    endif()
endif()

# Optional CUDA support
find_package(CUDA)
if(CUDA_FOUND AND ENABLE_NVFBC)
    add_definitions(-DHAVE_CUDA)
    target_link_libraries(x11vnc ${CUDA_LIBRARIES})
endif()
```

---

## Testing & Verification

### Verify System (Test System: station1)

```bash
# GPU info (expected: GTX 1050 Ti)
nvidia-smi -L
# GPU 0: NVIDIA GeForce GTX 1050 Ti (UUID: GPU-16399588-f8da-6a7d-53e1-15e0b16e8c14)

# Driver version (expected: 550.163.01)
nvidia-smi --query-gpu=driver_version --format=csv,noheader
# 550.163.01

# Check if NVFBC library exists
ls -la /usr/lib/x86_64-linux-gnu/libnvidia-fbc.so*

# Check X11 is running on NVIDIA
xrandr --listproviders
# Should show "NVIDIA-0" or similar

# CUDA compute capability (GTX 1050 Ti = 6.1)
nvidia-smi --query-gpu=compute_cap --format=csv,noheader
```

### Run x11vnc with NVFBC

```bash
# Start with NVFBC capture (proposed syntax)
x11vnc -display :0 -nvfbc -forever -shared

# Verbose mode for debugging
x11vnc -display :0 -nvfbc -debug_capture
```

### Benchmark Capture Performance

```bash
# Capture timing test (proposed)
x11vnc -display :0 -nvfbc -benchmark -q

# Compare with standard capture
x11vnc -display :0 -no-nvfbc -benchmark -q
```

---

## Troubleshooting

### NVFBC Returns NVFBC_ERROR_UNSUPPORTED_PLATFORM
- GeForce card requires driver patch OR Sunshine's patch-free method
- Ensure X11 display is on NVIDIA GPU (not Intel iGPU)

### NVFBC Returns NVFBC_ERR_X
- X11 session not running or not accessible
- Check `DISPLAY` environment variable
- Try running with `sudo` for debugging

### Poor Performance
- Ensure using `NVFBC_TOSYS_GRAB_FLAGS_NOWAIT` flag
- Consider CUDA path for better performance
- Check if GPU is power-throttled (`nvidia-smi -q -d PERFORMANCE`)

### Build Errors
- Missing NVFBC header: Copy from Sunshine's `third-party/nvfbc/`
- Missing CUDA: Install `nvidia-cuda-toolkit` or build without CUDA support

---

## References

1. **Sunshine Source Code**: https://github.com/LizardByte/Sunshine
2. **x11vnc Source Code**: https://github.com/LibVNC/x11vnc
3. **NVIDIA Capture SDK Documentation**: https://developer.nvidia.com/capture-sdk
4. **NVIDIA Driver Patch (for GeForce)**: https://github.com/keylase/nvidia-patch
5. **NVFBC API Reference**: (In Sunshine's `third-party/nvfbc/NvFBC.h`)

---

## License Considerations

- x11vnc: GPL-2.0+
- Sunshine: GPL-3.0
- NVFBC Header: NVIDIA proprietary (redistribution may require SDK agreement)

Ensure final project licensing is compatible. The NVFBC header from Sunshine can be used as reference since Sunshine is already GPL-licensed and distributes it.

---

## Version History

| Version | Date | Notes |
|---------|------|-------|
| 1.1 | 2026-01-09 | Added test system specs (Debian 13, GTX 1050 Ti, driver 550.163.01), GitHub fork workflow |
| 1.0 | 2026-01-09 | Initial requirements document |

---

## Notes on Driver 550.x and NVFBC

The test system runs driver **550.163.01**. Important considerations:

1. **NVFBC Patch Status**: Check if keylase/nvidia-patch supports 550.163.01
   ```bash
   cd /tmp
   git clone https://github.com/keylase/nvidia-patch.git
   ./nvidia-patch/patch-fbc.sh -c 550.163.01
   ```

2. **Sunshine's Patch-Free Method**: As of 2024, Sunshine eliminated the need for driver patching. Their implementation uses "magic" private data in the NVFBC create params. Study:
   - `sunshine-reference/src/platform/linux/nvfbc.cpp`
   - PR #2471: "Remove the need for a patched nvidia library for NvFBC"

3. **Driver 550.x Known Issues**: Some driver versions between 560.x-565.x had NVFBC issues. Driver 550.x should work. If issues arise, consider:
   - Driver 555.58.02 (known good)
   - Driver 570.86.15+ (newer stable)
