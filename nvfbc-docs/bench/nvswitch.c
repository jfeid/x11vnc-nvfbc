/*
 * nvswitch - what does it cost to switch NVFBC capture type, and can two
 * sessions coexist?
 *
 * docs/NVENC-H264-PLAN.md §11 leaves this to Phase 2/3 to settle, and the
 * answer decides the architecture.  The hybrid needs the diff map to know when
 * the screen is moving, but NVFBC_TOCUDA_SETUP_PARAMS has no diff map at all -
 * it carries only dwVersion and eBufferFormat.  So either the capture type is
 * switched on every gate transition, or two sessions run at once, or the
 * zero-copy path is abandoned.
 *
 *   ./nvswitch [iterations]
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <time.h>
#include <stdint.h>
#include "NvFBC.h"

/* NVFBC refuses to start on consumer GeForce parts without this private data -
 * the patch-free method keylase found and Sunshine implements. Same constant
 * nvfloor.c and the fork use. */
static const unsigned int MAGIC[4] = {
	0xAEF57AC5, 0x401D1A39, 0x1B856BBE, 0x9ED0CEBA
};

static double now_ms(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static NVFBC_API_FUNCTION_LIST api;

static const char *type_name(NVFBC_CAPTURE_TYPE t) {
	return t == NVFBC_CAPTURE_TO_SYS ? "TO_SYS" : "SHARED_CUDA";
}

/* Create a capture session of the given type; returns ms taken, or -1. */
static double make_session(NVFBC_SESSION_HANDLE h, NVFBC_CAPTURE_TYPE type) {
	NVFBC_CREATE_CAPTURE_SESSION_PARAMS cs;
	double t0 = now_ms();

	memset(&cs, 0, sizeof(cs));
	cs.dwVersion = NVFBC_CREATE_CAPTURE_SESSION_PARAMS_VER;
	cs.eCaptureType = type;
	cs.bWithCursor = NVFBC_FALSE;
	cs.eTrackingType = NVFBC_TRACKING_SCREEN;
	cs.dwSamplingRateMs = 16;
	if (api.nvFBCCreateCaptureSession(h, &cs) != NVFBC_SUCCESS) {
		fprintf(stderr, "  create %s failed: %s\n", type_name(type),
		    api.nvFBCGetLastErrorStr(h));
		return -1.0;
	}
	return now_ms() - t0;
}

static double destroy_session(NVFBC_SESSION_HANDLE h) {
	NVFBC_DESTROY_CAPTURE_SESSION_PARAMS dp;
	double t0 = now_ms();

	memset(&dp, 0, sizeof(dp));
	dp.dwVersion = NVFBC_DESTROY_CAPTURE_SESSION_PARAMS_VER;
	api.nvFBCDestroyCaptureSession(h, &dp);
	return now_ms() - t0;
}

int main(int argc, char **argv) {
	void *lib;
	PNVFBCCREATEINSTANCE ci;
	NVFBC_SESSION_HANDLE h = 0, h2 = 0;
	NVFBC_CREATE_HANDLE_PARAMS hp;
	int iters = (argc > 1) ? atoi(argv[1]) : 10;
	int i;
	double sum_sys = 0, sum_cuda = 0, sum_destroy = 0;

	lib = dlopen("libnvidia-fbc.so.1", RTLD_NOW);
	if (!lib) {
		fprintf(stderr, "nvswitch: %s\n", dlerror());
		return 1;
	}
	ci = (PNVFBCCREATEINSTANCE) dlsym(lib, "NvFBCCreateInstance");
	if (!ci) {
		fprintf(stderr, "nvswitch: no NvFBCCreateInstance\n");
		return 1;
	}
	memset(&api, 0, sizeof(api));
	api.dwVersion = NVFBC_VERSION;
	if (ci(&api) != NVFBC_SUCCESS) {
		fprintf(stderr, "nvswitch: CreateInstance failed\n");
		return 1;
	}
	memset(&hp, 0, sizeof(hp));
	hp.dwVersion = NVFBC_CREATE_HANDLE_PARAMS_VER;
	hp.privateData = MAGIC;
	hp.privateDataSize = sizeof(MAGIC);
	if (api.nvFBCCreateHandle(&h, &hp) != NVFBC_SUCCESS) {
		fprintf(stderr, "nvswitch: CreateHandle failed: %s\n",
		    api.nvFBCGetLastErrorStr(h));
		return 1;
	}

	printf("switch cost, %d iterations of TO_SYS <-> SHARED_CUDA:\n", iters);
	for (i = 0; i < iters; i++) {
		double a = make_session(h, NVFBC_CAPTURE_TO_SYS);
		double d1 = destroy_session(h);
		double b = make_session(h, NVFBC_CAPTURE_SHARED_CUDA);
		double d2 = destroy_session(h);
		if (a < 0 || b < 0) {
			printf("  aborted at iteration %d\n", i);
			break;
		}
		sum_sys += a; sum_cuda += b; sum_destroy += d1 + d2;
	}
	if (i > 0) {
		printf("  create TO_SYS      : %.1f ms\n", sum_sys / i);
		printf("  create SHARED_CUDA : %.1f ms\n", sum_cuda / i);
		printf("  destroy            : %.1f ms\n", sum_destroy / (2 * i));
		printf("  full switch        : %.1f ms\n",
		    (sum_sys + sum_cuda + sum_destroy) / (2 * i));
	}

	printf("\ntwo sessions at once, one handle:\n");
	if (make_session(h, NVFBC_CAPTURE_TO_SYS) >= 0) {
		double r = make_session(h, NVFBC_CAPTURE_SHARED_CUDA);
		printf("  second session on same handle: %s\n",
		    r >= 0 ? "ALLOWED" : "refused");
		destroy_session(h);
	}

	printf("\ntwo sessions at once, separate handles:\n");
	memset(&hp, 0, sizeof(hp));
	hp.dwVersion = NVFBC_CREATE_HANDLE_PARAMS_VER;
	hp.privateData = MAGIC;
	hp.privateDataSize = sizeof(MAGIC);
	if (api.nvFBCCreateHandle(&h2, &hp) != NVFBC_SUCCESS) {
		printf("  second handle refused: %s\n", api.nvFBCGetLastErrorStr(h2));
	} else {
		double a = make_session(h, NVFBC_CAPTURE_TO_SYS);
		double b = make_session(h2, NVFBC_CAPTURE_SHARED_CUDA);
		printf("  TO_SYS on handle 1 : %s\n", a >= 0 ? "ok" : "failed");
		printf("  CUDA  on handle 2  : %s\n", b >= 0 ? "ok" : "failed");
		printf("  -> both concurrently: %s\n",
		    (a >= 0 && b >= 0) ? "ALLOWED" : "refused");
		if (a >= 0) destroy_session(h);
		if (b >= 0) destroy_session(h2);
	}
	return 0;
}
