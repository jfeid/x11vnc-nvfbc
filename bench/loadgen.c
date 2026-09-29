/*
 * loadgen - deterministic X11 screen-load generator for x11vnc benchmarking.
 *
 * Dirties a controllable fraction of a controllable area at a controllable
 * rate, so capture-path measurements are comparable across builds.
 *
 * Server-side XFillRectangle is used on purpose: the generator itself stays
 * cheap and off the measurement, while the X server produces real damage
 * exactly the way an ordinary application does.
 *
 * cc -O2 -o loadgen loadgen.c -lX11
 */
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/extensions/XShm.h>
#include <sys/ipc.h>
#include <sys/shm.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static double now_s(void) {
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}

static void usage(const char *p) {
	fprintf(stderr,
	    "usage: %s -g WxH+X+Y [-r fps] [-d secs] [-c cellpx] [-f frac] [-t title]\n"
	    "  -g  window geometry               (default 960x540+64+64)\n"
	    "  -r  target updates per second     (default 60, 0 = unthrottled)\n"
	    "  -d  duration in seconds           (default 30)\n"
	    "  -c  cell size in pixels           (default 32, matches x11vnc tile)\n"
	    "  -f  fraction of cells redrawn     (default 1.0)\n", p);
	exit(2);
}

int main(int argc, char **argv) {
	int w = 960, h = 540, wx = 64, wy = 64;
	double rate = 60.0, dur = 30.0, frac = 1.0;
	int cell = 32, i;
	const char *title = "loadgen";
	long solid = -1;		/* -solid RRGGBB: hold one colour, for pixel checks */
	int pipe_mode = 0;		/* -pipe: flip on demand, for latency probing */
	int blit_mode = 0;		/* -blit: one large image per frame, like a video player */

	for (i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "-g") && i + 1 < argc) {
			if (sscanf(argv[++i], "%dx%d+%d+%d", &w, &h, &wx, &wy) != 4) usage(argv[0]);
		} else if (!strcmp(argv[i], "-r") && i + 1 < argc) { rate = atof(argv[++i]);
		} else if (!strcmp(argv[i], "-d") && i + 1 < argc) { dur = atof(argv[++i]);
		} else if (!strcmp(argv[i], "-c") && i + 1 < argc) { cell = atoi(argv[++i]);
		} else if (!strcmp(argv[i], "-f") && i + 1 < argc) { frac = atof(argv[++i]);
		} else if (!strcmp(argv[i], "-t") && i + 1 < argc) { title = argv[++i];
		} else if (!strcmp(argv[i], "-solid") && i + 1 < argc) { solid = strtol(argv[++i], NULL, 16);
		} else if (!strcmp(argv[i], "-pipe")) { pipe_mode = 1;
		} else if (!strcmp(argv[i], "-blit")) { blit_mode = 1;
		} else usage(argv[0]);
	}
	if (cell < 1) cell = 1;

	Display *dpy = XOpenDisplay(NULL);
	if (!dpy) { fprintf(stderr, "loadgen: cannot open display\n"); return 1; }
	int scr = DefaultScreen(dpy);

	XSetWindowAttributes swa;
	swa.override_redirect = True;              /* no WM decoration, exact geometry */
	swa.background_pixel = BlackPixel(dpy, scr);
	Window win = XCreateWindow(dpy, RootWindow(dpy, scr), wx, wy, w, h, 0,
	    CopyFromParent, InputOutput, CopyFromParent,
	    CWOverrideRedirect | CWBackPixel, &swa);
	XStoreName(dpy, win, title);
	XMapRaised(dpy, win);
	XFlush(dpy);

	GC gc = XCreateGC(dpy, win, 0, NULL);

	int cols = (w + cell - 1) / cell;
	int rows = (h + cell - 1) / cell;
	long total_cells = (long)cols * rows;
	long per_frame = (long)(total_cells * frac);
	if (per_frame < 1) per_frame = 1;

	unsigned int seed = 12345;                 /* fixed seed: repeatable runs */
	double t0 = now_s(), next = t0;
	long frames = 0;
	double period = (rate > 0.0) ? 1.0 / rate : 0.0;

	if (blit_mode) {
		/*
		 * One MIT-SHM XShmPutImage of the whole window per frame: the
		 * damage pattern a video player produces, as opposed to the
		 * thousands of small fills the default mode emits.  The
		 * distinction matters for NVFBC push model, which generates a
		 * frame per damage event and can otherwise capture part way
		 * through a repaint.
		 */
		XShmSegmentInfo shminfo;
		XImage *img = XShmCreateImage(dpy, DefaultVisual(dpy, scr),
		    DefaultDepth(dpy, scr), ZPixmap, NULL, &shminfo, w, h);
		if (!img) { fprintf(stderr, "loadgen: XShmCreateImage failed\n"); return 1; }
		shminfo.shmid = shmget(IPC_PRIVATE,
		    (size_t)img->bytes_per_line * img->height, IPC_CREAT | 0600);
		if (shminfo.shmid < 0) { perror("shmget"); return 1; }
		shminfo.shmaddr = img->data = shmat(shminfo.shmid, NULL, 0);
		shminfo.readOnly = False;
		if (!XShmAttach(dpy, &shminfo)) {
			fprintf(stderr, "loadgen: XShmAttach failed\n"); return 1; }
		XSync(dpy, False);
		shmctl(shminfo.shmid, IPC_RMID, NULL);   /* reclaimed on exit */

		printf("loadgen: blit %dx%d+%d+%d target=%.0f fps dur=%.0fs (one XShmPutImage/frame)\n",
		    w, h, wx, wy, rate, dur);
		fflush(stdout);

		unsigned int *px = (unsigned int *)img->data;
		long npx = (long)(img->bytes_per_line / 4) * img->height;
		while (now_s() - t0 < dur) {
			unsigned int base = (unsigned int)(frames * 2654435761u);
			for (long k = 0; k < npx; k++) {
				px[k] = base + (unsigned int)k;   /* whole surface changes */
			}
			XShmPutImage(dpy, win, gc, img, 0, 0, 0, 0, w, h, False);
			XFlush(dpy);
			frames++;
			if (period > 0.0) {
				next += period;
				double slack = next - now_s();
				if (slack > 0) usleep((useconds_t)(slack * 1e6));
				else next = now_s();
			}
		}
		double el = now_s() - t0;
		printf("loadgen: %ld frames in %.2fs = %.1f achieved fps\n", frames, el, frames / el);
		XShmDetach(dpy, &shminfo);
		shmdt(shminfo.shmaddr);
		XDestroyWindow(dpy, win);
		XCloseDisplay(dpy);
		return 0;
	}

	if (pipe_mode) {
		/*
		 * One colour per line on stdin; fill, XSync so the server has
		 * really processed it, then print the CLOCK_MONOTONIC time of
		 * that moment.  A latency probe correlates those stamps with
		 * when the change reaches a VNC client.  XSync (not XFlush) is
		 * the point: it makes the printed stamp mean "the X server has
		 * this", not "the request has been written to a socket".
		 */
		char line[64];
		printf("ready\n");
		fflush(stdout);
		while (fgets(line, sizeof line, stdin)) {
			unsigned long c = strtoul(line, NULL, 16);
			XSetForeground(dpy, gc, c);
			XFillRectangle(dpy, win, gc, 0, 0, w, h);
			XSync(dpy, False);
			printf("%.6f\n", now_s());
			fflush(stdout);
		}
		XDestroyWindow(dpy, win);
		XCloseDisplay(dpy);
		return 0;
	}

	if (solid >= 0) {
		/* Known colour at a known position, so a VNC client can fetch the
		 * rect and prove the capture coordinate mapping is right. */
		printf("loadgen: solid %06lx at %dx%d+%d+%d for %.0fs\n", solid, w, h, wx, wy, dur);
		fflush(stdout);
		while (now_s() - t0 < dur) {
			XSetForeground(dpy, gc, (unsigned long)solid);
			XFillRectangle(dpy, win, gc, 0, 0, w, h);
			XFlush(dpy);
			usleep(100 * 1000);
		}
		XDestroyWindow(dpy, win);
		XCloseDisplay(dpy);
		return 0;
	}

	printf("loadgen: %dx%d+%d+%d cell=%d grid=%dx%d cells/frame=%ld/%ld target=%.0f fps dur=%.0fs\n",
	    w, h, wx, wy, cell, cols, rows, per_frame, total_cells, rate, dur);
	fflush(stdout);

	while (now_s() - t0 < dur) {
		long k;
		for (k = 0; k < per_frame; k++) {
			long idx = (frac >= 1.0) ? k : (rand_r(&seed) % total_cells);
			int cx = (int)(idx % cols) * cell;
			int cy = (int)(idx / cols) * cell;
			/* value cycles per frame so every redraw is a genuine change */
			unsigned long px = ((frames * 7 + idx * 13) & 0xff) << 16
			                 | ((frames * 11 + idx * 5) & 0xff) << 8
			                 | ((frames * 3 + idx * 17) & 0xff);
			XSetForeground(dpy, gc, px);
			XFillRectangle(dpy, win, gc, cx, cy, cell, cell);
		}
		XFlush(dpy);
		frames++;

		if (period > 0.0) {
			next += period;
			double slack = next - now_s();
			if (slack > 0) usleep((useconds_t)(slack * 1e6));
			else next = now_s();       /* fell behind: don't accumulate debt */
		}
	}

	double elapsed = now_s() - t0;
	printf("loadgen: %ld frames in %.2fs = %.1f achieved fps (%.1f Mpx/s dirtied)\n",
	    frames, elapsed, frames / elapsed,
	    frames * (double)per_frame * cell * cell / elapsed / 1e6);

	XDestroyWindow(dpy, win);
	XCloseDisplay(dpy);
	return 0;
}
