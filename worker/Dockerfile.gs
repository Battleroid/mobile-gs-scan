ARG BASE_IMAGE=mobile-gs-scan/base:latest
FROM ${BASE_IMAGE}

# worker-gs is the heavy one — torch + gsplat + nerfstudio + glomap.
# Stub-friendly: the worker code in app/pipeline/* falls back to a
# synthetic "fake training" path when these aren't installed, so the
# rest of the stack stays runnable while the real CUDA install is
# still being shaken out.

# Compute capabilities the CUDA-bound deps (torch ext modules, gsplat,
# Glomap below) get compiled for. Multi-arch keeps the same image
# usable across the common GPU classes:
#   8.0 — A100 (Ampere datacenter)
#   8.6 — RTX 30-series, A6000, etc. (Ampere consumer / pro)
#   8.9 — RTX 40-series (incl. 4090), L4, L40 (Ada Lovelace)
#   9.0 — H100 (Hopper)
# Trade-off: each extra arch ~doubles the gsplat extension's CUDA
# compile time on a fresh build. After the first build the layer is
# cached. To trim build time on a constrained host, override at
# build-time, e.g. `--build-arg TORCH_CUDA_ARCH_LIST=8.9` and the
# matching --build-arg CMAKE_CUDA_ARCHITECTURES=89 below.
ARG TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0"
ENV TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST} \
    CUDA_HOME=/usr/local/cuda

# Pinned to the tested combo as of early 2026:
#   torch 2.4.1 + torchvision 0.19.1 (cu124 wheels)
#   nerfstudio 1.1.5 (latest released)
#   gsplat 1.4.0
#
# cu124 wheels run cleanly on a 12.8 toolkit (CUDA is forward-
# compatible within 12.x), which is why the base image bump to
# 12.8 doesn't force a torch bump. Going past nerfstudio 1.1.5
# requires building from source — there's no 1.1.6+ on PyPI.
RUN python -m pip install --extra-index-url https://download.pytorch.org/whl/cu124 \
        torch==2.4.1 torchvision==0.19.1 && \
    python -m pip install \
        opencv-python-headless==4.10.0.84 \
        open3d==0.18.0 \
        plyfile==1.1 \
        rich==13.9.4 \
        trimesh==4.5.3 \
        tyro==0.9.5 \
        viser==0.2.7 \
        nerfstudio==1.1.5 \
        gsplat==1.4.0

# CUDA architectures for COLMAP + glomap below. Same arch set as
# TORCH_CUDA_ARCH_LIST above, expressed in CMake's semicolon-list
# format (no decimals: "8.9" → "89"). Override via
# --build-arg CMAKE_CUDA_ARCHITECTURES=... when trimming for a
# specific card class. Declared here (above both builds) so the
# COLMAP step can share the same value without duplicating the ARG.
ARG CMAKE_CUDA_ARCHITECTURES="80;86;89;90"

