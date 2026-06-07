# docling-graph runtime image (CLI/library) for on-demand use via oc exec / Jobs.
FROM registry.access.redhat.com/ubi9/python-312:latest
USER 0
ENV PIP_NO_CACHE_DIR=1 HF_HOME=/opt/app-root/src/.cache/hf
# torch CPU wheels to avoid CUDA bulk
RUN pip install --upgrade pip && \
    pip install torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install docling-graph && \
    python -m spacy download en_core_web_sm || true
USER 1001
WORKDIR /work
CMD ["sleep", "infinity"]
