# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv

FROM node:22.23.1-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS node

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS python-builder

ARG DEPENDENCY_LOCK_SHA256=edf2fe6005e6bfe70cfb48d08b00eb0f3503d6c524fda01a65cfd2cb45afdadc
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY adapter/pyproject.toml adapter/pyproject.toml
COPY adapter/src adapter/src
COPY runtime/pyproject.toml runtime/pyproject.toml
COPY runtime/src runtime/src
COPY infrastructure/functions/control/pyproject.toml control/pyproject.toml
COPY infrastructure/functions/persistence/pyproject.toml persistence/pyproject.toml
COPY infrastructure/functions/persistence/src persistence/src
COPY tools/headless-client/pyproject.toml headless-client/pyproject.toml
RUN echo "${DEPENDENCY_LOCK_SHA256}  uv.lock" | sha256sum --check \
    && uv export --frozen --no-dev --no-emit-workspace \
      --package kirocrew-agentcore-runtime \
      --package kirocrew-agentcore-adapter \
      --package kirocrew-agentcore-persistence \
      --format requirements-txt --output-file /requirements.lock \
    && python -m pip wheel --require-hashes --wheel-dir /wheels \
      --requirement /requirements.lock \
    && python -m pip wheel --no-deps --wheel-dir /wheels ./adapter ./runtime ./persistence

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS kiro-cli-builder
ARG TARGETARCH
ARG KIRO_CLI_VERSION=2.18.1
ARG KIRO_CLI_AMD64_SHA256=4c2b991e617ba15663fff8d0931e486a855fd16db9a57ddb1694105912a4ffc8
ARG KIRO_CLI_ARM64_SHA256=4b8ad2c0d0e50f285827bcec95abf628e4ad704952a8d8d82cb353155e40365e
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl xz-utils \
    && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) artifact="kirocli-x86_64-linux-musl.tar.xz"; digest="$KIRO_CLI_AMD64_SHA256" ;; \
      arm64) artifact="kirocli-aarch64-linux-musl.tar.xz"; digest="$KIRO_CLI_ARM64_SHA256" ;; \
      *) echo "Unsupported target architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    url="https://prod.download.cli.kiro.dev/stable/${KIRO_CLI_VERSION}/${artifact}"; \
    curl --fail --silent --show-error --location "$url" --output /tmp/kiro-cli.tar.xz; \
    echo "$digest  /tmp/kiro-cli.tar.xz" | sha256sum --check; \
    mkdir -p /tmp/kiro-cli /opt/kiro-cli-home; \
    tar --extract --xz --file /tmp/kiro-cli.tar.xz --directory /tmp/kiro-cli; \
    HOME=/opt/kiro-cli-home KIRO_CLI_SKIP_SETUP=1 sh /tmp/kiro-cli/kirocli/install.sh; \
    /opt/kiro-cli-home/.local/bin/kiro-cli --version | grep --fixed-strings "${KIRO_CLI_VERSION}"

FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7 AS runtime
ARG SOURCE_REVISION=uncommitted
ARG KIROCREW_VERSION=0.2.0
ARG KIROCREW_ARTIFACT_SHA256=329b4b2e271d1253eb9b2115f19950485181934f2ba7ded1a993edfbf48c6f90
ARG KIRO_CLI_VERSION=2.18.1
ARG NODE_VERSION=22.23.1
ARG NPM_VERSION=10.9.8
ARG UV_VERSION=0.12.5
ARG PROTOCOL_VERSION=kirocrew-agentcore.v1
ARG BASE_IMAGE_DIGEST=sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
ARG DEPENDENCY_LOCK_SHA256=edf2fe6005e6bfe70cfb48d08b00eb0f3503d6c524fda01a65cfd2cb45afdadc
LABEL org.opencontainers.image.title="kirocrew-agentcore-runtime" \
      org.opencontainers.image.description="Unmodified KiroCrew on Amazon Bedrock AgentCore Runtime" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${KIROCREW_VERSION}" \
      org.opencontainers.image.base.digest="${BASE_IMAGE_DIGEST}" \
      dev.kirocrew.agentcore.protocol="${PROTOCOL_VERSION}" \
      dev.kirocrew.agentcore.kirocrew.artifact.sha256="${KIROCREW_ARTIFACT_SHA256}" \
      dev.kirocrew.agentcore.kiro-cli.version="${KIRO_CLI_VERSION}" \
      dev.kirocrew.agentcore.toolchain.node="${NODE_VERSION}" \
      dev.kirocrew.agentcore.toolchain.npm="${NPM_VERSION}" \
      dev.kirocrew.agentcore.toolchain.uv="${UV_VERSION}" \
      dev.kirocrew.agentcore.dependency-lock.sha256="${DEPENDENCY_LOCK_SHA256}"

# A practical in-sandbox development toolchain. Keep large language SDKs and
# databases project-local so the runtime stays below AgentCore's 2 GB image cap.
RUN apt-get update \
    && apt-get upgrade --yes --no-install-recommends \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        fd-find \
        file \
        gh \
        git \
        git-lfs \
        jq \
        less \
        nano \
        netcat-openbsd \
        openssh-client \
        pkg-config \
        procps \
        ripgrep \
        rsync \
        tini=0.19.0-1+b3 \
        tree \
        unzip \
        vim-tiny \
        xz-utils \
        zip \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && git lfs install --system \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /mnt/workspace/home --no-create-home app \
    && mkdir -p /opt/app /mnt/workspace/home /mnt/workspace/projects/default \
    && chown -R 10001:10001 /mnt/workspace
COPY --from=python-builder /wheels /wheels
RUN python -m pip install --no-index --no-deps /wheels/*.whl \
    && find /usr/local/lib/python3.12/site-packages -type f -name '*.pyc' -delete \
    && rm -rf /wheels
COPY --from=kiro-cli-builder /opt/kiro-cli-home/.local/bin/kiro-cli /usr/local/bin/kiro-cli
COPY --from=kiro-cli-builder /opt/kiro-cli-home/.local/bin/kiro-cli-chat /usr/local/bin/kiro-cli-chat
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN set -eux; \
    ln -sfn ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm; \
    ln -sfn ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx; \
    ln -sfn ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack; \
    test "$(node --version)" = "v${NODE_VERSION}"; \
    test "$(npm --version)" = "${NPM_VERSION}"; \
    uv --version | grep --extended-regexp --quiet "^uv ${UV_VERSION}( |$)"

ENV HOME=/mnt/workspace/home \
    SHELL=/bin/bash \
    KIROCREW_HOME=/mnt/workspace/home/.kiro/crew \
    KIROCREW_PORT=5476 \
    KIROCREW_HOST=127.0.0.1 \
    PROJECT_ROOT=/mnt/workspace/projects/default \
    KIROCREW_VERSION=${KIROCREW_VERSION} \
    KIROCREW_ARTIFACT_SHA256=${KIROCREW_ARTIFACT_SHA256} \
    KIRO_CLI_VERSION=${KIRO_CLI_VERSION} \
    KIROCREW_AGENTCORE_PROTOCOL=${PROTOCOL_VERSION} \
    KIROCREW_BASE_IMAGE_DIGEST=${BASE_IMAGE_DIGEST} \
    KIROCREW_DEPENDENCY_LOCK_SHA256=${DEPENDENCY_LOCK_SHA256} \
    KIROCREW_SOURCE_REVISION=${SOURCE_REVISION} \
    PATH=/mnt/workspace/home/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /mnt/workspace/projects/default
USER 10001:10001
EXPOSE 8080
VOLUME ["/mnt/workspace"]
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "kirocrew_agentcore_runtime"]
CMD ["serve"]
