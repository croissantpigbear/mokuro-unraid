# mokuro with NVIDIA GPU support + web UI
# Base image ships PyTorch 2.6 built against CUDA 12.4 (needs NVIDIA driver >= 550).
# torch >= 2.6 is required: transformers refuses to load the OCR model on older versions.
FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

ARG MOKURO_VERSION=0.2.5
ARG GIT_SHA=dev

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/config \
    HF_HOME=/config/.cache/huggingface \
    MANGA_DIR=/manga \
    WEBUI_PORT=8090 \
    WATCH=true \
    SCAN_INTERVAL=300 \
    SETTLE_SECONDS=120 \
    FORCE_CPU=false \
    EXTRA_ARGS="" \
    IMAGE_VERSION=${GIT_SHA}

# OpenCV needs these shared libs
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# Install mokuro (torch is already present so pip keeps the CUDA build) and the web UI deps.
# transformers 5.x needs torch >= 2.6; manga-ocr was built against 4.x, so stay on 4.x.
RUN pip install "mokuro==${MOKURO_VERSION}" "transformers>=4.45,<5" \
      "fastapi>=0.110" "uvicorn>=0.29" "python-multipart>=0.0.9" \
 # fail the build if pip swapped out the CUDA build of torch or anything is missing
 && python -c "import torch, mokuro, fastapi, uvicorn, multipart; assert torch.version.cuda, 'torch has no CUDA'; print('torch', torch.__version__, 'cuda', torch.version.cuda)" \
 # fail the build if transformers can't use this torch (the error users saw with transformers 5)
 && python -c "import transformers; from transformers.utils import is_torch_available; assert is_torch_available(), 'transformers cannot use torch'; print('transformers', transformers.__version__)" \
 # fail the build if mokuro rejects any option the web UI passes to it
 && mokuro --version --disable_confirmation=True --force_cpu=True --no_cache=True --legacy_html=False

COPY app.py /app/app.py
COPY static /app/static

VOLUME ["/manga", "/config"]
EXPOSE 8090
WORKDIR /manga

HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD python -c "import os,urllib.request as u;u.urlopen(f'http://127.0.0.1:{os.environ[\"WEBUI_PORT\"]}/healthz',timeout=4)" || exit 1

ENTRYPOINT ["python", "/app/app.py"]
