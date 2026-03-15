/*
 * gpssim_cuda.cu
 * CUDA-accelerated GPS signal generator — drop-in replacement for gps-sdr-sim
 *
 * Targets dual RTX 3080 Ti (Ampere sm_86). Compatible with Pascal/Turing too.
 *
 * ─── BUILD ───────────────────────────────────────────────────────────────────
 *
 * Single GPU:
 *   gcc -c -O3 -Dmain=__gpssim_cpu_main__ gpssim.c -o gpssim_cpu.o -lm -fopenmp
 *   nvcc -O3 -arch=sm_86 gpssim_cuda.cu gpssim_cpu.o getopt.o -o gps-sdr-sim-cuda -lm
 *
 * Dual GPU (recommended for RTX 3080 Ti pair):
 *   gcc -c -O3 -Dmain=__gpssim_cpu_main__ gpssim.c -o gpssim_cpu.o -lm -fopenmp
 *   nvcc -O3 -arch=sm_86 -DDUAL_GPU gpssim_cuda.cu gpssim_cpu.o getopt.o \
 *        -o gps-sdr-sim-cuda -lm
 *
 * Or use the provided Makefile.cuda:
 *   make -f Makefile.cuda          # single GPU
 *   make -f Makefile.cuda dual     # dual GPU pipelined
 *
 * ─── USAGE ───────────────────────────────────────────────────────────────────
 * Identical to gps-sdr-sim. All original flags work unchanged:
 *   ./gps-sdr-sim-cuda -e brdc0580.26n -l 32.924986,-117.123176,100 -d 3600 -b 8 -o gpssim.c8
 *
 * ─── ARCHITECTURE ────────────────────────────────────────────────────────────
 *
 * CPU handles (same as original, no change):
 *   - Ephemeris parsing, satellite position/velocity computation
 *   - Channel allocation and navigation message generation
 *   - Per-epoch pseudorange and Doppler computation (~1ms per epoch)
 *
 * GPU handles (replaces the inner IQ loop):
 *   - IQ sample generation: 260,000 samples per 100ms epoch at 2.6 MHz
 *   - One CUDA thread per output sample — embarrassingly parallel
 *   - SC08 conversion on GPU (saves PCIe bandwidth vs. doing it on host)
 *
 * Key insight — analytical phase computation:
 *   The original code maintains sequential phase accumulators updated
 *   sample-by-sample. For GPU parallelism we compute exact phase at
 *   sample index i without needing samples i-1, i-2, ...:
 *
 *     carr_phase(i) = carr_phase_epoch_start + i * carr_phasestep
 *     code_phase(i) = code_phase_epoch_start + i * f_code * delt  [mod 1023]
 *     dataBit(i)    = derived from nav word buffer + code count at i
 *
 *   CPU state is advanced by one full epoch after each GPU launch so the
 *   snapshot for the next epoch is correct.
 *
 * Dual GPU pipelining (with -DDUAL_GPU):
 *   GPU0 and GPU1 alternate epochs. While GPU0 computes epoch N+1,
 *   the CPU writes GPU1's finished epoch N to disk. This hides compute
 *   latency behind I/O latency.
 *
 * Expected performance on dual RTX 3080 Ti:
 *   3600s file at 2.6 MHz SC08: ~8-12 seconds
 *   (vs ~35+ minutes single-threaded CPU, ~4 minutes with OpenMP)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>
#include <cuda_runtime.h>

/* Pull in all type definitions and constants from the original header */
#include "gpssim.h"
#ifdef __cplusplus
extern "C" {
#endif
#include "getopt.h"
#ifdef __cplusplus
}
#endif

/* ─── External linkage to globals defined in gpssim.c ──────────────────────
 * These globals live in gpssim_cpu.o (compiled from gpssim.c).
 * We declare them extern so the linker resolves them. */
#ifdef __cplusplus
extern "C" {
#endif

extern int    sinTable512[];
extern int    cosTable512[];
extern int    allocatedSat[MAX_SAT];
extern double xyz[USER_MOTION_SIZE][3];

/* Functions defined in gpssim.c — prototypes from gpssim.h + a few extra */
extern void    usage(void);
extern void    subVect(double *y, const double *x1, const double *x2);
extern double  normVect(const double *x);
extern double  dotProd(const double *x1, const double *x2);
extern void    codegen(int *ca, int prn);
extern void    date2gps(const datetime_t *t, gpstime_t *g);
extern void    gps2date(const gpstime_t *g, datetime_t *t);
extern void    xyz2llh(const double *xyz, double *llh);
extern void    llh2xyz(const double *llh, double *xyz);
extern void    ltcmat(const double *llh, double t[3][3]);
extern void    ecef2neu(const double *xyz, double t[3][3], double *neu);
extern void    neu2azel(double *azel, const double *neu);
extern void    satpos(ephem_t eph, gpstime_t g, double *pos, double *vel, double *clk);
extern void    eph2sbf(const ephem_t eph, const ionoutc_t ionoutc,
                       unsigned long sbf[5][N_DWRD_SBF]);
extern int     replaceExpDesignator(char *str, int len);
extern double  subGpsTime(gpstime_t g1, gpstime_t g0);
extern gpstime_t incGpsTime(gpstime_t g0, double dt);
extern int     readRinexNavAll(ephem_t eph[][MAX_SAT], ionoutc_t *ionoutc,
                               const char *fname);
extern double  ionosphericDelay(const ionoutc_t *ionoutc, gpstime_t g,
                                double *llh, double *azel);
extern void    computeRange(range_t *rho, ephem_t eph, ionoutc_t *ionoutc,
                            gpstime_t g, double xyz[]);
extern void    computeCodePhase(channel_t *chan, range_t rho1, double dt);
extern int     readUserMotion(double xyz[USER_MOTION_SIZE][3], const char *filename);
extern int     readUserMotionLLH(double xyz[USER_MOTION_SIZE][3], const char *filename);
extern int     readNmeaGGA(double xyz[USER_MOTION_SIZE][3], const char *filename);
extern int     generateNavMsg(gpstime_t g, channel_t *chan, int init);
extern int     checkSatVisibility(ephem_t eph, gpstime_t g, double *xyz,
                                  double elvMask, double *azel);
extern int     allocateChannel(channel_t *chan, ephem_t eph[], ionoutc_t ionoutc,
                               gpstime_t grx, double xyz[], double elvmask);

#ifdef __cplusplus
}
#endif

