/*
 * diffcheck - is NVFBC's diff map trustworthy enough to drive tile marking?
 *
 * The question that matters: is the map a delta against the last frame *we
 * captured*, or against the last frame the driver *generated*?  If it is the
 * latter, a client that grabs slower than the display generates would silently
 * miss changes, and driving tile_has_diff[] from it would leave stale pixels
 * on screen.
 *
 * Method: keep our own copy of the previous captured frame, recompute dirty
 * 32x32 tiles by memcmp, and compare against what the driver reported.
 *
 *   MISSED = we saw a change the driver's map did not flag  <- disqualifying
 *   EXTRA  = driver flagged a tile we saw as unchanged      <- harmless
 *
 * Run with load on screen, e.g.  ./loadgen -g 1280x720+64+64 -r 60 -d 25 &
 *
 * cc -O2 -I../../src/nvfbc -o diffcheck diffcheck.c -ldl
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <time.h>
#include <stdint.h>
#include "NvFBC.h"

#define TILE 32

static const unsigned int MAGIC[4] = {0xAEF57AC5,0x401D1A39,0x1B856BBE,0x9ED0CEBA};

static double now_s(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t);
    return t.tv_sec + t.tv_nsec/1e9; }

int main(int argc, char **argv) {
    int grab_delay_ms = (argc > 1) ? atoi(argv[1]) : 0;   /* simulate slow consumer */
    double run_s      = (argc > 2) ? atof(argv[2]) : 15.0;

    void *lib = dlopen("libnvidia-fbc.so.1", RTLD_NOW);
    if (!lib) lib = dlopen("libnvidia-fbc.so", RTLD_NOW);
    if (!lib) { fprintf(stderr, "diffcheck: libnvidia-fbc not found\n"); return 1; }
    PNVFBCCREATEINSTANCE ci = (PNVFBCCREATEINSTANCE)dlsym(lib, "NvFBCCreateInstance");
    NVFBC_API_FUNCTION_LIST api = {0};
    api.dwVersion = NVFBC_VERSION;
    if (!ci || ci(&api) != NVFBC_SUCCESS) { fprintf(stderr, "diffcheck: instance\n"); return 1; }

    NVFBC_SESSION_HANDLE h = 0;
    NVFBC_CREATE_HANDLE_PARAMS hp = {0};
    hp.dwVersion = NVFBC_CREATE_HANDLE_PARAMS_VER;
    hp.privateData = MAGIC; hp.privateDataSize = sizeof(MAGIC);
    if (api.nvFBCCreateHandle(&h, &hp) != NVFBC_SUCCESS) {
        fprintf(stderr, "diffcheck: handle: %s\n", api.nvFBCGetLastErrorStr(h)); return 1; }

    void *buf = NULL, *dmap = NULL;
    NVFBC_CREATE_CAPTURE_SESSION_PARAMS cs = {0};
    cs.dwVersion = NVFBC_CREATE_CAPTURE_SESSION_PARAMS_VER;
    cs.eCaptureType = NVFBC_CAPTURE_TO_SYS;
    cs.eTrackingType = NVFBC_TRACKING_SCREEN;
    cs.bWithCursor = NVFBC_FALSE;
    cs.dwSamplingRateMs = 16;
    if (api.nvFBCCreateCaptureSession(h, &cs) != NVFBC_SUCCESS) {
        fprintf(stderr, "diffcheck: session: %s\n", api.nvFBCGetLastErrorStr(h)); return 1; }

    NVFBC_TOSYS_SETUP_PARAMS su = {0};
    su.dwVersion = NVFBC_TOSYS_SETUP_PARAMS_VER;
    su.eBufferFormat = NVFBC_BUFFER_FORMAT_BGRA;
    su.ppBuffer = &buf;
    su.bWithDiffMap = NVFBC_TRUE;
    su.ppDiffMap = &dmap;
    su.dwDiffMapScalingFactor = TILE;
    if (api.nvFBCToSysSetUp(h, &su) != NVFBC_SUCCESS) {
        fprintf(stderr, "diffcheck: setup: %s\n", api.nvFBCGetLastErrorStr(h)); return 1; }

    uint32_t mw = su.diffMapSize.w, mh = su.diffMapSize.h;
    printf("diffcheck: diffmap %ux%u (scale %d), grab_delay=%dms, %.0fs\n",
           mw, mh, TILE, grab_delay_ms, run_s);

    uint8_t *prev = NULL;
    uint32_t prev_sz = 0, fw = 0, fh = 0;
    long frames = 0, missed_tiles = 0, extra_tiles = 0, real_tiles = 0, map_tiles = 0;
    long frames_with_miss = 0;
    double t0 = now_s();

    while (now_s() - t0 < run_s) {
        NVFBC_TOSYS_GRAB_FRAME_PARAMS g = {0};
        NVFBC_FRAME_GRAB_INFO fi = {0};
        g.dwVersion = NVFBC_TOSYS_GRAB_FRAME_PARAMS_VER;
        g.dwFlags = NVFBC_TOSYS_GRAB_FLAGS_NOWAIT;
        g.pFrameGrabInfo = &fi;
        if (api.nvFBCToSysGrabFrame(h, &g) != NVFBC_SUCCESS) {
            fprintf(stderr, "diffcheck: grab: %s\n", api.nvFBCGetLastErrorStr(h)); break; }

        if (grab_delay_ms > 0) {
            struct timespec ts = {grab_delay_ms/1000, (grab_delay_ms%1000)*1000000L};
            nanosleep(&ts, NULL);
        }
        if (!fi.bIsNewFrame) continue;

        fw = fi.dwWidth; fh = fi.dwHeight;
        uint32_t stride = fw * 4;

        if (!prev || prev_sz != fi.dwByteSize) {
            free(prev);
            prev = malloc(fi.dwByteSize);
            prev_sz = fi.dwByteSize;
            memcpy(prev, buf, prev_sz);
            continue;                      /* first frame: nothing to compare */
        }

        int miss_this_frame = 0;
        for (uint32_t ty = 0; ty < mh; ty++) {
            for (uint32_t tx = 0; tx < mw; tx++) {
                uint32_t x0 = tx * TILE, y0 = ty * TILE;
                uint32_t w = (x0 + TILE > fw) ? fw - x0 : TILE;
                uint32_t hgt = (y0 + TILE > fh) ? fh - y0 : TILE;
                int changed = 0;
                for (uint32_t r = 0; r < hgt && !changed; r++) {
                    size_t off = (size_t)(y0 + r) * stride + (size_t)x0 * 4;
                    if (memcmp((uint8_t*)buf + off, prev + off, (size_t)w * 4)) changed = 1;
                }
                int flagged = ((uint8_t*)dmap)[ty * mw + tx] != 0;
                if (changed) real_tiles++;
                if (flagged) map_tiles++;
                if (changed && !flagged) { missed_tiles++; miss_this_frame = 1; }
                if (flagged && !changed) extra_tiles++;
            }
        }
        if (miss_this_frame) frames_with_miss++;
        memcpy(prev, buf, prev_sz);
        frames++;
    }

    printf("  frames compared     %ld  (%ux%u)\n", frames, fw, fh);
    printf("  tiles truly changed %ld\n", real_tiles);
    printf("  tiles map flagged   %ld\n", map_tiles);
    printf("  MISSED by map       %ld   (%.4f%% of changed)  frames affected: %ld\n",
           missed_tiles, real_tiles ? 100.0*missed_tiles/real_tiles : 0.0, frames_with_miss);
    printf("  EXTRA in map        %ld   (harmless overdraw)\n", extra_tiles);
    printf("  VERDICT: %s\n", missed_tiles == 0
        ? "diff map is safe to drive tile marking"
        : "diff map MISSES changes - do not trust it alone");

    NVFBC_DESTROY_CAPTURE_SESSION_PARAMS dp = {0};
    dp.dwVersion = NVFBC_DESTROY_CAPTURE_SESSION_PARAMS_VER;
    api.nvFBCDestroyCaptureSession(h, &dp);
    NVFBC_DESTROY_HANDLE_PARAMS dh = {0};
    dh.dwVersion = NVFBC_DESTROY_HANDLE_PARAMS_VER;
    api.nvFBCDestroyHandle(h, &dh);
    free(prev);
    return missed_tiles ? 2 : 0;
}
