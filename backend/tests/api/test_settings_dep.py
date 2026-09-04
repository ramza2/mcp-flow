import pytest
from app.api.dependencies import SettingsDep
from app.core.config import Settings
from app.core.constants import API_V1_PREFIX
from app.main import create_app
from httpx import ASGITransport, AsyncClient


def test_api_v1_prefix_is_canonical_constant() -> None:
    assert API_V1_PREFIX == "/api/v1"


@pytest.mark.asyncio
async def test_settings_dep_uses_app_injected_settings() -> None:
    custom = Settings(
        app_name="Custom MCPFlow Test API",
        environment="settings-dep-test",
        debug=False,
        docs_enabled=True,
    )
    application = create_app(settings=custom)

    @application.get("/__test/settings")
    async def _settings_probe(settings: SettingsDep) -> dict[str, str]:
        return {
            "app_name": settings.app_name,
            "environment": settings.environment,
        }

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/__test/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["app_name"] == "Custom MCPFlow Test API"
    assert body["environment"] == "settings-dep-test"
    assert application.state.settings is custom