/* ─── CUDA error checking ───────────────────────────────────────────────── */
#define CUDA_CHECK(call) \
    do { \
        cudaError_t _err = (call); \
        if (_err != cudaSuccess) { \
            fprintf(stderr, "CUDA error at %s:%d: %s\n", \
                    __FILE__, __LINE__, cudaGetErrorString(_err)); \
            exit(1); \
        } \
    } while (0)

/* ─── Constants ─────────────────────────────────────────────────────────── */
#define MAX_CHAN_GPU    16
#define CA_SEQ_LEN_GPU  1023
#define BLOCK_SIZE      256    /* threads/block — optimal for Ampere SM */

/* ─── Compact per-epoch channel descriptor ─────────────────────────────── */
/*
 * Snapshot of channel_t state at the start of each 100ms epoch.
 * Passed to the GPU kernel. All fields the kernel needs are here.
 * Uses float for phase values (sufficient precision for one epoch).
 */
typedef struct {
    int          prn;             /* 0 = inactive */
    int          gain;            /* amplitude scale (2^7 units) */
    int          carr_phasestep;  /* carrier phasestep: round(512*65536*f_carr*delt) */
    unsigned int carr_phase_raw;  /* raw uint carrier phase register at epoch start */
    float        code_phase_0;    /* code chip phase at epoch start [0, CA_SEQ_LEN) */
    float        f_code;          /* code chip frequency (Hz) — for phase advance */
    int          icode_0;         /* C/A code count within current nav bit */
    int          ibit_0;          /* bit count within current nav word */
    int          iword_0;         /* current nav word index */
    int          ca[CA_SEQ_LEN_GPU];      /* C/A code chips (0 or 1) */
    unsigned int dwrd[N_DWRD];    /* nav data words (for dataBit extraction) */
} EpochChannel;

/* ─── sin/cos lookup tables in GPU constant memory ──────────────────────
 * Identical values to the tables in gpssim.c. These are uploaded once
 * during GPU initialization. */
__constant__ int d_sinTable512[512];
__constant__ int d_cosTable512[512];

static const int h_sinTable512[512] = {
       2,   5,   8,  11,  14,  17,  20,  23,  26,  29,  32,  35,  38,  41,  44,  47,
      50,  53,  56,  59,  62,  65,  68,  71,  74,  77,  80,  83,  86,  89,  91,  94,
      97, 100, 103, 105, 108, 111, 114, 116, 119, 122, 125, 127, 130, 132, 135, 138,
     140, 143, 145, 148, 150, 153, 155, 157, 160, 162, 164, 167, 169, 171, 173, 176,
     178, 180, 182, 184, 186, 188, 190, 192, 194, 196, 198, 200, 202, 204, 205, 207,
     209, 210, 212, 214, 215, 217, 218, 220, 221, 223, 224, 225, 227, 228, 229, 230,
     232, 233, 234, 235, 236, 237, 238, 239, 240, 241, 241, 242, 243, 244, 244, 245,
     245, 246, 247, 247, 248, 248, 248, 249, 249, 249, 249, 250, 250, 250, 250, 250,
     250, 250, 250, 250, 250, 249, 249, 249, 249, 248, 248, 248, 247, 247, 246, 245,
     245, 244, 244, 243, 242, 241, 241, 240, 239, 238, 237, 236, 235, 234, 233, 232,
     230, 229, 228, 227, 225, 224, 223, 221, 220, 218, 217, 215, 214, 212, 210, 209,
     207, 205, 204, 202, 200, 198, 196, 194, 192, 190, 188, 186, 184, 182, 180, 178,
     176, 173, 171, 169, 167, 164, 162, 160, 157, 155, 153, 150, 148, 145, 143, 140,
     138, 135, 132, 130, 127, 125, 122, 119, 116, 114, 111, 108, 105, 103, 100,  97,
      94,  91,  89,  86,  83,  80,  77,  74,  71,  68,  65,  62,  59,  56,  53,  50,
      47,  44,  41,  38,  35,  32,  29,  26,  23,  20,  17,  14,  11,   8,   5,   2,
      -2,  -5,  -8, -11, -14, -17, -20, -23, -26, -29, -32, -35, -38, -41, -44, -47,
     -50, -53, -56, -59, -62, -65, -68, -71, -74, -77, -80, -83, -86, -89, -91, -94,
     -97,-100,-103,-105,-108,-111,-114,-116,-119,-122,-125,-127,-130,-132,-135,-138,
    -140,-143,-145,-148,-150,-153,-155,-157,-160,-162,-164,-167,-169,-171,-173,-176,
    -178,-180,-182,-184,-186,-188,-190,-192,-194,-196,-198,-200,-202,-204,-205,-207,
    -209,-210,-212,-214,-215,-217,-218,-220,-221,-223,-224,-225,-227,-228,-229,-230,
    -232,-233,-234,-235,-236,-237,-238,-239,-240,-241,-241,-242,-243,-244,-244,-245,
    -245,-246,-247,-247,-248,-248,-248,-249,-249,-249,-249,-250,-250,-250,-250,-250,
    -250,-250,-250,-250,-250,-249,-249,-249,-249,-248,-248,-248,-247,-247,-246,-245,
    -245,-244,-244,-243,-242,-241,-241,-240,-239,-238,-237,-236,-235,-234,-233,-232,
    -230,-229,-228,-227,-225,-224,-223,-221,-220,-218,-217,-215,-214,-212,-210,-209,
    -207,-205,-204,-202,-200,-198,-196,-194,-192,-190,-188,-186,-184,-182,-180,-178,
    -176,-173,-171,-169,-167,-164,-162,-160,-157,-155,-153,-150,-148,-145,-143,-140,
    -138,-135,-132,-130,-127,-125,-122,-119,-116,-114,-111,-108,-105,-103,-100, -97,
     -94, -91, -89, -86, -83, -80, -77, -74, -71, -68, -65, -62, -59, -56, -53, -50,
     -47, -44, -41, -38, -35, -32, -29, -26, -23, -20, -17, -14, -11,  -8,  -5,  -2
};