# COLMAP CLI from source. The glomap build below uses
# ``-DFETCH_COLMAP=ON`` to satisfy COLMAP-as-a-library, but that path
# does not install the ``colmap`` binary on ``$PATH``. The current
# pipeline doesn't need it directly, but future surfaces will:
#   * OpenMVS-textured mesh tier — ``colmap model_converter`` to
#     translate our ``sfm/transforms.json`` into a COLMAP workspace.
#   * Ad-hoc debugging from inside the worker shell (manual SfM
#     inspection, MVS workspace fixups, point-cloud sanity passes).
# Pinned at 3.10 (the stable tag closest to the COLMAP that glomap
# 1.0.0 already fetches internally, so the two installs agree on
# library ABI). GUI off to avoid pulling Qt into the image. Built
# BEFORE the glomap step so its layer caches independently — a
# glomap retag doesn't invalidate this and vice-versa.
ARG COLMAP_TAG=3.10
RUN (git clone --depth 1 --branch ${COLMAP_TAG} https://github.com/colmap/colmap.git /tmp/colmap \
        || git clone --depth 1 https://github.com/colmap/colmap.git /tmp/colmap) && \
    cmake -S /tmp/colmap -B /tmp/colmap/build -GNinja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CUDA_ARCHITECTURES="${CMAKE_CUDA_ARCHITECTURES}" \
        -DGUI_ENABLED=OFF && \
    cmake --build /tmp/colmap/build --target install -j $(nproc) && \
    rm -rf /tmp/colmap

# Glomap from source. Apt doesn't carry it; the nerfstudio docker image
# uses the same approach. Try the pinned tag first, fall back to main
# if the tag isn't there (colmap/glomap occasionally retags releases).
#
# -DGUI_ENABLED=OFF skips the COLMAP Qt-based GUI (we're running
# headless on a server) — without it COLMAP would also need
# qtbase5-dev + libqt5opengl5-dev installed in the base image, which
# we'd rather avoid.
ARG GLOMAP_TAG=1.0.0
RUN (git clone --depth 1 --branch ${GLOMAP_TAG} https://github.com/colmap/glomap.git /tmp/glomap \
        || git clone --depth 1 https://github.com/colmap/glomap.git /tmp/glomap) && \
    cmake -S /tmp/glomap -B /tmp/glomap/build -GNinja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CUDA_ARCHITECTURES="${CMAKE_CUDA_ARCHITECTURES}" \
        -DGUI_ENABLED=OFF \
        -DFETCH_COLMAP=ON \
        -DFETCH_POSELIB=ON && \
    cmake --build /tmp/glomap/build --target install -j $(nproc) && \
    rm -rf /tmp/glomap

# OpenMVS — the standard-tier mesh pipeline. Drives a sequence of
# binaries (InterfaceCOLMAP → DensifyPointCloud → ReconstructMesh →
# RefineMesh → TextureMesh) that produces a UV-mapped textured OBJ
# + MTL + JPG bundle. Lands BELOW glomap so the COLMAP-as-library
# linkage that glomap's ``FETCH_COLMAP=ON`` builds is already
# resolved by the time we build OpenMVS against the same headers.
#
# CUDA densifier is DISABLED here (``-DOpenMVS_USE_CUDA=OFF``):
#   * For 100–500 frame phone captures at ≤4K the CPU densifier
#     completes in 5–15 min — within ~2–3× of CUDA but well inside
#     the runtime budget the standard tier targets.
#   * Avoids a second CUDA-arch matrix on the image (the OpenMVS
#     CUDA build is finicky about Torch/cuDNN ABI overlap, which
#     this worker image is already balancing for gsplat).
#   * Reversible — flip the cmake flag if real-world dense times
#     overrun what users tolerate.
#
# BREAKPAD is also disabled — it'd otherwise phone telemetry / crash
# dumps home, which we don't want from a server worker.
#
# VCG is a header-only template library OpenMVS depends on; pinned
# at the 2024.09 tag (the last verified stable interface before
# the 2025 reorg). No build, just a clone-and-leave-on-disk.
ARG VCGLIB_TAG=2024.09
ARG OPENMVS_TAG=v2.3.0
RUN (git clone --depth 1 --branch ${VCGLIB_TAG} https://github.com/cnr-isti-vclab/vcglib.git /opt/vcglib \
        || git clone --depth 1 https://github.com/cnr-isti-vclab/vcglib.git /opt/vcglib) && \
    (git clone --depth 1 --branch ${OPENMVS_TAG} https://github.com/cdcseacave/openMVS.git /tmp/openmvs \
        || git clone --depth 1 https://github.com/cdcseacave/openMVS.git /tmp/openmvs) && \
    cmake -S /tmp/openmvs -B /tmp/openmvs/build -GNinja \
        -DCMAKE_BUILD_TYPE=Release \
        -DOpenMVS_USE_CUDA=OFF \
        -DOpenMVS_USE_BREAKPAD=OFF \
        -DVCG_ROOT=/opt/vcglib \
        -DCMAKE_INSTALL_PREFIX=/usr/local && \
    cmake --build /tmp/openmvs/build --target install -j $(nproc) && \
    rm -rf /tmp/openmvs

# spz tooling — Niantic's compressed splat format. The Python bindings
# are still fragile; we shell out to the upstream CLI instead.
ARG SPZ_TAG=v1.0.1
RUN (git clone --depth 1 --branch ${SPZ_TAG} https://github.com/nianticlabs/spz.git /tmp/spz \
        || git clone --depth 1 https://github.com/nianticlabs/spz.git /tmp/spz) && \
    cmake -S /tmp/spz -B /tmp/spz/build -GNinja -DCMAKE_BUILD_TYPE=Release && \
    cmake --build /tmp/spz/build -j $(nproc) && \
    install -m 0755 /tmp/spz/build/spz_pack /usr/local/bin/spz_pack 2>/dev/null || true && \
    rm -rf /tmp/spz

WORKDIR /app

# Application source goes here, NOT in Dockerfile.base. A code
# change in app/ only invalidates this single COPY layer; the
# torch / nerfstudio / glomap / spz layers above all stay cached.
# Putting COPY app in the base would invalidate downstream FROM
# layers and force the heavy CUDA install to redo every edit.
# Don't move this.
COPY app /app/app

CMD ["python", "-m", "app.worker_main"]
