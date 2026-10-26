"""notify / watch tests (fake WhatsApp, real subprocesses)."""

import sys

import pytest

from assistant import notify as notify_mod
from assistant.cli import main
from assistant.notify import NotifyError, format_duration, notify, watch
from assistant.state import State

ME = "user:111"


class FakePlatform:
    def __init__(self):
        self.sent = []

    def send_text(self, to, body, *, reply_to=None):
        self.sent.append((to, body))
        return f"wamid.{len(self.sent)}"


def known_state(tmp_path):
    state = State(tmp_path / "state.json")
    state.creator = ME
    return state


def test_notify_sends_to_saved_creator(tmp_path):
    platform = FakePlatform()
    ids = notify("Backup finished", platform=platform, state=known_state(tmp_path))
    assert platform.sent == [(ME, "Backup finished")] and ids == ["wamid.1"]


def test_notify_splits_long_messages(tmp_path):
    platform = FakePlatform()
    notify("word " * 2000, platform=platform, state=known_state(tmp_path))
    assert len(platform.sent) >= 3 and all(len(body) <= 4096 for _, body in platform.sent)


def test_notify_needs_creator_first(tmp_path):
    with pytest.raises(NotifyError, match="message once"):
        notify("hi", platform=FakePlatform(), state=State(tmp_path / "state.json"))


def test_notify_command_explains_missing_creator(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(notify_mod, "STATE_FILE", tmp_path / "state.json")
    assert main(["notify", "hello"]) == 1
    assert "message once" in caplog.text


def test_watch_success_reports_time(capsys):
    sent = []
    code = watch([sys.executable, "-c", "print('working')"], send=sent.append, label="job")
    assert code == 0
    assert "working" in capsys.readouterr().out          # output still shows live
    assert sent[0].startswith("✅ *job*") and "finished after" in sent[0]


def test_watch_failure_includes_exit_code_and_output_tail():
    sent = []
    code = watch([sys.executable, "-c", "import sys; print('boom: disk full'); sys.exit(3)"], send=sent.append)
    assert code == 3
    assert sent[0].startswith("❌") and "exit 3" in sent[0] and "boom: disk full" in sent[0]


def test_watch_single_string_runs_through_shell():
    sent = []
    code = watch([f'"{sys.executable}" -c "print(1)" && "{sys.executable}" -c "print(2)"'], send=sent.append)
    assert code == 0 and sent[0].startswith("✅")


def test_watch_missing_program_is_reported():
    sent = []
    code = watch(["definitely-not-a-program-xyz", "--flag"], send=sent.append)
    assert code == 127 and "could not start" in sent[0]


def test_watch_survives_notification_failure():
    def broken_send(_):
        raise RuntimeError("no network")
    assert watch([sys.executable, "-c", "pass"], send=broken_send) == 0


def test_format_duration():
    assert format_duration(7) == "7s"
    assert format_duration(185) == "3m 05s"
    assert format_duration(3 * 3600 + 120) == "3h 02m"
