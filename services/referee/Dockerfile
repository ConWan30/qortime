# QorTime official referee service (verified uploads). Build context = repo root.
# The signing key is NOT in the image: it comes from the QORTIME_REFEREE_SEED service variable at runtime.
FROM python:3.12-slim
# Must equal schema/contracts/compat.json referee_image.qorcap_sidecar_sha (tests/test_contracts.py checks).
ARG QORCAP_SHA=1680734b3ffbff3ee0d15c39fbbf702ab931ea91
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY qor_receipt ./qor_receipt
RUN pip install --no-cache-dir ".[referee,vision]"
# Pinned QorCap sidecar = the exact scorebug + end-of-game-menu readers QorCap ships (fail-closed if missing),
# including the Monday, Sunday and Thursday Night (madden27_{monday,sunday,thursday}_night) packs only QorCap carries.
RUN curl -fsSL "https://github.com/ConWan30/qorcap/archive/${QORCAP_SHA}.tar.gz" | tar -xz -C /opt \
 && mv "/opt/qorcap-${QORCAP_SHA}/sidecar" /opt/qorcap-sidecar && rm -rf "/opt/qorcap-${QORCAP_SHA}" \
 && test -f /opt/qorcap-sidecar/qorcap_reader.py && test -f /opt/qorcap-sidecar/qorcap_mnp.py \
 && test -f /opt/qorcap-sidecar/packs/madden27_monday_night_v1.npz \
 && test -f /opt/qorcap-sidecar/qorcap_snp.py && test -f /opt/qorcap-sidecar/packs/madden27_sunday_night_v1.npz \
 && test -f /opt/qorcap-sidecar/qorcap_tnp.py && test -f /opt/qorcap-sidecar/packs/madden27_thursday_night_v1.npz \
 && test -f /opt/qorcap-sidecar/packs/madden27_end_menu_v2.npz \
 && test -f /opt/qorcap-sidecar/qorcap_standard.py && test -f /opt/qorcap-sidecar/packs/madden27_standard_score_110028.npz \
 && test -f /opt/qorcap-sidecar/packs/madden27_standard_score_084523.npz
ENV QORCAP_SIDECAR=/opt/qorcap-sidecar \
    QORTIME_REFEREE_WORKDIR=/tmp/qortime-referee \
    PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["sh", "-c", "uvicorn qor_receipt.referee_service.app:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive 75"]
