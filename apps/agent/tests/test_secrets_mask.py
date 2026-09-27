from boundary_agent.mcp_manager import MCPManager
from boundary_agent.models import MCPServer
from boundary_agent.secrets_mask import MASK, mask_config, unmask_config

STORED = {
    "url": "https://mcp.example.test/mcp?exaApiKey=sk-live-123&region=eu",
    "headers": {"Authorization": "Bearer sk-live-123", "X-Api-Key": "k-456", "Accept": "application/json"},
    "env": {"GITHUB_TOKEN": "ghp_789", "LOG_LEVEL": "debug"},
}


def test_mask_hides_every_secret_but_keeps_the_rest_readable():
    masked = mask_config(STORED)
    text = str(masked)
    for secret in ("sk-live-123", "k-456", "ghp_789"):
        assert secret not in text
    assert masked["headers"] == {"Authorization": MASK, "X-Api-Key": MASK, "Accept": "application/json"}
    assert masked["env"] == {"GITHUB_TOKEN": MASK, "LOG_LEVEL": "debug"}
    assert "region=eu" in masked["url"] and f"exaApiKey={MASK}" in masked["url"]
    assert STORED["headers"]["Authorization"] == "Bearer sk-live-123"  # input not mutated


def test_mask_hides_password_in_url_userinfo():
    assert "hunter2" not in mask_config({"url": "https://user:hunter2@host.test/mcp"})["url"]


def test_update_with_masked_values_keeps_stored_secrets():
    edited = mask_config(STORED)
    edited["headers"]["Accept"] = "text/event-stream"
    edited["env"]["LOG_LEVEL"] = "info"
    saved = unmask_config(edited, STORED)
    assert saved["headers"] == {
        "Authorization": "Bearer sk-live-123",
        "X-Api-Key": "k-456",
        "Accept": "text/event-stream",
    }
    assert saved["env"] == {"GITHUB_TOKEN": "ghp_789", "LOG_LEVEL": "info"}
    assert saved["url"] == STORED["url"]
    assert MASK not in str(saved)


def test_update_can_replace_or_drop_a_secret():
    saved = unmask_config({"headers": {"Authorization": "Bearer new", "X-New": MASK}}, STORED)
    assert saved["headers"] == {"Authorization": "Bearer new"}  # a bare *** with nothing stored is dropped


def test_seeded_exa_key_is_sent_but_never_stored():
    manager = MCPManager()
    manager._runtime_headers["exa"] = {"Authorization": "Bearer sk-from-env"}
    server = MCPServer(
        name="exa", transport="streamable_http", enabled=True, config_json={"url": "https://mcp.exa.ai/mcp"}
    )
    assert manager._headers(server) == {"Authorization": "Bearer sk-from-env"}
    assert "headers" not in server.config_json