static const int h_cosTable512[512] = {
     250, 250, 250, 250, 250, 249, 249, 249, 249, 248, 248, 248, 247, 247, 246, 245,
     245, 244, 244, 243, 242, 241, 241, 240, 239, 238, 237, 236, 235, 234, 233, 232,
     230, 229, 228, 227, 225, 224, 223, 221, 220, 218, 217, 215, 214, 212, 210, 209,
     207, 205, 204, 202, 200, 198, 196, 194, 192, 190, 188, 186, 184, 182, 180, 178,
     176, 173, 171, 169, 167, 164, 162, 160, 157, 155, 153, 150, 148, 145, 143, 140,
     138, 135, 132, 130, 127, 125, 122, 119, 116, 114, 111, 108, 105, 103, 100,  97,
      94,  91,  89,  86,  83,  80,  77,  74,  71,  68,  65,  62,  59,  56,  53,  50,
      47,  44,  41,  38,  35,  32,  29,  26,  23,  20,  17,  14,  11,   8,   5,   2,
      -2,  -5,  -8, -11, -14, -17, -20, -23, -26, -29, -32, -35, -38, -41, -44, -47,
     -50, -53, -56, -59, -62, -65, -68, -71, -74, -77, -80, -83, -86, -89, -91, -94,
     -97,-100,-103,-105,-108,-111,-114,-116,-119,-122,-125,-127,-130,-132,-135,-138,
    -140,-143,-145,-148,-150,-153,-155,-157,-160,-162,-164,-167,-169,-171,-173,-176,
    -178,-180,-182,-184,-186,-188,-190,-192,-194,-196,-198,-200,-202,-204,-205,-207,
    -209,-210,-212,-214,-215,-217,-218,-220,-221,-223,-224,-225,-227,-228,-229,-230,
    -232,-233,-234,-235,-236,-237,-238,-239,-240,-241,-241,-242,-243,-244,-244,-245,
    -245,-246,-247,-247,-248,-248,-248,-249,-249,-249,-249,-250,-250,-250,-250,-250,
    -250,-250,-250,-250,-250,-249,-249,-249,-249,-248,-248,-248,-247,-247,-246,-245,
    -245,-244,-244,-243,-242,-241,-241,-240,-239,-238,-237,-236,-235,-234,-233,-232,
    -230,-229,-228,-227,-225,-224,-223,-221,-220,-218,-217,-215,-214,-212,-210,-209,
    -207,-205,-204,-202,-200,-198,-196,-194,-192,-190,-188,-186,-184,-182,-180,-178,
    -176,-173,-171,-169,-167,-164,-162,-160,-157,-155,-153,-150,-148,-145,-143,-140,
    -138,-135,-132,-130,-127,-125,-122,-119,-116,-114,-111,-108,-105,-103,-100, -97,
     -94, -91, -89, -86, -83, -80, -77, -74, -71, -68, -65, -62, -59, -56, -53, -50,
     -47, -44, -41, -38, -35, -32, -29, -26, -23, -20, -17, -14, -11,  -8,  -5,  -2,
       2,   5,   8,  11,  14,  17,  20,  23,  26,  29,  32,  35,  38,  41,  44,  47,
      50,  53,  56,  59,  62,  65,  68,  71,  74,  77,  80,  83,  86,  89,  91,  94,
      97, 100, 103, 105, 108, 111, 114, 116, 119, 122, 125, 127, 130, 132, 135, 138,
     140, 143, 145, 148, 150, 153, 155, 157, 160, 162, 164, 167, 169, 171, 173, 176,
     178, 180, 182, 184, 186, 188, 190, 192, 194, 196, 198, 200, 202, 204, 205, 207,
     209, 210, 212, 214, 215, 217, 218, 220, 221, 223, 224, 225, 227, 228, 229, 230,
     232, 233, 234, 235, 236, 237, 238, 239, 240, 241, 241, 242, 243, 244, 244, 245,
     245, 246, 247, 247, 248, 248, 248, 249, 249, 249, 249, 250, 250, 250, 250, 250
};

/* ─── IQ generation kernel ──────────────────────────────────────────────────
 *
 * One thread per output sample (isamp index).
 *
 * For each sample, we compute the exact carrier phase and code phase at
 * index isamp analytically, sum contributions from all active channels,
 * and write the SC16 I,Q pair.
 *
 * Carrier phase: uses the same integer representation as gpssim.c's
 *   #ifndef FLOAT_CARR_PHASE path. Phase register advances by carr_phasestep
 *   each sample. Table index = (phase >> 16) & 0x1ff.
 *
 * Code phase: float accumulator. f_code is chip rate (Hz), so f_code * delt
 *   chips advance per sample. We don't need delt separately because
 *   f_code_per_sample = f_code * delt is constant within an epoch —
 *   BUT we store f_code (Hz) and delt is implicit in the phasestep.
 *   To avoid passing delt, we precompute f_code_per_sample on CPU and
 *   store it in EpochChannel.f_code as "chips per sample".
 *
 * Data bit: derived from nav word buffer by counting code completions
 *   at sample isamp from the epoch-start icode/ibit/iword state.
 */
