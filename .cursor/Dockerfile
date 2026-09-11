# Base environment for the Raven Academic Writing MCP.
#
# Layers here are the slow, stable system dependencies:
#   - .NET 8 SDK (Microsoft Open XML SDK validation of generated fixtures)
#   - uv (pinned to the version used in CI) for Python dependency management
#   - a pre-provisioned CPython 3.12 toolchain that uv resolves the project with
#
# Repository-specific work (uv sync, dotnet restore) lives in the install phase
# so it tracks the checked-out lockfiles instead of being baked into the image.
FROM mcr.microsoft.com/dotnet/sdk:8.0

ENV DEBIAN_FRONTEND=noninteractive \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    UV_INSTALL_DIR=/usr/local/bin \
    UV_VERSION=0.12.13

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        sudo \
    && rm -rf /var/lib/apt/lists/*

# Cursor Cloud agents run as the "ubuntu" user; create it with passwordless sudo.
RUN useradd --create-home --shell /bin/bash --uid 1000 ubuntu \
    && printf 'ubuntu ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/ubuntu \
    && chmod 0440 /etc/sudoers.d/ubuntu

# Install a pinned uv system-wide so it is on PATH for every phase and shell.
RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh \
    && uv --version

USER ubuntu
WORKDIR /home/ubuntu

# Pre-provision the interpreter the project pins (requires-python >= 3.12).
RUN uv python install 3.12
