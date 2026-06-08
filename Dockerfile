# syntax=docker/dockerfile:1.6
#
# mcp-python-interpreter — streamable-HTTP build for fork-publish via GHCR.
#
# Defaults to the streamable-http transport on 0.0.0.0:8000/mcp so the image
# is drop-in compatible with the bifrost MCP registry's expected URL shape.
# Override via MCP_TRANSPORT / MCP_HOST / MCP_PORT env vars.
#
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY mcp_python_interpreter ./mcp_python_interpreter

# Build a wheel + install it into an isolated prefix that we'll copy to the
# runtime stage. Pulls in the mcp>=1.8.0 / fastmcp>=2.0.0 deps declared in
# pyproject.toml.
RUN pip install --prefix=/install .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_DISABLE_DNS_REBINDING_PROTECTION=true

# Copy the wheel install (site-packages + console scripts) over.
COPY --from=builder /install /usr/local

WORKDIR /work
EXPOSE 8000

# Run as the entry-point console script defined by pyproject.toml.
CMD ["mcp-python-interpreter"]
