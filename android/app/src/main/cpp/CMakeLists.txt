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

# 16 KB page-size alignment. Android 15 (API 35) added a runtime
# warning for apps whose native libs aren't aligned to 16 KB
# LOAD segments — the OS is moving toward 16 KB page-size devices
# (Pixel 8+ in the dev preview, broader rollout planned). NDK r27
# enabled this by default at the toolchain level, but only when
# the project explicitly opts in via this linker flag; without
# it the LOAD segments still land at the legacy 4 KB boundary
# and the user sees an "Android App Compatibility" dialog at
# launch listing libpebble_native.so. Setting it here makes the
# build forward-compatible without bumping minSdk.
target_link_options(pebble_native PRIVATE
    "-Wl,-z,max-page-size=16384"
    "-Wl,-z,common-page-size=16384"
)

# Standard NDK system libs. ``log`` is for debug __android_log_print
# during development; ``android`` for ALOG / asset surfaces if we
# ever need them.
find_library(log-lib log)
target_link_libraries(pebble_native PRIVATE ${log-lib})
