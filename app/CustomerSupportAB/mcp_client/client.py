import os
import logging
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient

logger = logging.getLogger(__name__)

from bedrock_agentcore.identity import requires_access_token

@requires_access_token(
    provider_name="",
    scopes=[],
    auth_flow="M2M",
)
def _get_bearer_token_my_gateway_secure(*, access_token: str):
    """Obtain OAuth access token via AgentCore Identity for my-gateway-secure."""
    return access_token

def get_my_gateway_secure_mcp_client() -> MCPClient | None:
    """Returns an MCP Client connected to the my-gateway-secure gateway."""
    url = os.environ.get("AGENTCORE_GATEWAY_MY_GATEWAY_SECURE_URL")
    if not url:
        logger.warning("AGENTCORE_GATEWAY_MY_GATEWAY_SECURE_URL not set — my-gateway-secure gateway tools unavailable")
        return None
    token = _get_bearer_token_my_gateway_secure()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return MCPClient(lambda: streamablehttp_client(url, headers=headers), prefix="my_gateway_secure")

def get_all_gateway_mcp_clients() -> list[MCPClient]:
    """Returns MCP clients for all configured gateways."""
    clients = []
    client = get_my_gateway_secure_mcp_client()
    if client:
        clients.append(client)
    return clients