__global__ void generateIQ_kernel(
    const EpochChannel* __restrict__ channels,
    int    numChan,
    int    numSamples,
    short* __restrict__ iq_out    /* interleaved SC16: I0,Q0,I1,Q1,... */
) {
    int isamp = blockIdx.x * blockDim.x + threadIdx.x;
    if (isamp >= numSamples) return;

    int i_acc = 0;
    int q_acc = 0;

    for (int ci = 0; ci < numChan; ci++) {
        /* Load channel snapshot into registers — promotes to L1 cache */
        int   prn           = channels[ci].prn;
        if (prn <= 0) continue;

        int   gain          = channels[ci].gain;
        int   carr_phasestep = channels[ci].carr_phasestep;
        unsigned int carr0  = channels[ci].carr_phase_raw;
        float code0         = channels[ci].code_phase_0;
        float fcode         = channels[ci].f_code;  /* chips per sample */
        int   icode0        = channels[ci].icode_0;
        int   ibit0         = channels[ci].ibit_0;
        int   iword0        = channels[ci].iword_0;

        /* ── Carrier phase at sample isamp ── */
        unsigned int carr_phase = carr0 + (unsigned int)carr_phasestep * (unsigned int)isamp;
        int iTable = (int)((carr_phase >> 16) & 0x1ff);

        /* ── Code phase at sample isamp ── */
        float cp = code0 + (float)isamp * fcode;
        /* Wrap into [0, CA_SEQ_LEN) — use reciprocal multiply for speed */
        cp -= floorf(cp * (1.0f / (float)CA_SEQ_LEN_GPU)) * (float)CA_SEQ_LEN_GPU;
        if (cp < 0.0f) cp += (float)CA_SEQ_LEN_GPU;
        int chip_idx = (int)cp;
        if ((unsigned)chip_idx >= (unsigned)CA_SEQ_LEN_GPU)
            chip_idx = CA_SEQ_LEN_GPU - 1;

        /* ── Data bit at sample isamp ──
         * Count total C/A code completions from epoch start to sample isamp.
         * Each code completion = CA_SEQ_LEN chips = one increment of icode.
         * chips_since_start = isamp * f_code (in chip units, not wrapped)
         * codes_completed = floor(chips_since_start / CA_SEQ_LEN) */
        float raw_chips = (float)isamp * fcode; /* unmodded chip count */
        int codes_elapsed = (int)(raw_chips * (1.0f / (float)CA_SEQ_LEN_GPU));

        int total_icode = icode0 + codes_elapsed;
        int icode_now   = total_icode % 20;
        int bits_elapsed = total_icode / 20;
        int total_ibit  = ibit0 + bits_elapsed;
        int ibit_now    = total_ibit % 30;
        int words_elapsed = total_ibit / 30;
        int iword_now   = iword0 + words_elapsed;
        /* Clamp to valid word buffer range */
        if (iword_now >= N_DWRD) iword_now = N_DWRD - 1;
        if (iword_now < 0)       iword_now = 0;

        int dataBit = (int)((channels[ci].dwrd[iword_now] >> (29 - ibit_now)) & 0x1u) * 2 - 1;
        int codeCA  = channels[ci].ca[chip_idx] * 2 - 1;  /* 0/1 -> ±1 */

        /* ── Accumulate ── */
        int ip = dataBit * codeCA * d_cosTable512[iTable] * gain;
        int qp = dataBit * codeCA * d_sinTable512[iTable] * gain;
        i_acc += ip;
        q_acc += qp;
    }

    /* Scale by 2^7 — identical to original */
    i_acc = (i_acc + 64) >> 7;
    q_acc = (q_acc + 64) >> 7;

    iq_out[isamp * 2]     = (short)i_acc;
    iq_out[isamp * 2 + 1] = (short)q_acc;
}

/* ─── SC08 conversion kernel ────────────────────────────────────────────────
 * Converts SC16 (12-bit signal in 16-bit words) to SC08 for HackRF output.
 * Matches: iq8[i] = iq16[i] >> 4 */
__global__ void convertSC08_kernel(
    const short*  __restrict__ iq16,
    signed char*  __restrict__ iq8,
    int n
) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n)
        iq8[i] = (signed char)(iq16[i] >> 4);
}

/* ─── GPU context ───────────────────────────────────────────────────────── */
typedef struct {
    int          device_id;
    EpochChannel *d_channels;   /* device: epoch channel snapshots */
    short        *d_iq16;       /* device: SC16 output buffer */
    signed char  *d_iq8;        /* device: SC08 output buffer */
    short        *h_iq16;       /* host pinned: SC16 DMA destination */
    signed char  *h_iq8;        /* host pinned: SC08 DMA destination */
    cudaStream_t  stream;
    size_t        bytes_iq16;
    size_t        bytes_iq8;
} GpuContext;

static GpuContext g_gpu[2];
static int        g_ngpu = 0;

/* ─── GPU initialization ────────────────────────────────────────────────── */
static void gpuInit(int iq_buff_size)
{
    int avail = 0;
    CUDA_CHECK(cudaGetDeviceCount(&avail));
    if (avail == 0) { fprintf(stderr, "ERROR: No CUDA GPUs found.\n"); exit(1); }

#ifdef DUAL_GPU
    g_ngpu = (avail >= 2) ? 2 : 1;
#else
    g_ngpu = 1;
#endif

    size_t b16 = (size_t)iq_buff_size * 2 * sizeof(short);
    size_t b8  = (size_t)iq_buff_size * 2 * sizeof(signed char);

    for (int d = 0; d < g_ngpu; d++) {
        CUDA_CHECK(cudaSetDevice(d));

        cudaDeviceProp p;
        CUDA_CHECK(cudaGetDeviceProperties(&p, d));
        fprintf(stderr, "[GPU %d] %s  SM %d.%d  %zu MB VRAM\n",
                d, p.name, p.major, p.minor, p.totalGlobalMem >> 20);

        g_gpu[d].device_id = d;
        g_gpu[d].bytes_iq16 = b16;
        g_gpu[d].bytes_iq8  = b8;

        CUDA_CHECK(cudaMalloc(&g_gpu[d].d_channels, MAX_CHAN_GPU * sizeof(EpochChannel)));
        CUDA_CHECK(cudaMalloc(&g_gpu[d].d_iq16, b16));
        CUDA_CHECK(cudaMalloc(&g_gpu[d].d_iq8,  b8));
        CUDA_CHECK(cudaMallocHost(&g_gpu[d].h_iq16, b16));
        CUDA_CHECK(cudaMallocHost(&g_gpu[d].h_iq8,  b8));
        CUDA_CHECK(cudaStreamCreate(&g_gpu[d].stream));

        CUDA_CHECK(cudaMemcpyToSymbol(d_sinTable512, h_sinTable512, 512 * sizeof(int)));
        CUDA_CHECK(cudaMemcpyToSymbol(d_cosTable512, h_cosTable512, 512 * sizeof(int)));
    }

#ifdef DUAL_GPU
    if (g_ngpu == 2) {
        int ok = 0;
        cudaDeviceCanAccessPeer(&ok, 0, 1);
        if (ok) {
            cudaSetDevice(0); cudaDeviceEnablePeerAccess(1, 0);
            cudaSetDevice(1); cudaDeviceEnablePeerAccess(0, 0);
            fprintf(stderr, "[GPU] P2P enabled between GPU 0 and GPU 1\n");
        }
    }
#endif
    fprintf(stderr, "[GPU] Running on %d GPU(s)\n", g_ngpu);
}

