/*
 * pollmode - why does polling ~55x/sec against a 60fps source yield only ~33
 * new frames/sec?
 *
 * Now that x11vnc grabs once per scan cycle, its poll rate is low and
 * unsynchronised with the driver's dwSamplingRateMs tick, so grabs can land
 * repeatedly inside one sampling window and come back empty.
 *
 * Compares, at a fixed poll rate:
 *   sample+NOWAIT   - what the code does today
 *   sample+WAIT     - NOWAIT_IF_NEW_FRAME_READY with a short timeout
 *   push+NOWAIT     - bPushModel, frames generated on damage
 *   push+WAIT       - both
 *
 * Run with load on screen:  ./loadgen -g 960x540+64+64 -r 60 -d 60 &
 *
 * cc -O2 -I../x11vnc/src/nvfbc -o pollmode pollmode.c -ldl
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <time.h>
#include <stdint.h>
#include "NvFBC.h"

static const unsigned int MAGIC[4] = {0xAEF57AC5,0x401D1A39,0x1B856BBE,0x9ED0CEBA};
static NVFBC_API_FUNCTION_LIST api;
static NVFBC_SESSION_HANDLE h;

static double now_s(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t);
    return t.tv_sec + t.tv_nsec/1e9; }

static void run(const char *label, int push, int wait_ms, double poll_hz, double secs)
{
    void *buf=NULL, *dmap=NULL;
    NVFBC_CREATE_CAPTURE_SESSION_PARAMS cs={0};
    cs.dwVersion=NVFBC_CREATE_CAPTURE_SESSION_PARAMS_VER;
    cs.eCaptureType=NVFBC_CAPTURE_TO_SYS;
    cs.eTrackingType=NVFBC_TRACKING_SCREEN;
    cs.bWithCursor=NVFBC_FALSE;
    cs.dwSamplingRateMs=16;
    cs.bPushModel = push ? NVFBC_TRUE : NVFBC_FALSE;
    if(api.nvFBCCreateCaptureSession(h,&cs)!=NVFBC_SUCCESS){
        printf("  %-28s session failed: %s\n",label,api.nvFBCGetLastErrorStr(h)); return; }

    NVFBC_TOSYS_SETUP_PARAMS su={0};
    su.dwVersion=NVFBC_TOSYS_SETUP_PARAMS_VER;
    su.eBufferFormat=NVFBC_BUFFER_FORMAT_BGRA;
    su.ppBuffer=&buf;
    su.bWithDiffMap=NVFBC_TRUE; su.ppDiffMap=&dmap; su.dwDiffMapScalingFactor=32;
    if(api.nvFBCToSysSetUp(h,&su)!=NVFBC_SUCCESS){
        printf("  %-28s setup failed: %s\n",label,api.nvFBCGetLastErrorStr(h)); goto done; }

    double t0=now_s(), next=t0, period = 1.0/poll_hz;
    long grabs=0, news=0;
    double busy=0;

    while (now_s() - t0 < secs) {
        NVFBC_TOSYS_GRAB_FRAME_PARAMS g={0}; NVFBC_FRAME_GRAB_INFO fi={0};
        g.dwVersion=NVFBC_TOSYS_GRAB_FRAME_PARAMS_VER;
        g.dwFlags = wait_ms > 0 ? NVFBC_TOSYS_GRAB_FLAGS_NOWAIT_IF_NEW_FRAME_READY
                                : NVFBC_TOSYS_GRAB_FLAGS_NOWAIT;
        g.dwTimeoutMs = wait_ms > 0 ? (uint32_t)wait_ms : 0;
        g.pFrameGrabInfo=&fi;
        double a=now_s();
        if(api.nvFBCToSysGrabFrame(h,&g)!=NVFBC_SUCCESS) break;
        busy += now_s()-a;
        grabs++;
        if(fi.bIsNewFrame) news++;

        next += period;
        double slack = next - now_s();
        if (slack > 0) { struct timespec ts={0,(long)(slack*1e9)}; nanosleep(&ts,NULL); }
        else next = now_s();
    }
    double dt = now_s()-t0;
    printf("  %-28s %6.1f grabs/s -> %5.1f new fps  (%4.1f%% of grabs new, %4.1f%% time in grab)\n",
           label, grabs/dt, news/dt, 100.0*news/(grabs?grabs:1), 100.0*busy/dt);
done:{
    NVFBC_DESTROY_CAPTURE_SESSION_PARAMS dp={0};
    dp.dwVersion=NVFBC_DESTROY_CAPTURE_SESSION_PARAMS_VER;
    api.nvFBCDestroyCaptureSession(h,&dp); }
}

int main(int argc, char **argv){
    double hz = (argc>1)? atof(argv[1]) : 55.0;
    double secs = (argc>2)? atof(argv[2]) : 8.0;

    void*lib=dlopen("libnvidia-fbc.so.1",RTLD_NOW);
    if(!lib) lib=dlopen("libnvidia-fbc.so",RTLD_NOW);
    if(!lib){fprintf(stderr,"no libnvidia-fbc\n");return 1;}
    PNVFBCCREATEINSTANCE ci=(PNVFBCCREATEINSTANCE)dlsym(lib,"NvFBCCreateInstance");
    api.dwVersion=NVFBC_VERSION;
    if(!ci || ci(&api)!=NVFBC_SUCCESS){fprintf(stderr,"instance failed\n");return 1;}
    NVFBC_CREATE_HANDLE_PARAMS hp={0};
    hp.dwVersion=NVFBC_CREATE_HANDLE_PARAMS_VER;
    hp.privateData=MAGIC; hp.privateDataSize=sizeof(MAGIC);
    if(api.nvFBCCreateHandle(&h,&hp)!=NVFBC_SUCCESS){
        fprintf(stderr,"handle: %s\n",api.nvFBCGetLastErrorStr(h)); return 1;}

    printf("polling at %.0f Hz for %.0fs each, 60fps source:\n", hz, secs);
    run("sample + NOWAIT (today)",  0, 0, hz, secs);
    run("sample + WAIT 5ms",        0, 5, hz, secs);
    run("push   + NOWAIT",          1, 0, hz, secs);
    run("push   + WAIT 5ms",        1, 5, hz, secs);

    NVFBC_DESTROY_HANDLE_PARAMS dh={0};
    dh.dwVersion=NVFBC_DESTROY_HANDLE_PARAMS_VER;
    api.nvFBCDestroyHandle(h,&dh);
    return 0;
}
