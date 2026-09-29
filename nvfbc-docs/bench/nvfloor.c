/*
 * nvfloor - measures the NVFBC cost floor on this machine, independently of
 * x11vnc.  These are properties of the GPU/driver/screen layout, so they are
 * the yardstick x11vnc's measured behaviour is compared against.
 *
 * Emits a human table plus "#KV key=value" lines for measure.py.
 *
 * cc -O2 -I../../src/nvfbc -o nvfloor nvfloor.c -ldl
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

static double now_us(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t);
    return t.tv_sec*1e6 + t.tv_nsec/1e3; }
static int cmp_d(const void*a,const void*b){ double x=*(const double*)a,y=*(const double*)b;
    return (x>y)-(x<y); }

/* One capture session; returns p50 grab cost in us.  force=1 makes NVFBC copy
 * every time, isolating grab+DMA from frame novelty. */
static double measure(const char *label, const char *kv,
                      NVFBC_TRACKING_TYPE track, uint32_t outid,
                      NVFBC_BOX box, NVFBC_SIZE size, int force, int nsamp)
{
    void *buf=NULL,*dmap=NULL; double p50=-1;
    NVFBC_CREATE_CAPTURE_SESSION_PARAMS cs={0};
    cs.dwVersion=NVFBC_CREATE_CAPTURE_SESSION_PARAMS_VER;
    cs.eCaptureType=NVFBC_CAPTURE_TO_SYS;
    cs.eTrackingType=track; cs.dwOutputId=outid;
    cs.bWithCursor=NVFBC_FALSE;
    cs.captureBox=box; cs.frameSize=size; cs.bRoundFrameSize=NVFBC_TRUE;
    cs.dwSamplingRateMs=16;
    if(api.nvFBCCreateCaptureSession(h,&cs)!=NVFBC_SUCCESS){
        printf("  %-42s FAILED: %s\n",label,api.nvFBCGetLastErrorStr(h)); return -1; }

    NVFBC_TOSYS_SETUP_PARAMS su={0};
    su.dwVersion=NVFBC_TOSYS_SETUP_PARAMS_VER;
    su.eBufferFormat=NVFBC_BUFFER_FORMAT_BGRA;
    su.ppBuffer=&buf;
    su.bWithDiffMap=NVFBC_TRUE; su.ppDiffMap=&dmap; su.dwDiffMapScalingFactor=32;
    if(api.nvFBCToSysSetUp(h,&su)!=NVFBC_SUCCESS){
        printf("  %-42s SETUP FAILED: %s\n",label,api.nvFBCGetLastErrorStr(h)); goto done; }

    double *t = malloc(sizeof(double)*nsamp);
    int n=0, warm=5;
    uint32_t fw=0, fh=0, fsz=0;
    for(int i=0; i<nsamp+warm && n<nsamp; i++){
        NVFBC_TOSYS_GRAB_FRAME_PARAMS g={0}; NVFBC_FRAME_GRAB_INFO fi={0};
        g.dwVersion=NVFBC_TOSYS_GRAB_FRAME_PARAMS_VER;
        g.dwFlags=NVFBC_TOSYS_GRAB_FLAGS_NOWAIT
                | (force?NVFBC_TOSYS_GRAB_FLAGS_FORCE_REFRESH:0);
        g.pFrameGrabInfo=&fi;
        double t0=now_us();
        if(api.nvFBCToSysGrabFrame(h,&g)!=NVFBC_SUCCESS){
            printf("  %-42s GRAB FAILED: %s\n",label,api.nvFBCGetLastErrorStr(h));
            free(t); goto done; }
        double dt=now_us()-t0;
        fw=fi.dwWidth; fh=fi.dwHeight; fsz=fi.dwByteSize;
        /* without FORCE_REFRESH we want the "nothing new" cost, so real
         * frames (which carry a DMA) are not representative - skip them */
        if(i>=warm && (force || !fi.bIsNewFrame)) t[n++]=dt;
    }
    printf("  %-42s %4ux%-4u %5.1f MB  ", label, fw, fh, fsz/1048576.0);
    if(n){
        qsort(t,n,sizeof(double),cmp_d);
        p50=t[n/2];
        printf("p50 %8.2f us  (n=%d)\n", p50, n);
        if(kv) printf("#KV %s=%.3f\n", kv, p50);
    } else printf("no samples\n");
    free(t);
done:{
    NVFBC_DESTROY_CAPTURE_SESSION_PARAMS dp={0};
    dp.dwVersion=NVFBC_DESTROY_CAPTURE_SESSION_PARAMS_VER;
    api.nvFBCDestroyCaptureSession(h,&dp); }
    return p50;
}

