/* Compatibility helpers used only by SmartVoice's MSYS2 Windows build. */
#ifndef SMARTVOICE_QWEN_WINDOWS_COMPAT_H
#define SMARTVOICE_QWEN_WINDOWS_COMPAT_H

#if defined(SMARTVOICE_QWEN_MSYS)
#include <string.h>
#include <unistd.h>

static inline unsigned char smartvoice_ascii_lower(unsigned char c) {
    return c >= 'A' && c <= 'Z' ? (unsigned char)(c + ('a' - 'A')) : c;
}

static inline char *smartvoice_strcasestr(const char *haystack, const char *needle) {
    if (!*needle) return (char *)haystack;
    for (; *haystack; ++haystack) {
        const char *a = haystack;
        const char *b = needle;
        while (*a && *b && smartvoice_ascii_lower((unsigned char)*a) ==
                                  smartvoice_ascii_lower((unsigned char)*b)) {
            ++a;
            ++b;
        }
        if (!*b) return (char *)haystack;
    }
    return NULL;
}

#define strcasestr smartvoice_strcasestr
#endif

#endif
