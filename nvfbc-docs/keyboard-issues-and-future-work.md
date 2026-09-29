# Keyboard Input — Known Issues and Future Work

## Current Workaround

When using TigerVNC from Windows with a non-English locale (e.g. Greek),
keep the Windows keyboard on English and switch locale on the **server**
using the server's keyboard shortcut (e.g. Alt+Shift, Super+Space).
This gives AnyDesk-like "remote keyboard" behavior.

## Problem: Client Locale Interferes with Server Keyboard

VNC protocol sends keysyms (logical characters) rather than scancodes
(physical key positions). When the Windows client has a non-English locale
active (e.g. Greek), TigerVNC sends locale-specific keysyms (e.g.
XK_Greek_ALPHA). x11vnc must reverse-map these to X11 keycodes, which
fails for Shift+letter combinations when the server is in a different
XKB group than the keysym expects.

AnyDesk does not have this problem because it sends scancodes — the
server's layout determines the output and the client's locale is
irrelevant.

## Attempted Fixes (Reverted)

### 1. XkbLockGroup — Temporary group switching per keystroke
Switched XKB group to match the keysym's group before sending the keycode,
then restored. Caused severe lag, prevented locale switching, and locked
the server into whichever locale was active at connect time.

### 2. XkbLockModifiers — Direct lock state manipulation
Used XkbLockModifiers() instead of XTestFakeKeyEvent for lock-type
modifier bits (Caps Lock, Num Lock). Intended to prevent lock state
corruption from toggle semantics. Also caused keyboard state issues
in practice.

### 3. add_keysyms fallback for non-current group
Discarded keysym matches in non-current XKB groups, letting add_keysym()
dynamically bind them to free keycodes. Corrupted the server keymap
causing mixed case output and unresponsive Caps Lock.

## Future Fix: QEMU Extended Key Events (Option A)

The proper solution is to implement QEMU Extended Key Event support
(RFB message type 255). This protocol extension sends XT scancodes
alongside keysyms. TigerVNC already sends these.

### Architecture

```
TigerVNC (Windows)                    x11vnc (Linux)
  Physical key press                    Receive rfbQemuExtendedKeyEventMsg
  -> XT scancode + keysym   ------>     -> Extract XT scancode
                                        -> Map XT scancode to X11 keycode
                                        -> XTestFakeKeyEvent(keycode)
                                        -> Server XKB layout determines output
```

### What is needed

1. **libvncserver gap**: The `kbdAddEvent` callback only passes
   `(down, keySym, client)` — it drops the scancode. Either:
   - Extend libvncserver to add a new callback that includes the keycode
   - Or intercept raw RFB messages in x11vnc before libvncserver processes them

2. **XT scancode to X11 keycode mapping**: Map the XT scancode from the
   QEMU event to the corresponding X11 keycode for XTestFakeKeyEvent.

3. **Fallback**: When the QEMU extension is not available (older clients),
   fall back to the existing keysym-based path.

### Protocol reference

Already defined in libvncserver (`/usr/include/rfb/rfbproto.h`):

```c
#define rfbQemuEvent 255
#define rfbEncodingQemuExtendedKeyEvent 0xFFFFFEFE

typedef struct {
    uint8_t type;      /* 255 */
    uint8_t subtype;   /* 0 */
    uint16_t down;
    uint32_t keysym;
    uint32_t keycode;  /* XT scancode */
} rfbQemuExtendedKeyEventMsg;
```

### Files to modify

- `src/keyboard.c` — Add scancode-based input path
- `src/keyboard.h` — New callback or handler declaration
- `src/screen.c` — Register extended key event handler
- Possibly libvncserver itself or use raw message interception
