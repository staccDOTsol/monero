#!/usr/bin/env bash
# Build librandomx as a shared library from the repo's external/randomx submodule
# (or a given source dir) and drop it in ./lib so xmrfun_stratum/randomx.py can
# load it via ctypes.
#
#   git submodule update --init external/randomx
#   ./build_randomx.sh                # uses ../external/randomx
#   ./build_randomx.sh /path/to/RandomX
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${1:-$HERE/../external/randomx}"
if [ ! -f "$SRC/CMakeLists.txt" ]; then
  echo "RandomX source not found at $SRC (git submodule update --init external/randomx)" >&2
  exit 1
fi
BUILD="${RANDOMX_BUILD_DIR:-$HERE/build/randomx}"
cmake -S "$SRC" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
      -DCMAKE_POSITION_INDEPENDENT_CODE=ON >/dev/null
cmake --build "$BUILD" --target randomx -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
mkdir -p "$HERE/lib"
for f in "$BUILD"/librandomx.so* "$BUILD"/librandomx*.dylib; do
  [ -e "$f" ] && cp -fP "$f" "$HERE/lib/"
done
ls -l "$HERE/lib"
