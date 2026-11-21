/* pisampler-gain: a stereo gain stage between the sound engine and the DAC.
 *
 * JACK client "pisampler-out" with ports in_1/in_2 -> out_1/out_2. The gain is
 * a plain number written as a line of text to the FIFO given as argv[1]
 * (e.g. "1.5\n"); changes are ramped over one audio period so they never click.
 * It runs inside the JACK graph, so it adds no latency.
 *
 * Build: gcc -O2 -o pisampler-gain pisampler-gain.c -ljack
 */
#include <fcntl.h>
#include <jack/jack.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

static jack_port_t *in[2], *out[2];
static _Atomic float target = 1.0f;
static float current = 1.0f;

static int process(jack_nframes_t n, void *arg)
{
    (void)arg;
    const float goal = atomic_load(&target);
    const float step = n ? (goal - current) / (float)n : 0.0f;
    const float *i0 = jack_port_get_buffer(in[0], n), *i1 = jack_port_get_buffer(in[1], n);
    float *o0 = jack_port_get_buffer(out[0], n), *o1 = jack_port_get_buffer(out[1], n);
    float g = current;
    for (jack_nframes_t k = 0; k < n; k++) {
        g += step;
        o0[k] = i0[k] * g;
        o1[k] = i1[k] * g;
    }
    current = goal;
    return 0;
}

static void on_shutdown(void *arg)
{
    (void)arg;
    _exit(1);                    /* JACK went away: let systemd restart us */
}

int main(int argc, char **argv)
{
    const char *fifo = argc > 1 ? argv[1] : "/run/pisampler/gain";
    jack_client_t *c = jack_client_open("pisampler-out", JackNoStartServer, NULL);
    if (!c) {
        fprintf(stderr, "pisampler-gain: JACK is not running\n");
        return 1;
    }
    in[0] = jack_port_register(c, "in_1", JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, 0);
    in[1] = jack_port_register(c, "in_2", JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, 0);
    out[0] = jack_port_register(c, "out_1", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    out[1] = jack_port_register(c, "out_2", JACK_DEFAULT_AUDIO_TYPE, JackPortIsOutput, 0);
    jack_set_process_callback(c, process, NULL);
    jack_on_shutdown(c, on_shutdown, NULL);
    if (!in[0] || !in[1] || !out[0] || !out[1] || jack_activate(c)) {
        fprintf(stderr, "pisampler-gain: cannot set up JACK ports\n");
        return 1;
    }

    unlink(fifo);
    if (mkfifo(fifo, 0660) != 0) {
        perror("pisampler-gain: mkfifo");
        return 1;
    }
    int fd = open(fifo, O_RDWR);         /* read+write: never sees end-of-file */
    FILE *f = fd >= 0 ? fdopen(fd, "r") : NULL;
    if (!f) {
        perror("pisampler-gain: open fifo");
        return 1;
    }
    char line[64];
    while (fgets(line, sizeof line, f)) {
        float g = strtof(line, NULL);
        if (g >= 0.0f && g <= 32.0f) {   /* up to +30 dB */
            atomic_store(&target, g);
            printf("gain %.3f\n", g);
            fflush(stdout);
        }
    }
    return 0;
}