int main(void){
    void*lib=dlopen("libnvidia-fbc.so.1",RTLD_NOW);
    if(!lib) lib=dlopen("libnvidia-fbc.so",RTLD_NOW);
    if(!lib){ fprintf(stderr,"nvfloor: libnvidia-fbc not found\n"); return 1; }
    PNVFBCCREATEINSTANCE ci=(PNVFBCCREATEINSTANCE)dlsym(lib,"NvFBCCreateInstance");
    if(!ci){ fprintf(stderr,"nvfloor: no NvFBCCreateInstance\n"); return 1; }
    api.dwVersion=NVFBC_VERSION;
    if(ci(&api)!=NVFBC_SUCCESS){ fprintf(stderr,"nvfloor: CreateInstance failed\n"); return 1; }

    NVFBC_CREATE_HANDLE_PARAMS hp={0};
    hp.dwVersion=NVFBC_CREATE_HANDLE_PARAMS_VER;
    hp.privateData=MAGIC; hp.privateDataSize=sizeof(MAGIC);
    if(api.nvFBCCreateHandle(&h,&hp)!=NVFBC_SUCCESS){
        fprintf(stderr,"nvfloor: CreateHandle failed: %s\n",api.nvFBCGetLastErrorStr(h)); return 1; }

    NVFBC_GET_STATUS_PARAMS sp={0}; sp.dwVersion=NVFBC_GET_STATUS_PARAMS_VER;
    api.nvFBCGetStatus(h,&sp);
    printf("NVFBC cost floor: screen %ux%u, %u outputs\n", sp.screenSize.w, sp.screenSize.h,
           sp.dwOutputNum);
    printf("#KV screen_w=%u\n#KV screen_h=%u\n", sp.screenSize.w, sp.screenSize.h);
    for(uint32_t i=0;i<sp.dwOutputNum;i++){
        printf("  output id=%-4u %-8s %ux%u+%u+%u\n", sp.outputs[i].dwId, sp.outputs[i].name,
               sp.outputs[i].trackedBox.w, sp.outputs[i].trackedBox.h,
               sp.outputs[i].trackedBox.x, sp.outputs[i].trackedBox.y);
        printf("#KV output_geom_%s=%ux%u+%u+%u\n", sp.outputs[i].name,
               sp.outputs[i].trackedBox.w, sp.outputs[i].trackedBox.h,
               sp.outputs[i].trackedBox.x, sp.outputs[i].trackedBox.y);
    }

    NVFBC_BOX nobox={0}; NVFBC_SIZE nosize={0};

    printf("\n");
    measure("poll, no new frame (per redundant grab)", "grab_nonew_us",
            NVFBC_TRACKING_SCREEN, 0, nobox, nosize, 0, 2000);
    measure("full-screen grab + DMA (today)", "grab_full_us",
            NVFBC_TRACKING_SCREEN, 0, nobox, nosize, 1, 100);
    for(uint32_t i=0;i<sp.dwOutputNum;i++){
        char lbl[80], kv[48];
        snprintf(lbl,sizeof lbl,"output-tracked grab (%s)", sp.outputs[i].name);
        snprintf(kv,sizeof kv,"grab_output_%s_us", sp.outputs[i].name);
        measure(lbl, kv, NVFBC_TRACKING_OUTPUT, sp.outputs[i].dwId, nobox, nosize, 1, 100);
    }

    NVFBC_DESTROY_HANDLE_PARAMS dh={0};
    dh.dwVersion=NVFBC_DESTROY_HANDLE_PARAMS_VER;
    api.nvFBCDestroyHandle(h,&dh);
    return 0;
}