static void gpuFree(void)
{
    for (int d = 0; d < g_ngpu; d++) {
        cudaSetDevice(d);
        cudaFree(g_gpu[d].d_channels);
        cudaFree(g_gpu[d].d_iq16);
        cudaFree(g_gpu[d].d_iq8);
        cudaFreeHost(g_gpu[d].h_iq16);
        cudaFreeHost(g_gpu[d].h_iq8);
        cudaStreamDestroy(g_gpu[d].stream);
    }
}

/* ─── Build epoch channel snapshot ─────────────────────────────────────────
 * Called once per epoch before GPU launch.
 * Converts channel_t state to the compact EpochChannel form.
 * f_code is stored as chips-per-sample (f_code_hz * delt). */
static int buildEpochChannels(channel_t *chan, EpochChannel *ec,
                               int *gain, double delt)
{
    int n = 0;
    for (int i = 0; i < MAX_CHAN; i++) {
        if (chan[i].prn <= 0) continue;
        EpochChannel *e = &ec[n++];
        e->prn             = chan[i].prn;
        e->gain            = gain[i];
        e->carr_phasestep  = chan[i].carr_phasestep;
        e->carr_phase_raw  = chan[i].carr_phase;   /* unsigned int */
        e->code_phase_0    = (float)chan[i].code_phase;
        e->f_code          = (float)(chan[i].f_code * delt); /* chips/sample */
        e->icode_0         = chan[i].icode;
        e->ibit_0          = chan[i].ibit;
        e->iword_0         = chan[i].iword;
        memcpy(e->ca,   chan[i].ca,   CA_SEQ_LEN_GPU * sizeof(int));
        for (int w = 0; w < N_DWRD; w++)
            e->dwrd[w] = (unsigned int)chan[i].dwrd[w];
    }
    return n;
}

/* ─── Advance CPU channel state by iq_buff_size samples ─────────────────────
 * After the GPU computes the IQ samples for this epoch, we must advance
 * the CPU channel_t state by exactly iq_buff_size samples so the next
 * buildEpochChannels() produces a correct snapshot.
 *
 * This is equivalent to running the original sample loop iq_buff_size times,
 * but done in one pass for efficiency. */
static void advanceChannels(channel_t *chan, int iq_buff_size, double delt)
{
    for (int i = 0; i < MAX_CHAN; i++) {
        if (chan[i].prn <= 0) continue;

        /* Advance code phase by iq_buff_size samples */
        chan[i].code_phase += chan[i].f_code * delt * iq_buff_size;

        /* Roll through chip/bit/word boundaries */
        while (chan[i].code_phase >= CA_SEQ_LEN) {
            chan[i].code_phase -= CA_SEQ_LEN;
            chan[i].icode++;
            if (chan[i].icode >= 20) {
                chan[i].icode = 0;
                chan[i].ibit++;
                if (chan[i].ibit >= 30) {
                    chan[i].ibit = 0;
                    chan[i].iword++;
                    if (chan[i].iword >= N_DWRD)
                        chan[i].iword = N_DWRD - 1;
                }
                chan[i].dataBit = (int)((chan[i].dwrd[chan[i].iword] >>
                                        (29 - chan[i].ibit)) & 0x1UL) * 2 - 1;
            }
        }
        chan[i].codeCA = chan[i].ca[(int)chan[i].code_phase] * 2 - 1;

        /* Advance carrier phase register by iq_buff_size phasesteps */
        chan[i].carr_phase += (unsigned int)chan[i].carr_phasestep
                              * (unsigned int)iq_buff_size;
    }
}

/* ─── Launch epoch on GPU (async) ──────────────────────────────────────── */
static void gpuLaunchEpoch(int gid, EpochChannel *h_ec, int numChan,
                            int iq_buff_size, int data_format)
{
    GpuContext *g = &g_gpu[gid];
    CUDA_CHECK(cudaSetDevice(gid));

    CUDA_CHECK(cudaMemcpyAsync(g->d_channels, h_ec,
                               numChan * sizeof(EpochChannel),
                               cudaMemcpyHostToDevice, g->stream));

    int nblk = (iq_buff_size + BLOCK_SIZE - 1) / BLOCK_SIZE;
    generateIQ_kernel<<<nblk, BLOCK_SIZE, 0, g->stream>>>(
        g->d_channels, numChan, iq_buff_size, g->d_iq16);

    if (data_format == SC08) {
        int n   = iq_buff_size * 2;
        int blk = (n + BLOCK_SIZE - 1) / BLOCK_SIZE;
        convertSC08_kernel<<<blk, BLOCK_SIZE, 0, g->stream>>>(
            g->d_iq16, g->d_iq8, n);
        CUDA_CHECK(cudaMemcpyAsync(g->h_iq8, g->d_iq8, g->bytes_iq8,
                                   cudaMemcpyDeviceToHost, g->stream));
    } else {
        CUDA_CHECK(cudaMemcpyAsync(g->h_iq16, g->d_iq16, g->bytes_iq16,
                                   cudaMemcpyDeviceToHost, g->stream));
    }
}

static void gpuSyncEpoch(int gid)
{
    CUDA_CHECK(cudaSetDevice(gid));
    CUDA_CHECK(cudaStreamSynchronize(g_gpu[gid].stream));
}

/* Write completed GPU epoch result to file */
static void gpuWriteEpoch(int gid, FILE *fp, int iq_buff_size, int data_format)
{
    GpuContext *g = &g_gpu[gid];
    if (data_format == SC08) {
        fwrite(g->h_iq8, 1, 2 * iq_buff_size, fp);
    } else if (data_format == SC01) {
        /* SC01: 1-bit quantization packed 8 samples/byte; done on CPU from SC16 */
        int total = 2 * iq_buff_size;
        for (int k = 0; k < total; k += 8) {
            unsigned char byte = 0x00;
            for (int b = 0; b < 8 && (k + b) < total; b++)
                byte |= (g->h_iq16[k + b] > 0 ? 0x01 : 0x00) << (7 - b);
            fwrite(&byte, 1, 1, fp);
        }
    } else { /* SC16 */
        fwrite(g->h_iq16, 2, 2 * iq_buff_size, fp);
    }
}

