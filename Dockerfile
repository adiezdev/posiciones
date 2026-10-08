# ---------------------------------------------------------------------------
# Etapa 1: compilar Audiveris, el motor que lee la partitura del PDF.
# La primera construcción tarda mucho: descarga y compila un proyecto Java.
# Si falla, comprueba que el tag existe en:
#   https://github.com/Audiveris/audiveris/releases
# ---------------------------------------------------------------------------
FROM eclipse-temurin:25-jdk-jammy AS audiveris

ARG AUDIVERIS_REF=5.10.2

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch "${AUDIVERIS_REF}" \
        https://github.com/Audiveris/audiveris.git .
RUN ./gradlew --no-daemon installDist

# El proyecto tiene submódulos, así que la distribución no queda siempre en el
# mismo sitio ni el lanzador se llama siempre igual. La buscamos y dejamos un
# enlace con nombre fijo para que el resto del Dockerfile no dependa de eso.
RUN set -eu; \
    BIN="$(find /src -type d -path '*/build/install/*/bin' | head -n1)"; \
    test -n "$BIN" || { echo "No se encontró la distribución compilada"; exit 1; }; \
    DIST="$(dirname "$BIN")"; \
    echo "Distribución encontrada en: $DIST"; \
    cp -a "$DIST" /opt/audiveris-dist; \
    LANZADOR="$(find /opt/audiveris-dist/bin -maxdepth 1 -type f ! -name '*.bat' | head -n1)"; \
    test -n "$LANZADOR" || { echo "No se encontró el ejecutable"; exit 1; }; \
    chmod +x "$LANZADOR"; \
    ln -sf "$(basename "$LANZADOR")" /opt/audiveris-dist/bin/audiveris-cli; \
    echo "Contenido de bin:"; ls -1 /opt/audiveris-dist/bin

# ---------------------------------------------------------------------------
# Etapa 2: la aplicación
# ---------------------------------------------------------------------------
FROM eclipse-temurin:25-jre-jammy

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        musescore3 \
        xvfb \
        libgl1 libegl1 libxkbcommon0 libasound2 \
        fonts-freefont-ttf fonts-dejavu-core \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa \
        python3 python3-pip \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=audiveris /opt/audiveris-dist /opt/audiveris

ENV AUDIVERIS_BIN=/opt/audiveris/bin/audiveris-cli \
    MSCORE_BIN=mscore3 \
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/4.00/tessdata \
    JAVA_TOOL_OPTIONS=-Djava.awt.headless=true \
    QT_QPA_PLATFORM=offscreen \
    XDG_RUNTIME_DIR=/tmp \
    DATOS_DIR=/datos \
    PYTHONUNBUFFERED=1

RUN mkdir -p /datos

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY posiciones.py anotar.py validar.py partitura.py estilo.py \
     superponer.py api.py ./
COPY static ./static

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=40s \
    CMD curl -fsS http://localhost:8000/salud || exit 1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
