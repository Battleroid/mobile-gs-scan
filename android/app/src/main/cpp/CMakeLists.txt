cmake_minimum_required(VERSION 3.22.1)
project(pebble_native LANGUAGES CXX)

# Single-translation-unit shared library exposing one JNI entry
# point: ``Java_dev_battleroid_mobilegsscan_quality_NativeKernels_analyze``.
# Kept small so the cross-arch APK overhead is bounded — the .so
# weighs <30 KB per ABI even with debug symbols.
add_library(pebble_native SHARED frame_quality.cpp)

target_compile_features(pebble_native PRIVATE cxx_std_17)
target_compile_options(pebble_native PRIVATE
    -Wall
    -Wextra
    -Wno-unused-parameter
    -O3
    -ffast-math
    -fvisibility=hidden
)

# Standard NDK system libs. ``log`` is for debug __android_log_print
# during development; ``android`` for ALOG / asset surfaces if we
# ever need them.
find_library(log-lib log)
target_link_libraries(pebble_native PRIVATE ${log-lib})