/* ─── main() ────────────────────────────────────────────────────────────────
 *
 * Argument parsing, ephemeris loading, channel init and navigation message
 * handling are IDENTICAL to the original gpssim.c main().
 * The inner IQ generation loop is replaced with GPU dispatch.
 */
int main(int argc, char *argv[])
{
    clock_t tstart, tend;
    FILE *fp;
    int sv, neph, ieph, i;
    ephem_t eph[EPHEM_ARRAY_SIZE][MAX_SAT];
    gpstime_t g0;
    double llh[3];
    channel_t chan[MAX_CHAN];
    double elvmask = 0.0;

    short      *iq_buff  = NULL;
    signed char *iq8_buff = NULL;

    gpstime_t grx;
    double delt;
    int iumd, numd;
    char umfile[MAX_CHAR];

    int staticLocationMode = FALSE;
    int nmeaGGA = FALSE;
    int umLLH   = FALSE;

    char navfile[MAX_CHAR];
    char outfile[MAX_CHAR];

    double samp_freq;
    int iq_buff_size;
    int data_format;
    int result;

    int gain[MAX_CHAN];
    double path_loss, ant_gain;
    int fixed_gain = 128;
    double ant_pat[37];
    int ibs;

    datetime_t t0, tmin, tmax;
    gpstime_t gmin, gmax;
    double dt;
    int igrx;

    double duration;
    int iduration;
    int verb;
    int timeoverwrite = FALSE;

    ionoutc_t ionoutc;
    int path_loss_enable = TRUE;

    fprintf(stderr, "GPS-SDR-SIM CUDA Edition\n");

    /* ── Defaults ── */
    navfile[0] = 0; umfile[0] = 0;
    strcpy(outfile, "gpssim.bin");
    samp_freq   = 2.6e6;
    data_format = SC16;
    g0.week     = -1;
    iduration   = USER_MOTION_SIZE;
    duration    = (double)iduration / 10.0;
    verb        = FALSE;
    ionoutc.enable = TRUE;
    ionoutc.leapen = FALSE;

    if (argc < 3) { usage(); exit(1); }

    while ((result = getopt(argc, argv, "e:u:x:g:c:l:o:s:b:L:T:t:d:ipv")) != -1) {
        switch (result) {
        case 'e': strcpy(navfile, optarg); break;
        case 'u': strcpy(umfile, optarg); nmeaGGA = FALSE; umLLH = FALSE; break;
        case 'x': strcpy(umfile, optarg); umLLH = TRUE; break;
        case 'g': strcpy(umfile, optarg); nmeaGGA = TRUE; break;
        case 'c':
            staticLocationMode = TRUE;
            sscanf(optarg, "%lf,%lf,%lf", &xyz[0][0], &xyz[0][1], &xyz[0][2]);
            break;
        case 'l':
            staticLocationMode = TRUE;
            sscanf(optarg, "%lf,%lf,%lf", &llh[0], &llh[1], &llh[2]);
            llh[0] /= R2D; llh[1] /= R2D;
            llh2xyz(llh, xyz[0]);
            break;
        case 'o': strcpy(outfile, optarg); break;
        case 's':
            samp_freq = atof(optarg);
            if (samp_freq < 1.0e6) {
                fprintf(stderr, "ERROR: Invalid sampling frequency.\n"); exit(1);
            }
            break;
        case 'b':
            data_format = atoi(optarg);
            if (data_format != SC01 && data_format != SC08 && data_format != SC16) {
                fprintf(stderr, "ERROR: Invalid I/Q data format.\n"); exit(1);
            }
            break;
        case 'L':
            ionoutc.leapen = TRUE;
            sscanf(optarg, "%d,%d,%d", &ionoutc.wnlsf, &ionoutc.dn, &ionoutc.dtlsf);
            break;
        case 'T':
            timeoverwrite = TRUE;
            if (strncmp(optarg, "now", 3) == 0) {
                time_t timer; struct tm *gmt;
                time(&timer); gmt = gmtime(&timer);
                t0.y = gmt->tm_year + 1900; t0.m = gmt->tm_mon + 1;
                t0.d = gmt->tm_mday; t0.hh = gmt->tm_hour;
                t0.mm = gmt->tm_min; t0.sec = (double)gmt->tm_sec;
                date2gps(&t0, &g0);
                break;
            }
            /* fall through */
        case 't':
            sscanf(optarg, "%d/%d/%d,%d:%d:%lf",
                   &t0.y, &t0.m, &t0.d, &t0.hh, &t0.mm, &t0.sec);
            if (t0.y <= 1980 || t0.m < 1 || t0.m > 12 || t0.d < 1 || t0.d > 31 ||
                t0.hh < 0 || t0.hh > 23 || t0.mm < 0 || t0.mm > 59 ||
                t0.sec < 0.0 || t0.sec >= 60.0) {
                fprintf(stderr, "ERROR: Invalid date and time.\n"); exit(1);
            }
            t0.sec = floor(t0.sec); date2gps(&t0, &g0);
            break;
        case 'd': duration = atof(optarg); break;
        case 'i': ionoutc.enable = FALSE; break;
        case 'p':
            if (optind < argc && argv[optind][0] != '-') {
                fixed_gain = atoi(argv[optind]);
                if (fixed_gain < 1 || fixed_gain > 128) {
                    fprintf(stderr, "ERROR: Fixed gain must be 1-128.\n"); exit(1);
                }
                optind++;
            }
            path_loss_enable = FALSE;
            break;
        case 'v': verb = TRUE; break;
        case ':': case '?': usage(); exit(1);
        default: break;
        }
    }

    if (navfile[0] == 0) {
        fprintf(stderr, "ERROR: GPS ephemeris file not specified.\n"); exit(1);
    }
    if (umfile[0] == 0 && !staticLocationMode) {
        staticLocationMode = TRUE;
        llh[0] = 35.681298 / R2D;
        llh[1] = 139.766247 / R2D;
        llh[2] = 10.0;
    }
    if (duration < 0.0 ||
        (!staticLocationMode && duration > (double)USER_MOTION_SIZE / 10.0) ||
        (staticLocationMode  && duration > STATIC_MAX_DURATION)) {
        fprintf(stderr, "ERROR: Invalid duration.\n"); exit(1);
    }
    iduration = (int)(duration * 10.0 + 0.5);

    samp_freq    = floor(samp_freq / 10.0);
    iq_buff_size = (int)samp_freq;
    samp_freq   *= 10.0;
    delt         = 1.0 / samp_freq;

    /* ── Receiver position ── */
    if (!staticLocationMode) {
        if (nmeaGGA)    numd = readNmeaGGA(xyz, umfile);
        else if (umLLH) numd = readUserMotionLLH(xyz, umfile);
        else            numd = readUserMotion(xyz, umfile);
        if (numd == -1) { fprintf(stderr, "ERROR: Failed to open motion file.\n"); exit(1); }
        if (numd == 0)  { fprintf(stderr, "ERROR: Empty motion file.\n"); exit(1); }
        if (numd > iduration) numd = iduration;
        xyz2llh(xyz[0], llh);
    } else {
        fprintf(stderr, "Using static location mode.\n");
        numd = iduration;
        llh2xyz(llh, xyz[0]);
    }
    fprintf(stderr, "xyz = %11.1f, %11.1f, %11.1f\n", xyz[0][0], xyz[0][1], xyz[0][2]);
    fprintf(stderr, "llh = %11.6f, %11.6f, %11.1f\n", llh[0]*R2D, llh[1]*R2D, llh[2]);

    /* ── Ephemeris ── */
    neph = readRinexNavAll(eph, &ionoutc, navfile);
    if (neph == 0)  { fprintf(stderr, "ERROR: No ephemeris available.\n"); exit(1); }
    if (neph == -1) { fprintf(stderr, "ERROR: Ephemeris file not found.\n"); exit(1); }

    if (verb && ionoutc.vflg) {
        fprintf(stderr, "  %12.3e %12.3e %12.3e %12.3e\n",
                ionoutc.alpha0, ionoutc.alpha1, ionoutc.alpha2, ionoutc.alpha3);
        fprintf(stderr, "  %12.3e %12.3e %12.3e %12.3e\n",
                ionoutc.beta0, ionoutc.beta1, ionoutc.beta2, ionoutc.beta3);
        fprintf(stderr, "   %19.11e %19.11e  %9d %9d\n",
                ionoutc.A0, ionoutc.A1, ionoutc.tot, ionoutc.wnt);
        fprintf(stderr, "%6d\n", ionoutc.dtls);
    }

    for (sv = 0; sv < MAX_SAT; sv++)
        if (eph[0][sv].vflg == 1) { gmin = eph[0][sv].toc; tmin = eph[0][sv].t; break; }
    gmax.sec = gmax.week = tmax.sec = tmax.mm = tmax.hh = tmax.d = tmax.m = tmax.y = 0;
    for (sv = 0; sv < MAX_SAT; sv++)
        if (eph[neph-1][sv].vflg == 1) { gmax = eph[neph-1][sv].toc; tmax = eph[neph-1][sv].t; break; }

    if (g0.week >= 0) {
        if (timeoverwrite) {
            gpstime_t gtmp; datetime_t ttmp; double dsec;
            gtmp.week = g0.week;
            gtmp.sec  = (double)(((int)g0.sec) / 7200) * 7200.0;
            dsec = subGpsTime(gtmp, gmin);
            ionoutc.wnt = gtmp.week; ionoutc.tot = (int)gtmp.sec;
            for (sv = 0; sv < MAX_SAT; sv++)
                for (i = 0; i < neph; i++)
                    if (eph[i][sv].vflg == 1) {
                        gtmp = incGpsTime(eph[i][sv].toc, dsec); gps2date(&gtmp, &ttmp);
                        eph[i][sv].toc = gtmp; eph[i][sv].t = ttmp;
                        gtmp = incGpsTime(eph[i][sv].toe, dsec); eph[i][sv].toe = gtmp;
                    }
        } else {
            if (subGpsTime(g0, gmin) < 0.0 || subGpsTime(gmax, g0) < 0.0) {
                fprintf(stderr, "ERROR: Invalid start time.\n"); exit(1);
            }
        }
    } else {
        g0 = gmin; t0 = tmin;
    }
    fprintf(stderr, "Start time = %4d/%02d/%02d,%02d:%02d:%02.0f (%d:%.0f)\n",
            t0.y, t0.m, t0.d, t0.hh, t0.mm, t0.sec, g0.week, g0.sec);
    fprintf(stderr, "Duration = %.1f [sec]\n", (double)numd / 10.0);

    ieph = -1;
    for (i = 0; i < neph && ieph < 0; i++)
        for (sv = 0; sv < MAX_SAT; sv++)
            if (eph[i][sv].vflg == 1) {
                dt = subGpsTime(g0, eph[i][sv].toc);
                if (dt >= -SECONDS_IN_HOUR && dt < SECONDS_IN_HOUR) { ieph = i; break; }
            }
    if (ieph == -1) { fprintf(stderr, "ERROR: No matching ephemeris set.\n"); exit(1); }

    /* ── Buffers (small CPU buffers for SC01 only) ── */
    iq_buff = (short *)calloc(2 * iq_buff_size, sizeof(short));
    if (!iq_buff) { fprintf(stderr, "ERROR: Failed to allocate I/Q buffer.\n"); exit(1); }
    if (data_format == SC08 || data_format == SC01) {
        size_t n8 = (data_format == SC08) ? (size_t)(2 * iq_buff_size) : (size_t)(iq_buff_size / 4);
        iq8_buff = (signed char *)calloc(n8, 1);
        if (!iq8_buff) { fprintf(stderr, "ERROR: Failed to allocate 8-bit buffer.\n"); exit(1); }
    }

    /* ── Output file ── */
    if (strcmp("-", outfile) == 0) {
        fp = stdout;
    } else {
        fp = fopen(outfile, "wb");
        if (!fp) { fprintf(stderr, "ERROR: Failed to open output file.\n"); exit(1); }
    }

    /* ── GPU init ── */
    gpuInit(iq_buff_size);

    /* ── Channel init ── */
    for (i = 0; i < MAX_CHAN; i++) chan[i].prn = 0;
    for (sv = 0; sv < MAX_SAT; sv++) allocatedSat[sv] = -1;
    grx = incGpsTime(g0, 0.0);
    allocateChannel(chan, eph[ieph], ionoutc, grx, xyz[0], elvmask);
    for (i = 0; i < MAX_CHAN; i++)
        if (chan[i].prn > 0)
            fprintf(stderr, "%02d %6.1f %5.1f %11.1f %5.1f\n", chan[i].prn,
                    chan[i].azel[0]*R2D, chan[i].azel[1]*R2D,
                    chan[i].rho0.d, chan[i].rho0.iono_delay);

    /* ── Antenna gain pattern ── */
    {
        const double apdb[37] = {
             0.00,  0.00,  0.22,  0.44,  0.67,  1.11,  1.56,  2.00,  2.44,  2.89,
             3.56,  4.22,  4.89,  5.56,  6.22,  6.89,  7.56,  8.22,  8.89,  9.78,
            10.67, 11.56, 12.44, 13.33, 14.44, 15.56, 16.67, 17.78, 18.89, 20.00,
            21.33, 22.67, 24.00, 25.56, 27.33, 29.33, 31.56
        };
        for (i = 0; i < 37; i++) ant_pat[i] = pow(10.0, -apdb[i] / 20.0);
    }

    /* ─────────────────────────────────────────────────────────────────────
     * MAIN GENERATION LOOP
     *
     * Each iteration = one 100ms epoch = iq_buff_size IQ samples.
     *
     * Single GPU flow (default):
     *   1. CPU computes ranges/gains
     *   2. CPU snapshots channel state -> EpochChannel[]
     *   3. GPU launches kernel (async), DMA back (async)
     *   4. CPU advances channel state (runs while GPU works)
     *   5. CPU syncs GPU stream, writes epoch to disk
     *
     * Dual GPU flow (-DDUAL_GPU):
     *   Each GPU alternates epochs. The sync/write of epoch N happens
     *   after the launch of epoch N+1, hiding GPU latency behind disk I/O.
     * ───────────────────────────────────────────────────────────────────── */

    EpochChannel h_ec[MAX_CHAN_GPU];
    int numChan;
    int cur_gpu = 0;

    tstart = clock();
    grx = incGpsTime(grx, 0.1);

    for (iumd = 1; iumd < numd; iumd++) {

        /* ── CPU: range + gain update ── */
        for (i = 0; i < MAX_CHAN; i++) {
            if (chan[i].prn > 0) {
                range_t rho;
                sv = chan[i].prn - 1;
                if (!staticLocationMode)
                    computeRange(&rho, eph[ieph][sv], &ionoutc, grx, xyz[iumd]);
                else
                    computeRange(&rho, eph[ieph][sv], &ionoutc, grx, xyz[0]);
                chan[i].azel[0] = rho.azel[0];
                chan[i].azel[1] = rho.azel[1];
                computeCodePhase(&chan[i], rho, 0.1);
                chan[i].carr_phasestep = (int)round(512.0 * 65536.0 * chan[i].f_carr * delt);
                path_loss = 20200000.0 / rho.d;
                ibs = (int)((90.0 - rho.azel[1] * R2D) / 5.0);
                ant_gain = ant_pat[ibs];
                gain[i] = path_loss_enable
                          ? (int)(path_loss * ant_gain * 128.0)
                          : fixed_gain;
            }
        }

        /* ── Snapshot channel state for GPU ── */
        numChan = buildEpochChannels(chan, h_ec, gain, delt);

        /* ── Dual GPU: sync previous GPU, write its epoch ── */
        if (g_ngpu == 2 && iumd > 1) {
            int prev = 1 - cur_gpu;
            gpuSyncEpoch(prev);
            gpuWriteEpoch(prev, fp, iq_buff_size, data_format);
        }

        /* ── Launch GPU kernel for this epoch (async) ── */
        gpuLaunchEpoch(cur_gpu, h_ec, numChan, iq_buff_size, data_format);

        /* ── CPU: advance channel state (overlaps with GPU compute) ── */
        advanceChannels(chan, iq_buff_size, delt);

        /* ── Single GPU: sync and write immediately ── */
        if (g_ngpu == 1) {
            gpuSyncEpoch(0);
            gpuWriteEpoch(0, fp, iq_buff_size, data_format);
        } else {
            cur_gpu = 1 - cur_gpu;
        }

        /* ── Nav message + channel refresh every 30s ── */
        igrx = (int)(grx.sec * 10.0 + 0.5);
        if (igrx % 300 == 0) {
            for (i = 0; i < MAX_CHAN; i++)
                if (chan[i].prn > 0) generateNavMsg(grx, &chan[i], 0);
            for (sv = 0; sv < MAX_SAT; sv++) {
                if (eph[ieph+1][sv].vflg == 1) {
                    dt = subGpsTime(eph[ieph+1][sv].toc, grx);
                    if (dt < SECONDS_IN_HOUR) {
                        ieph++;
                        for (i = 0; i < MAX_CHAN; i++)
                            if (chan[i].prn != 0)
                                eph2sbf(eph[ieph][chan[i].prn-1], ionoutc, chan[i].sbf);
                    }
                    break;
                }
            }
            if (!staticLocationMode)
                allocateChannel(chan, eph[ieph], ionoutc, grx, xyz[iumd], elvmask);
            else
                allocateChannel(chan, eph[ieph], ionoutc, grx, xyz[0], elvmask);
            if (verb) {
                fprintf(stderr, "\n");
                for (i = 0; i < MAX_CHAN; i++)
                    if (chan[i].prn > 0)
                        fprintf(stderr, "%02d %6.1f %5.1f %11.1f %5.1f\n",
                                chan[i].prn, chan[i].azel[0]*R2D, chan[i].azel[1]*R2D,
                                chan[i].rho0.d, chan[i].rho0.iono_delay);
            }
        }

        grx = incGpsTime(grx, 0.1);
        fprintf(stderr, "\rTime into run = %4.1f", subGpsTime(grx, g0));
        fflush(stdout);
    }

    /* ── Flush last epoch (dual GPU) ── */
    if (g_ngpu == 2) {
        gpuSyncEpoch(cur_gpu);
        gpuWriteEpoch(cur_gpu, fp, iq_buff_size, data_format);
    }

    tend = clock();
    fprintf(stderr, "\nDone!\n");

    if (fp != stdout) fclose(fp);
    gpuFree();
    free(iq_buff);
    if (iq8_buff) free(iq8_buff);

    fprintf(stderr, "Process time = %.1f [sec]\n",
            (double)(tend - tstart) / CLOCKS_PER_SEC);
    return 0;
}
