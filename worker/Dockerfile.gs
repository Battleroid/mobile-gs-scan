ARG BASE_IMAGE=mobile-gs-scan/base:latest
FROM ${BASE_IMAGE}

# worker-gs is the heavy one — torch + gsplat + nerfstudio + glomap.
# Stub-friendly: the worker code in app/pipeline/* falls back to a
# synthetic "fake training" path when these aren't installed, so the
# rest of the stack stays runnable while the real CUDA install is
# still being shaken out.

ENV TORCH_CUDA_ARCH_LIST="8.9" \
    CUDA_HOME=/usr/local/cuda

# Torch matched to the cuDNN-devel cuda 12.4 base. nerfstudio pulls in
# tinycudann, gsplat, splatfacto pipelines, and the splat exporter.
# Pinned to a known-good combo as of early 2026; revisit when nerfstudio
# bumps its torch requirement.
RUN python -m pip install --extra-index-url https://download.pytorch.org/whl/cu124 \
        torch==2.4.1 torchvision==0.19.1 && \
    python -m pip install \
        opencv-python-headless==4.10.0.84 \
        open3d==0.18.0 \
        plyfile==1.1 \
        rich==13.9.4 \
        tyro==0.9.5 \
        viser==0.2.7 \
        nerfstudio==1.1.5 \
        gsplat==1.4.0

# Glomap from source. Apt doesn't carry it; the nerfstudio docker image
# uses the same approach. Cloning a pinned tag keeps reproducibility.
ARG GLOMAP_TAG=1.0.0
RUN git clone --depth 1 --branch ${GLOMAP_TAG} https://github.com/colmap/glomap.git /tmp/glomap && \
    cmake -S /tmp/glomap -B /tmp/glomap/build -GNinja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_CUDA_ARCHITECTURES=89 \
        -DFETCH_COLMAP=ON \
        -DFETCH_POSELIB=ON && \
    cmake --build /tmp/glomap/build --target install -j $(nproc) && \
    rm -rf /tmp/glomap

# spz tooling — Niantic's compressed splat format. The Python bindings
# are still fragile; we shell out to the upstream CLI instead.
ARG SPZ_TAG=v1.0.1
RUN git clone --depth 1 --branch ${SPZ_TAG} https://github.com/nianticlabs/spz.git /tmp/spz || \
    git clone --depth 1 https://github.com/nianticlabs/spz.git /tmp/spz && \
    cmake -S /tmp/spz -B /tmp/spz/build -GNinja -DCMAKE_BUILD_TYPE=Release && \
    cmake --build /tmp/spz/build -j $(nproc) && \
    install -m 0755 /tmp/spz/build/spz_pack /usr/local/bin/spz_pack 2>/dev/null || true && \
    rm -rf /tmp/spz

WORKDIR /app

CMD ["python", "-m", "app.worker_main"]
