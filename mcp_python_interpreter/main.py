"""Main module for mcp-python-interpreter."""

import os

from mcp_python_interpreter.server import mcp


def main():
    """Run the MCP Python Interpreter server.

    Transport selection (env-driven so existing stdio users are unaffected):

      MCP_TRANSPORT=stdio           (default) — original behavior
      MCP_TRANSPORT=streamable-http — HTTP transport on MCP_HOST:MCP_PORT
                                      (defaults 0.0.0.0:8000, path /mcp)
      MCP_TRANSPORT=sse             — SSE transport on MCP_HOST:MCP_PORT

    DNS-rebinding protection:

      The MCP SDK ships a transport-security layer that rejects requests
      whose Host header isn't in {127.0.0.1:*, localhost:*, [::1]:*} when
      using SSE / streamable-http. That's the right default for a server
      bound to 127.0.0.1 on a developer laptop, but it kills any remote
      MCP deployment behind a gateway / reverse proxy / container name.

      Two knobs are exposed via env:

        MCP_ALLOWED_HOSTS=h1,h2  — extra Host values to accept
                                   (e.g. "mcp-python:*,my-gateway.local:*")
        MCP_DISABLE_DNS_REBINDING_PROTECTION=true — turn the check off
                                                    entirely (suitable when
                                                    the server is on a
                                                    trusted network only).
    """
    transport = os.environ.get('MCP_TRANSPORT', 'stdio')

    if transport == 'stdio':
        mcp.run(transport='stdio')
        return

    if transport not in ('streamable-http', 'sse'):
        raise SystemExit(
            f"Unknown MCP_TRANSPORT={transport!r}; expected "
            "'stdio', 'streamable-http', or 'sse'."
        )

    # FastMCP's host/port come from its Settings object (FASTMCP_HOST /
    # FASTMCP_PORT env vars or constructor kwargs) — they are NOT accepted
    # as kwargs to .run(). Mutate settings in place so users can drive them
    # with the same MCP_* env vars used elsewhere.
    mcp.settings.host = os.environ.get('MCP_HOST', '0.0.0.0')
    mcp.settings.port = int(os.environ.get('MCP_PORT', '8000'))

    # streamable-http response framing: SSE-streamed (default) vs single
    # application/json reply. Some MCP gateways (e.g. bifrost) only parse
    # the JSON form during background tool-discovery, so default to JSON
    # when running streamable-http for broader compatibility. Override
    # with MCP_JSON_RESPONSE=false to force SSE.
    json_resp_env = os.environ.get('MCP_JSON_RESPONSE', '').lower()
    if transport == 'streamable-http':
        if json_resp_env in ('1', 'true', 'yes', ''):
            mcp.settings.json_response = True
        elif json_resp_env in ('0', 'false', 'no'):
            mcp.settings.json_response = False

        # Stateless mode: spawn a fresh transport per request, no session
        # tracking required. This is what most MCP gateways (bifrost,
        # supergateway, mcp-proxy) actually want when proxying for many
        # downstream callers — the gateway handles session continuity, and
        # the upstream MCP just needs to answer one request at a time.
        # Default ON for streamable-http; override with
        # MCP_STATELESS_HTTP=false to keep stateful sessions.
        stateless_env = os.environ.get('MCP_STATELESS_HTTP', '').lower()
        if stateless_env in ('1', 'true', 'yes', ''):
            mcp.settings.stateless_http = True
        elif stateless_env in ('0', 'false', 'no'):
            mcp.settings.stateless_http = False

    # Transport-security (DNS-rebinding) config.
    disable_dns_rebinding = os.environ.get(
        'MCP_DISABLE_DNS_REBINDING_PROTECTION', ''
    ).lower() in ('1', 'true', 'yes')
    allowed_hosts_env = os.environ.get('MCP_ALLOWED_HOSTS', '').strip()

    if disable_dns_rebinding or allowed_hosts_env:
        # Only import + construct when actually needed (older SDKs without
        # the security layer raise ImportError, which is fine — those users
        # are on the default-trusted-localhost path anyway).
        from mcp.server.transport_security import TransportSecuritySettings

        if disable_dns_rebinding:
            tss = TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            )
        else:
            extra = [h.strip() for h in allowed_hosts_env.split(',') if h.strip()]
            tss = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=extra,
            )
        # FastMCP threads transport_security through .settings only on very
        # recent SDKs; on older ones we have to drop down a level and pass
        # it directly to the async runner.
        try:
            mcp.settings.transport_security = tss  # type: ignore[attr-defined]
            mcp.run(transport=transport)
            return
        except (AttributeError, ValueError):
            import anyio
            if transport == 'streamable-http':
                anyio.run(
                    lambda: mcp.run_streamable_http_async(
                        transport_security=tss,
                    )
                )
            else:
                anyio.run(
                    lambda: mcp.run_sse_async(transport_security=tss)
                )
            return

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()