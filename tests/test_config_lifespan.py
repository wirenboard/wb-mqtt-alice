from unittest.mock import AsyncMock, MagicMock

from wb.mqtt_alice.config import main


async def test_lifespan_runs_startup_tasks(monkeypatch):
    config = object()
    calls = []
    monkeypatch.setattr(main, "init_globals", MagicMock(side_effect=lambda: calls.append("init")))
    monkeypatch.setattr(main, "load_config", MagicMock(return_value=config))
    finalize = MagicMock(side_effect=lambda *a, **kw: calls.append("finalize"))
    monkeypatch.setattr(main, "finalize_config_change", finalize)
    restore = AsyncMock(side_effect=lambda *a: calls.append("restore"))
    monkeypatch.setattr(main, "restore_client_status_if_needed", restore)

    async with main.app.router.lifespan_context(main.app):
        assert calls == ["init", "finalize", "restore"]

    finalize.assert_called_once_with(config, force_client_reload=False)
    restore.assert_awaited_once_with(config)
