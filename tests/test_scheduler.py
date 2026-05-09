from app.services import scheduler as scheduler_module


def test_scheduler_runs_monitor_every_minute(monkeypatch):
    captured = {}

    class FakeScheduler:
        running = False

        def add_job(self, func, trigger, **kwargs):
            captured["trigger"] = trigger
            captured.update(kwargs)

        def start(self):
            captured["started"] = True

    class FakeSettings:
        scheduler_enabled = True

    monkeypatch.setattr(scheduler_module, "settings", FakeSettings())
    monkeypatch.setattr(scheduler_module, "scheduler", FakeScheduler())

    scheduler_module.start_scheduler()

    assert captured["trigger"] == "interval"
    assert captured["minutes"] == 1
    assert captured["max_instances"] == 1
    assert captured["coalesce"] is True
    assert captured["started"] is True
