// Set macOS default audio output device by name.
// Build: cc scripts/set_audio_output.c -framework CoreAudio -framework CoreFoundation -o runtime/set_audio_output
#include <CoreAudio/CoreAudio.h>
#include <CoreFoundation/CoreFoundation.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

static void lower_ascii(const char *in, char *out, size_t n) {
    size_t i;
    for (i = 0; i + 1 < n && in[i]; i++) out[i] = (char)tolower((unsigned char)in[i]);
    out[i] = 0;
}

static int get_name(AudioDeviceID id, char *buf, size_t n) {
    CFStringRef s = NULL;
    UInt32 size = sizeof(s);
    AudioObjectPropertyAddress a = {
        kAudioObjectPropertyName,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain
    };
    if (AudioObjectGetPropertyData(id, &a, 0, NULL, &size, &s) != noErr || !s) return 0;
    Boolean ok = CFStringGetCString(s, buf, n, kCFStringEncodingUTF8);
    CFRelease(s);
    return ok;
}

static UInt32 output_channels(AudioDeviceID id) {
    AudioObjectPropertyAddress a = {
        kAudioDevicePropertyStreamConfiguration,
        kAudioDevicePropertyScopeOutput,
        kAudioObjectPropertyElementMain
    };
    UInt32 size = 0;
    if (AudioObjectGetPropertyDataSize(id, &a, 0, NULL, &size) != noErr || size == 0) return 0;
    AudioBufferList *bl = (AudioBufferList *)malloc(size);
    if (!bl) return 0;
    if (AudioObjectGetPropertyData(id, &a, 0, NULL, &size, bl) != noErr) {
        free(bl);
        return 0;
    }
    UInt32 channels = 0;
    for (UInt32 i = 0; i < bl->mNumberBuffers; i++) channels += bl->mBuffers[i].mNumberChannels;
    free(bl);
    return channels;
}

static AudioDeviceID current_default_output(void) {
    AudioDeviceID id = 0;
    UInt32 size = sizeof(id);
    AudioObjectPropertyAddress a = {
        kAudioHardwarePropertyDefaultOutputDevice,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain
    };
    if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, 0, NULL, &size, &id) != noErr) return 0;
    return id;
}

int main(int argc, char **argv) {
    AudioObjectPropertyAddress a = {
        kAudioHardwarePropertyDevices,
        kAudioObjectPropertyScopeGlobal,
        kAudioObjectPropertyElementMain
    };
    UInt32 size = 0;
    OSStatus st = AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, &a, 0, NULL, &size);
    if (st != noErr) {
        fprintf(stderr, "cannot get audio devices: %d\n", (int)st);
        return 1;
    }
    int count = (int)(size / sizeof(AudioDeviceID));
    AudioDeviceID *ids = (AudioDeviceID *)malloc(size);
    if (!ids) return 1;
    st = AudioObjectGetPropertyData(kAudioObjectSystemObject, &a, 0, NULL, &size, ids);
    if (st != noErr) {
        fprintf(stderr, "cannot read audio devices: %d\n", (int)st);
        free(ids);
        return 1;
    }

    AudioDeviceID current = current_default_output();
    if (argc < 2 || strcmp(argv[1], "--list") == 0) {
        for (int i = 0; i < count; i++) {
            char name[512];
            UInt32 channels = output_channels(ids[i]);
            if (channels && get_name(ids[i], name, sizeof(name))) {
                printf("%u\t%s\tchannels=%u%s\n", ids[i], name, channels, ids[i] == current ? "\tdefault" : "");
            }
        }
        free(ids);
        return 0;
    }

    char query[1024] = "";
    for (int i = 1; i < argc; i++) {
        if (i > 1) strncat(query, " ", sizeof(query) - strlen(query) - 1);
        strncat(query, argv[i], sizeof(query) - strlen(query) - 1);
    }
    char query_lower[1024];
    lower_ascii(query, query_lower, sizeof(query_lower));

    AudioDeviceID match = 0;
    char match_name[512] = "";
    for (int pass = 0; pass < 2 && !match; pass++) {
        for (int i = 0; i < count; i++) {
            char name[512], name_lower[512];
            if (!output_channels(ids[i]) || !get_name(ids[i], name, sizeof(name))) continue;
            lower_ascii(name, name_lower, sizeof(name_lower));
            if ((pass == 0 && strcmp(name_lower, query_lower) == 0) ||
                (pass == 1 && (strstr(name_lower, query_lower) || strstr(query_lower, name_lower)))) {
                match = ids[i];
                strncpy(match_name, name, sizeof(match_name) - 1);
                break;
            }
        }
    }

    if (!match) {
        fprintf(stderr, "audio output device not found: %s. Available:", query);
        for (int i = 0; i < count; i++) {
            char name[512];
            if (output_channels(ids[i]) && get_name(ids[i], name, sizeof(name))) fprintf(stderr, " %s;", name);
        }
        fprintf(stderr, "\n");
        free(ids);
        return 2;
    }

    AudioObjectPropertySelector selectors[2] = {
        kAudioHardwarePropertyDefaultOutputDevice,
        kAudioHardwarePropertyDefaultSystemOutputDevice
    };
    for (int i = 0; i < 2; i++) {
        AudioObjectPropertyAddress set_addr = {
            selectors[i],
            kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain
        };
        AudioDeviceID value = match;
        UInt32 value_size = sizeof(value);
        st = AudioObjectSetPropertyData(kAudioObjectSystemObject, &set_addr, 0, NULL, value_size, &value);
        if (st != noErr) {
            fprintf(stderr, "failed to set %s: %d\n", match_name, (int)st);
            free(ids);
            return 1;
        }
    }
    printf("default output set to %s\n", match_name);
    free(ids);
    return 0;
}
