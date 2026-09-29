/* keytarget.c - keypress-driven repaint target for latency probing.
 *
 * Opens a small window on the live X display and repaints (incrementing a
 * counter) whenever the probe key arrives. Used with vncprobe.ps1 to measure
 * keypress -> screen-change latency without depending on the user's real
 * applications. Exits after -d seconds, on SIGTERM/SIGINT, or when the
 * window is destroyed.
 *
 * By default it passively grabs *only* the probe key (-k, default Page Down)
 * on the root window and does not take input focus, so the machine stays
 * usable while a run is in progress. -graball restores the original
 * behaviour (XGrabKeyboard + focus), which takes the whole keyboard away
 * from whoever is sitting at the desktop - including their ability to type
 * anywhere else - and makes Escape the only way out.
 *
 * Usage: ./keytarget [-g WxH+X+Y] [-d seconds] [-t title] [-k keysym] [-graball]
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <time.h>
#include <sys/select.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/keysym.h>

static volatile sig_atomic_t signalled = 0;

static void on_signal(int sig)
{
    (void)sig;
    signalled = 1;
}

static void usage(void)
{
    fprintf(stderr,
            "usage: keytarget [-g WxH+X+Y] [-d seconds] [-t title]\n"
            "                 [-k keysym] [-graball]\n"
            "  -g geometry   window geometry (default 400x300+64+64)\n"
            "  -d seconds    exit after this long (default 60)\n"
            "  -t title      window title\n"
            "  -k keysym     probe key to grab, e.g. 0xFF56 (default Page Down);\n"
            "                must match vncprobe.ps1 -Keysym\n"
            "  -graball      grab the entire keyboard and take focus (original\n"
            "                behaviour): the desktop becomes untypable until this\n"
            "                exits, and Escape is the only interactive way out\n");
    exit(2);
}

static void repaint(Display *dpy, Window win, int w, int h, long count)
{
    char buf[64];
    GC gc;
    XGCValues gcv;

    XClearWindow(dpy, win);
    gcv.foreground = BlackPixel(dpy, DefaultScreen(dpy));
    gcv.line_width = 2;
    gc = XCreateGC(dpy, win, GCForeground | GCLineWidth, &gcv);
    snprintf(buf, sizeof(buf), "keypresses: %ld", count);
    XDrawString(dpy, win, gc, 16, h / 2, buf, strlen(buf));
    XDrawRectangle(dpy, win, gc, 4, 4, w - 9, h - 9);
    XFreeGC(dpy, gc);
    XFlush(dpy);
}

int main(int argc, char **argv)
{
    const char *disp_name = NULL;
    const char *title = "x11vnc-bench-keytarget";
    int w = 400, h = 300, x = 64, y = 64;
    int duration = 60;
    int i;
    Display *dpy;
    Window win, root;
    XEvent ev;
    long count = 0;
    struct timespec t0, now;
    int ready_printed = 0;
    unsigned long probe_keysym = 0xFF56;   /* XK_Next, Page Down */
    int grab_all = 0;
    KeyCode probe_keycode = 0;

    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-g") && i + 1 < argc) {
            char g;
            if (sscanf(argv[++i], "%dx%d+%d+%d%c", &w, &h, &x, &y, &g) != 4 ||
                w < 16 || h < 16)
                usage();
        } else if (!strcmp(argv[i], "-d") && i + 1 < argc) {
            duration = atoi(argv[++i]);
            if (duration <= 0)
                usage();
        } else if (!strcmp(argv[i], "-t") && i + 1 < argc) {
            title = argv[++i];
        } else if (!strcmp(argv[i], "-k") && i + 1 < argc) {
            probe_keysym = strtoul(argv[++i], NULL, 0);
            if (!probe_keysym)
                usage();
        } else if (!strcmp(argv[i], "-graball")) {
            grab_all = 1;
        } else if (!strcmp(argv[i], "-display") && i + 1 < argc) {
            disp_name = argv[++i];
        } else
            usage();
    }

    dpy = XOpenDisplay(disp_name);
    if (!dpy) {
        fprintf(stderr, "keytarget: cannot open display %s\n",
                disp_name ? disp_name : "(default)");
        return 1;
    }
    root = DefaultRootWindow(dpy);

    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);

    /* override-redirect: mutter ignores XRaiseWindow on managed windows,
     * and a covered window's repaint never reaches the visible framebuffer.
     * An override-redirect window we raise ourselves stays on top. */
    {
        XSetWindowAttributes attrs;
        attrs.override_redirect = True;
        attrs.background_pixel = WhitePixel(dpy, DefaultScreen(dpy));
        attrs.border_pixel = BlackPixel(dpy, DefaultScreen(dpy));
        win = XCreateWindow(dpy, root, x, y, w, h, 2,
                            CopyFromParent, InputOutput, CopyFromParent,
                            CWOverrideRedirect | CWBackPixel | CWBorderPixel,
                            &attrs);
    }
    XStoreName(dpy, win, title);
    XSelectInput(dpy, win, KeyPressMask | ExposureMask | StructureNotifyMask);
    XMapWindow(dpy, win);

    clock_gettime(CLOCK_MONOTONIC, &t0);

    for (;;) {
        if (XPending(dpy)) {
            XNextEvent(dpy, &ev);
            switch (ev.type) {
            case MapNotify:
                XRaiseWindow(dpy, win);
                if (grab_all) {
                    /* Whole-keyboard grab: keys reach this window regardless
                     * of the WM's focus policy (GNOME re-asserts focus after
                     * XSetInputFocus alone), at the cost of making the desktop
                     * untypable for as long as this runs. */
                    XSetInputFocus(dpy, win, RevertToPointerRoot, CurrentTime);
                    if (XGrabKeyboard(dpy, win, False, GrabModeAsync,
                                      GrabModeAsync, CurrentTime) != GrabSuccess)
                        fprintf(stderr, "keytarget: XGrabKeyboard failed, "
                                        "relying on focus\n");
                } else {
                    /* Default: passively grab only the probe key, on the root
                     * window. The injected key reaches us whatever holds focus,
                     * every other key still goes where the user expects, and we
                     * never steal focus - so the desktop stays usable. */
                    probe_keycode = XKeysymToKeycode(dpy, (KeySym)probe_keysym);
                    if (!probe_keycode) {
                        fprintf(stderr, "keytarget: keysym 0x%lX is not mapped "
                                        "on this keyboard\n", probe_keysym);
                        goto out;
                    }
                    XGrabKey(dpy, probe_keycode, AnyModifier, root, False,
                             GrabModeAsync, GrabModeAsync);
                }
                if (!ready_printed) {
                    printf("ready (%s, keysym 0x%lX)\n",
                           grab_all ? "whole keyboard grabbed"
                                    : "probe key only", probe_keysym);
                    fflush(stdout);
                    ready_printed = 1;
                }
                break;
            case Expose:
                repaint(dpy, win, w, h, count);
                break;
            case KeyPress: {
                KeySym ks = XLookupKeysym(&ev.xkey, 0);
                if (ks == XK_Escape)
                    goto out;
                count++;
                /* Raise before repainting: if the window is covered by the
                 * user's windows the repaint never reaches the visible
                 * framebuffer and x11vnc has nothing to detect. */
                XRaiseWindow(dpy, win);
                repaint(dpy, win, w, h, count);
                break;
            }
            case DestroyNotify:
                goto out;
            }
            continue;
        }

        clock_gettime(CLOCK_MONOTONIC, &now);
        if (signalled || now.tv_sec - t0.tv_sec >= duration)
            break;

        {
            /* Wait for X events instead of polling: an injected key event
             * must be handled within ~1 ms or it corrupts the latency
             * measurement this tool exists for. */
            fd_set rfds;
            struct timeval tv = {0, 100 * 1000};
            FD_ZERO(&rfds);
            FD_SET(ConnectionNumber(dpy), &rfds);
            select(ConnectionNumber(dpy) + 1, &rfds, NULL, NULL, &tv);
        }
    }
out:
    printf("keytarget: %ld keypresses\n", count);
    XDestroyWindow(dpy, win);
    XCloseDisplay(dpy);
    return 0;
}
