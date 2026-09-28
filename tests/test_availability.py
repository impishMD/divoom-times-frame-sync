# SPDX-FileCopyrightText: 2026 impishMD
# SPDX-License-Identifier: Apache-2.0

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import requests

from timesframesync import cli
from timesframesync.config import Config, FrameUnavailable, SyncError
from timesframesync.frame import Frame, PROBE_TIMEOUT
from test_sources import multi, cycle


def test_probe_reads_api_with_short_timeout_and_existing_tokens():
    frame = Frame(Config(host="127.0.0.1", token="123456"))
    frame.session.post = Mock(return_value=Mock(json=Mock(return_value={"ReturnCode": 0})))
    frame.session.get = Mock()
    frame.check_available()
    args, kwargs = frame.session.post.call_args
    assert args == ("http://127.0.0.1:9000/divoom_api",)
    assert kwargs["timeout"] == PROBE_TIMEOUT == (3, 3)
    assert json.loads(kwargs["data"]) == {
        "Command": "Channel/GetClockInfo", "ReturnCode": 0,
        "DeviceToken": 123456, "LocalToken": 123456}
    assert frame.session.trust_env is False
    frame.session.post.assert_called_once()
    frame.session.get.assert_not_called()


@pytest.mark.parametrize("error", [requests.ConnectionError, requests.ConnectTimeout,
                                       requests.ReadTimeout, requests.exceptions.ChunkedEncodingError])
@pytest.mark.parametrize("operation", ["probe", "inventory", "file_digest"])
def test_network_outages_are_typed_and_nested_timers_stay_quiet(error, operation, caplog):
    frame = Frame(Config(host="127.0.0.1"))
    frame.session.post = Mock(side_effect=error("private-token"))
    frame.session.get = Mock(side_effect=error("private-token"))
    caplog.set_level(logging.INFO)
    with pytest.raises(FrameUnavailable) as failure:
        if operation == "probe":
            frame.check_available()
        elif operation == "inventory":
            frame.inventory()
        else:
            frame.file_digest("/userdata/clip.mp4")
    assert "private-token" not in str(failure.value)
    assert not caplog.records


@pytest.mark.parametrize("body", [{"ReturnCode": 1, "ReturnMessage": "invalid token"},
                                  {}, [], None, "unexpected"])
def test_api_rejection_or_invalid_body_is_not_an_offline_frame(body, caplog):
    frame = Frame(Config(host="127.0.0.1"))
    frame.session.post = Mock(return_value=Mock(json=Mock(return_value=body)))
    with pytest.raises(SyncError) as failure:
        frame.check_available()
    assert not isinstance(failure.value, FrameUnavailable)
    assert any(record.levelno == logging.ERROR for record in caplog.records)


@pytest.mark.parametrize("stage,error", [("raise_for_status", requests.HTTPError("401")),
                                        ("json", ValueError("invalid JSON"))])
def test_http_and_json_errors_are_not_offline(stage, error):
    response = Mock()
    getattr(response, stage).side_effect = error
    frame = Frame(Config(host="127.0.0.1"))
    frame.session.post = Mock(return_value=response)
    with pytest.raises(SyncError) as failure:
        frame.check_available()
    assert not isinstance(failure.value, FrameUnavailable)


def test_offline_cycle_preserves_manifests_and_journals_without_source_requests(multi):
    cycle(multi)
    before = {p.relative_to(multi.config.data_dir): p.read_bytes()
              for p in multi.config.data_dir.rglob("*") if p.is_file()}
    for client in multi.clients.values():
        client.reset_mock()
    multi.frame.check_available = Mock(side_effect=FrameUnavailable("offline"))
    multi.frame.inventory = Mock()
    with pytest.raises(FrameUnavailable):
        cli.sync_cycle(multi)
    after = {p.relative_to(multi.config.data_dir): p.read_bytes()
             for p in multi.config.data_dir.rglob("*") if p.is_file()}
    assert before == after
    for client in multi.clients.values():
        assert not client.mock_calls
    multi.frame.inventory.assert_not_called()


