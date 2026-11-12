"""A what-if mapping file (an offline replay harness's invented entries) can never be picked up by the
normal service: the loader refuses it by default, the service only accepts it when its caller asks
for it explicitly and the source is a replay file, and the shipped wiring never asks."""

from __future__ import annotations

import pytest
from runtime_harness import MAPPING, Runtime, post

from tradingbot.runtime.app import build_service
from tradingbot.runtime.mapping import MappingPolicy, WhatIfMappingRefused
from tradingbot.runtime.service import RuntimeService

WHAT_IF = 'what_if = "invented what-if analysis, not the owner"\n'


def _what_if_file(tmp_path):
    shipped = MAPPING.read_text(encoding="utf-8")
    text = shipped.replace("version = 2\n", "version = 2\n" + WHAT_IF, 1)
    assert WHAT_IF in text
    path = tmp_path / "what_if_mapping.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loader_refuses_a_what_if_file_by_default(tmp_path):
    with pytest.raises(WhatIfMappingRefused):
        MappingPolicy.from_toml(_what_if_file(tmp_path))


def test_loader_refuses_the_marker_whatever_its_value(tmp_path):
    for value in ('""', "false", "0"):
        path = tmp_path / f"m{len(value)}.toml"
        path.write_text(f"version = 2\nwhat_if = {value}\n", encoding="utf-8")
        with pytest.raises(WhatIfMappingRefused):
            MappingPolicy.from_toml(path)


def test_loader_reads_it_only_when_allowed_and_marks_the_policy(tmp_path):
    policy = MappingPolicy.from_toml(_what_if_file(tmp_path), allow_what_if=True)
    assert policy.what_if
    assert policy.executable_assets == ["TLT"]
    assert MappingPolicy.from_toml(MAPPING).what_if is None


def test_an_empty_marker_is_refused_even_when_allowed(tmp_path):
    path = tmp_path / "empty.toml"
    path.write_text('version = 2\nwhat_if = ""\n', encoding="utf-8")
    with pytest.raises(ValueError):
        MappingPolicy.from_toml(path, allow_what_if=True)


def test_the_service_refuses_a_what_if_mapping_by_default(tmp_path):
    with pytest.raises(WhatIfMappingRefused):
        Runtime(tmp_path, [post("1", 0, "We expect bonds to stay choppy.")],
                mapping_file=_what_if_file(tmp_path))


def test_build_service_never_allows_it(tmp_path):
    (tmp_path / "ok").mkdir()
    rt = Runtime(tmp_path / "ok", [post("1", 0, "We expect bonds to stay choppy.")])
    rt.service.close()
    config = rt.config.model_copy(
        update={"paths": rt.config.paths.model_copy(update={"mapping_file": _what_if_file(tmp_path)})}
    )
    with pytest.raises(WhatIfMappingRefused):
        build_service(config, clock=rt.clock.now)


def test_the_service_accepts_it_only_for_a_replay_file_source(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, "We expect bonds to stay choppy.")])
    rt.service.close()
    config = rt.config.model_copy(
        update={"paths": rt.config.paths.model_copy(update={"mapping_file": _what_if_file(tmp_path)})}
    )

    class LiveLike:
        name, realtime = "live", True

        def poll(self):
            return []

        def commit(self, post):
            pass

        def cursor(self):
            return None

        def next_poll_delay(self, cadence):
            return cadence

    with pytest.raises(ValueError, match="replay-file source"):
        RuntimeService(config, source=LiveLike(), broker=rt.broker, clock=rt.clock.now,
                       store=rt.store, allow_what_if_mapping=True)
    svc = RuntimeService(config, source=rt.source, broker=rt.broker, clock=rt.clock.now,
                         store=rt.store, allow_what_if_mapping=True)
    try:
        assert svc.mapping.what_if
        started = [e for e in rt.store.events() if e["event"] == "SERVICE_START"]
        assert started and started[-1]["detail"]["what_if_mapping"] is True
    finally:
        svc.close()
