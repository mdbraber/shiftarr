FROM python:3.12-slim

# ffmpeg for extraction/decoding. Try installing ffsubsync from wheels (no compiler);
# fall back to a minimal gcc only if a source build of webrtcvad is needed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir ffsubsync

COPY shiftarr.py plex.py server.py allowlist.txt /app/
WORKDIR /app

ENV SHIFTARR_ALLOWLIST=/app/allowlist.txt \
    SHIFTARR_LANG=eng \
    SHIFTARR_TAG=en \
    SHIFTARR_LOG=/config/shiftarr.log \
    PORT=8000

EXPOSE 8000
CMD ["python", "-u", "server.py"]
