#!/usr/bin/env bash
# Cross-compiles the edge264-mvc decoder for Windows x86-64 from Linux (#105):
# edge264mvc.2.dll and edge264mvc_test.exe, at the commit install.sh pins, so
# that Windows decodes 3D with the same version as the native install and the
# .deb. MinGW's runtime and winpthreads are linked in statically (edge264's
# Makefile does it for OS=windows): the two files need nothing else. edge264's
# BSD licence goes with them, as in the .deb.
#   scripts/build-edge264-windows.sh [output dir]      default: dist/windows
# Needs MinGW-w64 GCC with POSIX threads (edge264 uses pthreads), make and git:
#   sudo apt install gcc-mingw-w64-x86-64-posix make git
set -euo pipefail

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
OUTPUT_DIR=${1:-"$REPO_DIR/dist/windows"}
# not CC/STRIP: a generic CC=gcc in the shell would build for Linux
MINGW_CC=${MINGW_CC:-x86_64-w64-mingw32-gcc-posix}
MINGW_STRIP=${MINGW_STRIP:-x86_64-w64-mingw32-strip}

for command in "$MINGW_CC" "$MINGW_STRIP" make git; do
    command -v "$command" >/dev/null || {
        echo "Missing build tool: $command (sudo apt install gcc-mingw-w64-x86-64-posix make git)" >&2
        exit 1
    }
done

# the repository and commit pinned in install.sh: one decoder version everywhere
EDGE264_REPO=$(sed -n 's/^EDGE264_REPO=//p' "$REPO_DIR/scripts/install.sh")
EDGE264_COMMIT=$(sed -n 's/^EDGE264_COMMIT=//p' "$REPO_DIR/scripts/install.sh")
[ -n "$EDGE264_REPO" ] && [ -n "$EDGE264_COMMIT" ] || {
    echo "EDGE264_REPO/EDGE264_COMMIT not found in scripts/install.sh" >&2
    exit 1
}

build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
git clone --quiet "$EDGE264_REPO" "$build/edge264"
git -C "$build/edge264" checkout --quiet "$EDGE264_COMMIT"
# portable, as the .deb: baseline x86-64, faster variants picked at run time
make -C "$build/edge264" -j"$(nproc)" V=no OS=windows CC="$MINGW_CC" \
    CFLAGS=-march=x86-64 VARIANTS=x86-64-v2,x86-64-v3,logs
"$MINGW_STRIP" --strip-unneeded "$build/edge264/edge264mvc_test.exe" "$build/edge264/edge264mvc.2.dll"

install -d "$OUTPUT_DIR"
install -m 755 "$build/edge264/edge264mvc_test.exe" "$OUTPUT_DIR/"
install -m 644 "$build/edge264/edge264mvc.2.dll" "$build/edge264/LICENSE_BSD.txt" "$OUTPUT_DIR/"
echo "$EDGE264_COMMIT" > "$OUTPUT_DIR/edge264.commit"
echo "edge264-mvc ${EDGE264_COMMIT:0:12} for Windows in $OUTPUT_DIR"
