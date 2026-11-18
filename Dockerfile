# Unstructured Data Profiler — demo console, for Cloud Run.
#
# 3.12 rather than 3.14: every dependency here ships prebuilt wheels for it, so
# the image builds without a compiler toolchain.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so application edits don't invalidate the install layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY estate_profiler/ ./estate_profiler/
COPY demo/ ./demo/
COPY tools/ ./tools/

# Bake the synthetic estate into the image. A hosted container has no file
# share to point at, so this is what makes the demo self-sufficient: the
# bundled estate is scannable the moment the service starts.
RUN python tools/make_sample_estate.py sample_estate

# Cloud Run's filesystem is in-memory and resets on restart; /tmp is the
# conventional scratch space. Run history is therefore ephemeral by design —
# BigQuery is the durable record if a run needs to outlive the instance.
ENV PROFILER_RUN_ROOT=/tmp/runs \
    PROFILER_UPLOAD_DIR=/tmp/uploads \
    HOST=0.0.0.0 \
    PORT=8080

# Drop privileges: nothing here needs root, and /tmp stays writable.
RUN useradd --create-home --uid 1000 profiler \
    && chown -R profiler:profiler /app
USER profiler

EXPOSE 8080
CMD ["python", "demo/server.py"]