def worker(tmp_path):
    sync = Mock()
    sync.config = Config(host="127.0.0.1", data_dir=tmp_path, sync_interval=30)
    sync.sync_album.return_value = {"items": 1, "uploaded": 0, "removed": 0, "errors": {}}
    return sync


def test_preflight_precedes_sources_and_reconciliation(tmp_path):
    sync = worker(tmp_path)
    cli.sync_cycle(sync, play=False)
    assert sync.mock_calls == [call.frame.check_available(), call.refresh(), call.sync_album(play=False)]


def test_run_throttles_outage_reports_and_recovers_without_losing_first_play(tmp_path, monkeypatch, caplog):
    sync = worker(tmp_path)
    attempts = [FrameUnavailable("offline")] * 22 + [None, None, FrameUnavailable("offline"), None]
    sync.frame.check_available.side_effect = attempts
    clock = SimpleNamespace(now=0)
    waits = []
    handlers = {}

    class Stop:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, interval):
            waits.append(interval)
            clock.now += interval
            if len(waits) == len(attempts):
                handlers[cli.signal.SIGTERM](None, None)

    stop = Stop()
    monkeypatch.setattr(cli.threading, "Event", lambda: stop)
    monkeypatch.setattr(cli.signal, "signal", lambda sig, handler: handlers.update({sig: handler}))
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock.now)
    caplog.set_level(logging.INFO)
    cli.run(sync)
    assert waits == [30] * len(attempts)
    assert sync.refresh.call_count == 3
    assert sync.sync_album.call_args_list == [call(play=True), call(play=False), call(play=False)]
    assert sum("Frame unavailable" in r.message for r in caplog.records) == 3
    assert sum("connection restored" in r.message for r in caplog.records) == 2
    assert sum("Sync cycle complete" in r.message for r in caplog.records) == 2
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert caplog.records[-1].message == "Service stopped"


def test_connection_loss_during_cycle_stops_later_targets_and_invalidates_snapshot(multi, caplog):
    multi.refresh()
    multi.groups = {"immich": [multi.specs[0]], "other": [multi.specs[1]]}
    target = Mock()
    target.sync_album.side_effect = FrameUnavailable("offline")
    multi.target_sync = Mock(return_value=target)
    with pytest.raises(FrameUnavailable):
        multi.sync_album()
    assert multi.target_sync.call_count == 1
    assert multi.ready is None
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_successful_probe_alone_does_not_report_recovery_from_midcycle_outage(tmp_path, caplog):
    sync = worker(tmp_path)
    sync.sync_album.side_effect = FrameUnavailable("offline")
    availability = cli.FrameAvailability()
    caplog.set_level(logging.INFO)
    for _ in range(2):
        with pytest.raises(FrameUnavailable):
            cli.sync_cycle(sync, availability=availability)
    assert sum("cycle interrupted" in r.message for r in caplog.records) == 1
    assert "connection restored" not in caplog.text
    assert "Sync cycle complete" not in caplog.text


@pytest.mark.parametrize("command", ["sync", "cache"])
def test_one_shot_offline_sync_fails_but_cache_still_works(tmp_path, monkeypatch, command):
    sync = worker(tmp_path)
    sync.refresh.return_value = {"errors": {}}
    sync.frame.check_available.side_effect = FrameUnavailable("offline")
    monkeypatch.setattr(cli.Config, "load", lambda *a, **k: sync.config)
    monkeypatch.setattr(cli, "Synchronizer", lambda *a: sync)
    monkeypatch.setattr("sys.argv", ["tfs", command])
    assert cli.main() == (1 if command == "sync" else 0)
    if command == "sync":
        sync.refresh.assert_not_called()
    else:
        sync.frame.check_available.assert_not_called()
        sync.refresh.assert_called_once_with(download=True)
