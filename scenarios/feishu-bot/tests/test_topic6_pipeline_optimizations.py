from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import threading
import types
from pathlib import Path

import httpx
import pandas as pd
import pytest


TOPIC6_DIR = Path(__file__).resolve().parents[1] / "cases" / "topic6"
ANNOTATE_SCRIPT = (
    TOPIC6_DIR
    / "ma-resources"
    / "skills"
    / "topic6-annotation"
    / "scripts"
    / "datahub_annotate.py"
)
PIPELINE_F_SCRIPT = (
    TOPIC6_DIR
    / "ma-resources"
    / "skills"
    / "topic6-insight"
    / "scripts"
    / "pipeline_f.py"
)
WEB_REPORT_DIR = (
    TOPIC6_DIR / "ma-resources" / "skills" / "topic6-web-report"
)
WEB_REPORT_BUILDER = WEB_REPORT_DIR / "assets" / "source" / "build-report.mjs"
WEB_REPORT_EXAMPLE = WEB_REPORT_DIR / "assets" / "source" / "source.example.json"
PIPELINE_E_SCRIPT = PIPELINE_F_SCRIPT.with_name("pipeline_e.py")
COORDINATOR_PROMPT = (
    TOPIC6_DIR / "ma-resources" / "agents" / "coordinator.system.md"
)
PIPELINE_OVERVIEW = TOPIC6_DIR / "topic6_pipeline_overview.html"
PHASE_F_PROMPT = (
    TOPIC6_DIR
    / "ma-resources"
    / "skills"
    / "topic6-annotation"
    / "prompts"
    / "07_阶段_F发布.md"
)
ANNOTATOR_PROMPT = (
    TOPIC6_DIR / "ma-resources" / "agents" / "annotator.system.md"
)
EVENT_REGISTRY_DIR = (
    TOPIC6_DIR / "ma-resources" / "skills" / "topic6-event-registry"
)
RELAY_SCRIPT = EVENT_REGISTRY_DIR / "scripts" / "relay.py"
C2_RUNNER_SCRIPT = EVENT_REGISTRY_DIR / "scripts" / "run_topic6_c2.py"
DEMO_ROUTES_SCRIPT = (
    TOPIC6_DIR
    / "ma-resources"
    / "skills"
    / "topic6-annotation"
    / "scripts"
    / "run_demo_routes.py"
)
RUN_CONFIG_STATE_SCRIPT = (
    TOPIC6_DIR
    / "ma-resources"
    / "skills"
    / "topic6-annotation"
    / "scripts"
    / "run_config_state.py"
)
WAIT_DATAHUB_BATCH_SCRIPT = RUN_CONFIG_STATE_SCRIPT.with_name(
    "wait_datahub_batch.py"
)
ENVIRONMENT_CONFIG = TOPIC6_DIR / "ma-resources" / "environment.json"
C0_PROMPT_DIR = (
    TOPIC6_DIR
    / "ma-resources"
    / "skills"
    / "topic6-annotation"
    / "prompts"
    / "C0_基础事实"
)
C0_V13_PROMPT = C0_PROMPT_DIR / "v13.md"
MARKETING_CALENDAR = (
    TOPIC6_DIR
    / "ma-resources"
    / "skills"
    / "topic6-fetch-normalize"
    / "references"
    / "marketing_calendar_2026.csv"
)
E2_NODES_SCRIPT = (
    TOPIC6_DIR
    / "ma-resources"
    / "skills"
    / "topic6-insight"
    / "01_统计"
    / "e2_nodes.py"
)


def _load_annotate_module():
    if "pandas" not in sys.modules and importlib.util.find_spec("pandas") is None:
        sys.modules.setdefault("pandas", types.ModuleType("pandas"))
    spec = importlib.util.spec_from_file_location("topic6_datahub_annotate", ANNOTATE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_relay_module():
    # rc.14 relay 依赖同目录 c2_runtime
    if str(RELAY_SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(RELAY_SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("topic6_event_registry_relay", RELAY_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_c2_runner_module():
    spec = importlib.util.spec_from_file_location(
        "topic6_event_registry_runner", C2_RUNNER_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_demo_routes_module():
    spec = importlib.util.spec_from_file_location(
        "topic6_demo_routes", DEMO_ROUTES_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_run_config_state_module():
    spec = importlib.util.spec_from_file_location(
        "topic6_run_config_state", RUN_CONFIG_STATE_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_wait_datahub_batch_module():
    spec = importlib.util.spec_from_file_location(
        "topic6_wait_datahub_batch", WAIT_DATAHUB_BATCH_SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_pipeline_f_module():
    spec = importlib.util.spec_from_file_location("topic6_pipeline_f", PIPELINE_F_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_pipeline_e_module():
    openai_stub = types.ModuleType("openai")
    openai_stub.AsyncOpenAI = object
    old_openai = sys.modules.get("openai")
    old_stdout, old_stderr = sys.stdout, sys.stderr
    fake_stdout = types.SimpleNamespace(buffer=__import__("io").BytesIO())
    fake_stderr = types.SimpleNamespace(buffer=__import__("io").BytesIO())
    sys.modules["openai"] = openai_stub
    try:
        sys.stdout, sys.stderr = fake_stdout, fake_stderr
        spec = importlib.util.spec_from_file_location(
            "topic6_pipeline_e", PIPELINE_E_SCRIPT
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        if old_openai is None:
            sys.modules.pop("openai", None)
        else:
            sys.modules["openai"] = old_openai
    return module


def _load_e2_nodes_module():
    script_dir = str(E2_NODES_SCRIPT.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("topic6_e2_nodes", E2_NODES_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeHttpResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self._body


class _FakeStreamingHttpResponse:
    def __init__(self, chunks):
        self._lines = [
            f"data: {json.dumps(chunk)}\n".encode()
            for chunk in chunks
        ] + [b"data: [DONE]\n"]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def __iter__(self):
        return iter(self._lines)


class _BlockingStreamingHttpResponse:
    def __init__(self):
        self._closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def __iter__(self):
        return self

    def __next__(self):
        self._closed.wait(5)
        raise StopIteration

    def close(self):
        self._closed.set()


def test_relay_appends_resource_to_versioned_relay_base_url(monkeypatch):
    module = _load_relay_module()
    requested_urls = []
    requested_bodies = []

    def fake_urlopen(request, timeout):
        assert timeout == 600
        requested_urls.append(request.full_url)
        requested_bodies.append(json.loads(request.data))
        return _FakeStreamingHttpResponse(
            [
                {
                    "choices": [
                        {"finish_reason": None, "delta": {"content": "o"}}
                    ]
                },
                {
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                    "choices": [
                        {"finish_reason": "stop", "delta": {"content": "k"}}
                    ],
                },
            ]
        )

    monkeypatch.setenv("LLM_RELAY_BASE_URL", "https://relay.example/v1/")
    monkeypatch.setenv("LLM_RELAY_API_KEY", "test-key")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    assert module.Relay("test-model", 10, "system").call("hello") == "ok"
    assert requested_urls == ["https://relay.example/v1/chat/completions"]
    assert requested_bodies[0]["stream"] is True
    assert requested_bodies[0]["stream_options"] == {"include_usage": True}


def test_relay_uses_llm_relay_env_and_ignores_ark_env(monkeypatch):
    """模型统一走 bmc-llm-relay：ARK_* 只是方舟控制面凭据，不得用于模型调用。"""
    module = _load_relay_module()
    requested_urls = []

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        assert request.headers["Authorization"] == "Bearer relay-key"
        return _FakeStreamingHttpResponse(
            [{"usage": {"prompt_tokens": 1, "completion_tokens": 1},
              "choices": [{"finish_reason": "stop", "delta": {"content": "ok"}}]}]
        )

    monkeypatch.setenv("LLM_RELAY_BASE_URL", "https://bmc-llm-relay.example.com/v1")
    monkeypatch.setenv("LLM_RELAY_API_KEY", "relay-key")
    monkeypatch.setenv("ARK_BASE_URL", "https://ark.example/api/v3")
    monkeypatch.setenv("ARK_API_KEY", "ark-control-plane-key")
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    module.Relay("test-model", 10, "system").call("hello")
    assert requested_urls == ["https://bmc-llm-relay.example.com/v1/chat/completions"]


@pytest.mark.parametrize("loader", ["relay", "embedder"])
def test_relay_and_embedder_do_not_fall_back_to_ark_env(monkeypatch, loader):
    """缺 LLM_RELAY_* 时必须报错,不得静默改用 ARK_*/OPENAI_*(会改走火山方舟计费与配额)。"""
    module = _load_relay_module()
    for name in ("LLM_RELAY_BASE_URL", "LLM_RELAY_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARK_BASE_URL", "https://ark.example/api/v3")
    monkeypatch.setenv("ARK_API_KEY", "ark-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    with pytest.raises(SystemExit, match="LLM_RELAY"):
        if loader == "relay":
            module.Relay("m", 10, "s")
        else:
            module.Embedder(model="Doubao-embedding")


def test_relay_enforces_overall_stream_deadline(monkeypatch):
    module = _load_relay_module()
    response = _BlockingStreamingHttpResponse()

    monkeypatch.setenv("LLM_RELAY_BASE_URL", "https://bmc-llm-relay.example.com/v1")
    monkeypatch.setenv("LLM_RELAY_API_KEY", "test-key")
    monkeypatch.setattr(
        module.urllib.request, "urlopen", lambda *_args, **_kwargs: response
    )

    with pytest.raises(TimeoutError, match="整体时限"):
        module.Relay(
            "test-model", 10, "system", timeout=0.05, retries=1
        ).call("hello")


def test_vision_embedder_uses_multimodal_protocol_one_text_per_request(monkeypatch):
    module = _load_relay_module()
    requested_urls = []
    requested_bodies = []

    def fake_urlopen(request, timeout):
        assert timeout == 600
        requested_urls.append(request.full_url)
        requested_bodies.append(json.loads(request.data))
        return _FakeHttpResponse(
            {"usage": {"input_tokens": 1}, "data": {"embedding": [0.1, 0.2]}}
        )

    monkeypatch.setenv("LLM_RELAY_BASE_URL", "https://ark.example/api/v3")
    monkeypatch.setenv("LLM_RELAY_API_KEY", "test-key")
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    assert module.Embedder(model="doubao-embedding-vision-251215").embed(
        ["hello", "world"], batch=64
    ) == [
        [0.1, 0.2],
        [0.1, 0.2],
    ]
    assert requested_urls == [
        "https://ark.example/api/v3/embeddings/multimodal",
        "https://ark.example/api/v3/embeddings/multimodal",
    ]
    assert requested_bodies == [
        {
            "model": "doubao-embedding-vision-251215",
            "input": [{"type": "text", "text": "hello"}],
            "encoding_format": "float",
        },
        {
            "model": "doubao-embedding-vision-251215",
            "input": [{"type": "text", "text": "world"}],
            "encoding_format": "float",
        },
    ]


def test_embedder_keeps_standard_batch_protocol_for_legacy_override(monkeypatch):
    module = _load_relay_module()
    requested_bodies = []

    def fake_urlopen(request, timeout):
        assert timeout == 600
        assert request.full_url == "https://ark.example/api/v3/embeddings"
        requested_bodies.append(json.loads(request.data))
        return _FakeHttpResponse(
            {
                "usage": {"prompt_tokens": 2},
                "data": [
                    {"embedding": [0.1]},
                    {"embedding": [0.2]},
                ],
            }
        )

    monkeypatch.setenv("LLM_RELAY_BASE_URL", "https://ark.example/api/v3")
    monkeypatch.setenv("LLM_RELAY_API_KEY", "test-key")
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    embedder = module.Embedder(model="text-embedding-3-small")
    assert embedder.embed(["hello", "world"], batch=64) == [[0.1], [0.2]]
    assert requested_bodies == [
        {"model": "text-embedding-3-small", "input": ["hello", "world"]}
    ]


def test_extract_result_rows_flattens_nested_data_and_result_alias():
    module = _load_annotate_module()

    rows = module._extract_result_rows(
        {
            "result_list": [
                {
                    "input_data": {"row_id": "T0001", "title": "标题"},
                    "result": {"是否营销可用": "是"},
                }
            ]
        }
    )

    assert rows[0]["row_id"] == "T0001"
    assert rows[0]["title"] == "标题"
    assert rows[0]["llm_result"] == '{"是否营销可用": "是"}'


def test_prepare_input_publishes_converted_workbook_atomically(tmp_path, monkeypatch):
    module = _load_annotate_module()
    source = tmp_path / "input.xlsx"
    source.touch()
    writes = []
    replacements = []

    class _FakeFrame:
        columns = ["hottopic_desc"]

        def rename(self, *, columns):
            assert columns == {"hottopic_desc": "desc"}
            self.columns = ["desc"]
            return self

        def to_excel(self, path, index=False):
            assert index is False
            writes.append(path)
            path.write_text("complete workbook", encoding="utf-8")

    monkeypatch.setattr(module.pd, "read_excel", lambda _path: _FakeFrame())

    def fake_replace(source_path, destination_path):
        replacements.append((source_path, destination_path))
        destination_path.write_bytes(source_path.read_bytes())
        source_path.unlink()

    monkeypatch.setattr(module.os, "replace", fake_replace)

    prepared = module._prepare_input(source)

    assert prepared == tmp_path / ".input_prepared.xlsx"
    assert writes[0] != prepared
    assert writes[0].name.endswith(".tmp.xlsx")
    assert replacements == [(writes[0], prepared)]
    assert prepared.read_text(encoding="utf-8") == "complete workbook"


def test_download_inline_results_reads_paginated_result_list(tmp_path, monkeypatch):
    module = _load_annotate_module()
    written_rows = []

    class _FakeFrame:
        def __init__(self, rows):
            self._rows = rows
            self.columns = set().union(*(row.keys() for row in rows))

        def to_excel(self, path, index=False):
            assert index is False
            written_rows.extend(self._rows)
            path.write_text("written", encoding="utf-8")

    monkeypatch.setattr(
        module, "pd", types.SimpleNamespace(DataFrame=_FakeFrame)
    )
    pages = {
        1: {"result_list": [{"row_id": "T0001", "llm_result": "{}"}]},
        2: {"result_list": [{"row_id": "T0002", "llm_result": "{}"}]},
    }

    monkeypatch.setattr(
        module,
        "_get_task",
        lambda _key, _task_id, *, page=None, page_size=None: pages.get(
            page, {"result_list": []}
        ),
    )
    out = tmp_path / "result.xlsx"
    module._download_inline_results(
        "secret",
        42,
        {"result_list": [{"row_id": "partial", "llm_result": "{}"}]},
        out,
        expected_rows=2,
    )

    assert [row["row_id"] for row in written_rows] == ["T0001", "T0002"]


def test_get_task_retries_transient_503(monkeypatch):
    module = _load_annotate_module()
    attempts = []
    delays = []

    class _Response:
        def __init__(self, status_code, body=None):
            self.status_code = status_code
            self._body = body or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise module.requests.HTTPError(
                    f"{self.status_code} unavailable", response=self
                )

        def json(self):
            return self._body

    responses = [
        _Response(503),
        _Response(200, {"code": 0, "data": {"task_status": "TASK_STATUS_RUNNING"}}),
    ]

    def fake_get(*_args, **_kwargs):
        attempts.append(True)
        return responses.pop(0)

    monkeypatch.setattr(module.requests, "get", fake_get)
    monkeypatch.setattr(module.time, "sleep", delays.append)

    result = module._get_task("secret", 5041)

    assert result["task_status"] == "TASK_STATUS_RUNNING"
    assert len(attempts) == 2
    assert delays == [module.TASK_QUERY_RETRY_BASE_SECONDS]


def test_get_task_uses_datahub_page_num_parameter(monkeypatch):
    module = _load_annotate_module()
    requested_params = []

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": 0, "data": {"result_list": []}}

    def fake_get(*_args, **kwargs):
        requested_params.append(kwargs["params"])
        return _Response()

    monkeypatch.setattr(module.requests, "get", fake_get)

    module._get_task("secret", 5042, page=2, page_size=1000)

    assert requested_params == [{"page_num": 2, "page_size": 1000}]


def test_validate_model_id_prints_list_and_returns_exact_match(capsys):
    module = _load_annotate_module()
    models = [
        {
            "model_id": "Doubao-pro-32k",
            "platform": "Volcengine",
            "input_price": "¥1.2",
            "output_price": "¥3.4",
        },
        {"model_id": "gpt-5.6-luna", "platform": "OpenAI"},
    ]

    info = module._validate_model_id("Doubao-pro-32k", models)

    assert info == {
        "platform": "Volcengine",
        "input_price": 1.2,
        "output_price": 3.4,
    }
    output = capsys.readouterr().out
    assert "/api/v1/model/list: 2 models" in output
    assert "Doubao-pro-32k, gpt-5.6-luna" in output


def test_validate_model_id_suggests_case_sensitive_match():
    module = _load_annotate_module()

    with pytest.raises(
        ValueError,
        match="invalid model_id: doubao-pro-32k; 大小写敏感候选: Doubao-pro-32k",
    ):
        module._validate_model_id(
            "doubao-pro-32k",
            [{"model_id": "Doubao-pro-32k"}],
        )


def test_agent_prompts_use_canonical_model_id_and_allow_one_retry():
    coordinator = COORDINATOR_PROMPT.read_text(encoding="utf-8")
    annotator = ANNOTATOR_PROMPT.read_text(encoding="utf-8")

    assert "`DeepSeek-V4-Flash`" in coordinator
    assert '--model-id "$DATAHUB_MODEL_ID"' in annotator
    assert "C2_CHAT_MODEL_ID" in coordinator
    assert "`DeepSeek-V4.1-Flash`" in coordinator
    assert "最多重试 1 次" in annotator
    assert "`Doubao-pro-32k`" not in coordinator
    assert "--model-id Doubao-pro-32k" not in annotator


def test_annotation_prompt_version_is_explicit_and_fail_closed():
    coordinator = COORDINATOR_PROMPT.read_text(encoding="utf-8")
    annotator = ANNOTATOR_PROMPT.read_text(encoding="utf-8")

    assert "/mnt/skills/topic6-annotation/SKILL.md" in coordinator
    assert "每条委派消息必须是输入契约 JSON" in coordinator
    assert "`prompt_version`" in coordinator
    assert "禁止扫描目录猜最新版" in coordinator
    assert '"prompt_version": "{required_prompt_version}"' in annotator
    assert "^v[1-9][0-9]*$" in annotator
    assert "预检失败不得启动 worker" in annotator
    assert '"prompt_version": "v4"' not in annotator


def test_coordinator_uses_memory_tools_not_filesystem_paths():
    coordinator = COORDINATOR_PROMPT.read_text(encoding="utf-8")

    assert "Memory Store 不是沙箱文件系统" in coordinator
    assert "/topic6/_版本状态.md" in coordinator
    assert "/mnt/memory/$TOPIC6_MEMORY_STORE_ID" not in coordinator


def test_c2_demo_fast_normalizes_merge_compatible_result():
    module = _load_c2_runner_module()
    records = [
        {"record_id": "a", "platform": "微博", "title": "事件A"},
        {"record_id": "b", "platform": "抖音", "title": "事件A视频"},
    ]

    result = module._normalize_demo_fast_results(
        records,
        {
            "results": [
                {"row_id": "a", "一级事件名": "事件A"},
                {"row_id": "b", "一级事件名": "事件A"},
            ]
        },
    )
    assert result == [
        {"row_id": "a", "一级事件名": "事件A"},
        {"row_id": "b", "一级事件名": "事件A"},
    ]

    with pytest.raises(ValueError, match="missing row_ids"):
        module._normalize_demo_fast_results(
            [
                {"record_id": "a", "platform": "微博", "title": "事件A"},
                {"record_id": "b", "platform": "抖音", "title": "事件B"},
            ],
            {"results": [{"row_id": "a", "一级事件名": "事件A"}]},
        )


def test_c2_demo_fast_rejects_non_demo_mode():
    module = _load_c2_runner_module()
    args = module.argparse.Namespace(mode="full", demo_fast=True)

    with pytest.raises(ValueError, match="only valid with --mode demo"):
        module.run(args)


def test_demo_routes_require_complete_unique_results():
    module = _load_demo_routes_module()
    rows = [{"row_id": "a"}, {"row_id": "b"}]

    normalized = module._normalize_results(
        "r2",
        rows,
        [
            {"row_id": "a", "是否商业合作": "是", "判断说明": "存在合作"},
            {"row_id": "b", "是否商业合作": "否", "判断说明": "无双边关系"},
        ],
    )
    assert [row["r2_是否商业合作"] for row in normalized] == ["是", "否"]

    with pytest.raises(ValueError, match="missing or invalid row_ids"):
        module._normalize_results(
            "r2",
            rows,
            [{"row_id": "a", "是否商业合作": "是", "判断说明": "存在合作"}],
        )

    with pytest.raises(ValueError, match="duplicate row_id"):
        module._normalize_results(
            "r2",
            rows,
            [
                {"row_id": "a", "是否商业合作": "是", "判断说明": "存在合作"},
                {"row_id": "a", "是否商业合作": "否", "判断说明": "重复"},
            ],
        )


def test_demo_routes_retry_disconnect_429_and_5xx(monkeypatch):
    module = _load_demo_routes_module()
    route = module.ROUTES["r2"]
    rows = [{"row_id": "a"}]
    request = httpx.Request(
        "POST", "https://ark.example/chat/completions"
    )
    outcomes = [
        httpx.RemoteProtocolError("remote closed"),
        httpx.Response(
            429,
            text="rate limited",
            request=request,
        ),
        httpx.Response(
            503,
            text="unavailable",
            request=request,
        ),
        httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "results": [
                                        {
                                            "row_id": "a",
                                            "是否商业合作": "是",
                                            "判断说明": "存在合作",
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
            },
            request=request,
        ),
    ]
    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            self.posts = []
            clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(module.httpx, "Client", FakeClient)
    monkeypatch.setattr(module.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(module.random, "uniform", lambda _start, _end: 0)

    result = module._request_route(
        "r2",
        route,
        rows,
        model="doubao-seed-evolving",
        base_url="https://ark.example",
        api_key="test-key",
        max_attempts=4,
    )

    assert len(clients) == 4
    assert all(client.closed for client in clients)
    assert all(len(client.posts) == 1 for client in clients)
    assert all(client.kwargs["http2"] is False for client in clients)
    assert all(
        client.posts[0][1]["headers"]["Connection"] == "close"
        for client in clients
    )
    assert result["normalized"][0]["r2_是否商业合作"] == "是"
    assert result["input_tokens"] == 10


def test_demo_routes_start_all_five_routes_concurrently(tmp_path, monkeypatch):
    module = _load_demo_routes_module()
    project = tmp_path / "project"
    source = project / "usable.xlsx"
    project.mkdir()
    source.write_bytes(b"stable input signature")
    frame = pd.DataFrame(
        [{"row_id": "a", "platform": "微博", "title": "A", "desc": "A desc"}]
    )
    barrier = threading.Barrier(len(module.ROUTES), timeout=2)
    started = []
    lock = threading.Lock()

    monkeypatch.setattr(module.pd, "read_excel", lambda _path: frame.copy())
    monkeypatch.setenv("LLM_RELAY_API_KEY", "test-key")
    monkeypatch.setenv("LLM_RELAY_BASE_URL", "https://relay.example/v1")
    monkeypatch.setattr(module, "_record_cost", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "_finalize_route",
        lambda *_args, **_kwargs: {"rows": 1},
    )

    def fake_request(route_name, route, rows, **_kwargs):
        with lock:
            started.append(route_name)
        barrier.wait()
        return {
            "route": route_name,
            "normalized": [
                {
                    "row_id": rows[0]["row_id"],
                    f"{route_name}_{route['label']}": "否",
                    f"{route_name}_判断说明": "测试判断",
                    f"{route_name}_parse_error": 0,
                }
            ],
            "input_tokens": 1,
            "output_tokens": 1,
        }

    monkeypatch.setattr(module, "_request_route", fake_request)

    result = module.run(
        str(project),
        str(source),
        "doubao-seed-evolving",
        1,
    )

    assert set(started) == set(module.ROUTES)
    assert set(result) == set(module.ROUTES)


def test_demo_routes_persist_completed_routes_and_resume_missing_chunk(
    tmp_path, monkeypatch
):
    module = _load_demo_routes_module()
    project = tmp_path / "project"
    source = project / "usable.xlsx"
    project.mkdir()
    input_frame = pd.DataFrame(
        [
            {"row_id": "a", "platform": "微博", "title": "A", "desc": "A desc"},
            {"row_id": "b", "platform": "抖音", "title": "B", "desc": "B desc"},
            {"row_id": "c", "platform": "小红书", "title": "C", "desc": "C desc"},
            {"row_id": "d", "platform": "微信", "title": "D", "desc": "D desc"},
        ]
    )
    source.write_bytes(b"stable input signature")

    def fake_atomic_excel(frame, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(frame.to_json(force_ascii=False), encoding="utf-8")

    monkeypatch.setattr(module.pd, "read_excel", lambda _path: input_frame.copy())
    monkeypatch.setattr(module, "_atomic_excel", fake_atomic_excel)
    monkeypatch.setenv("LLM_RELAY_API_KEY", "test-key")
    monkeypatch.setenv("LLM_RELAY_BASE_URL", "https://relay.example/v1")
    monkeypatch.setattr(module, "_record_cost", lambda *_args, **_kwargs: None)

    failed_once = False
    first_calls = []

    def fake_request(route_name, route, rows, **_kwargs):
        nonlocal failed_once
        row_ids = tuple(row["row_id"] for row in rows)
        first_calls.append((route_name, row_ids))
        if route_name == "r2" and row_ids == ("c", "d") and not failed_once:
            failed_once = True
            raise RuntimeError("simulated disconnect")
        return {
            "route": route_name,
            "normalized": [
                {
                    "row_id": row["row_id"],
                    f"{route_name}_{route['label']}": "是",
                    f"{route_name}_判断说明": "测试判断",
                    f"{route_name}_parse_error": 0,
                }
                for row in rows
            ],
            "input_tokens": len(rows) * 10,
            "output_tokens": len(rows) * 2,
        }

    monkeypatch.setattr(module, "_request_route", fake_request)

    with pytest.raises(RuntimeError, match="rerun to resume"):
        module.run(
            str(project),
            str(source),
            "doubao-seed-evolving",
            1,
            batch_size=2,
            max_workers=2,
        )

    for route_name in ("r1", "r3", "r4", "r5"):
        paths = module._task_paths(project, route_name, 1)
        assert paths["raw"].exists()
        assert paths["post"].exists()
        assert json.loads(paths["meta"].read_text())["status"] == "pass"

    r2_checkpoint = json.loads(
        module._task_paths(project, "r2", 1)["checkpoint"].read_text()
    )
    assert set(r2_checkpoint["chunks"]) == {"0"}
    assert not module._task_paths(project, "r2", 1)["meta"].exists()

    resumed_calls = []

    def resumed_request(route_name, route, rows, **kwargs):
        resumed_calls.append((route_name, tuple(row["row_id"] for row in rows)))
        return fake_request(route_name, route, rows, **kwargs)

    monkeypatch.setattr(module, "_request_route", resumed_request)
    result = module.run(
        str(project),
        str(source),
        "doubao-seed-evolving",
        1,
        batch_size=2,
        max_workers=2,
    )

    assert resumed_calls == [("r2", ("c", "d"))]
    assert list(result) == list(module.ROUTES)
    assert result["r2"]["rows"] == 4
    assert result["r1"]["cached"] is True
    r2_meta = json.loads(module._task_paths(project, "r2", 1)["meta"].read_text())
    assert r2_meta["batch_size"] == 2
    assert r2_meta["chunk_count"] == 2
    assert r2_meta["input_tokens"] == 40


def test_pipeline_e_streams_with_fresh_client_and_retries_disconnect(monkeypatch):
    module = _load_pipeline_e_module()
    clients = []
    create_kwargs = []

    class _Stream:
        def __aiter__(self):
            chunks = [
                types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            delta=types.SimpleNamespace(content="洞察"),
                            finish_reason=None,
                        )
                    ],
                    usage=None,
                ),
                types.SimpleNamespace(
                    choices=[
                        types.SimpleNamespace(
                            delta=types.SimpleNamespace(content="完成"),
                            finish_reason="stop",
                        )
                    ],
                    usage=types.SimpleNamespace(
                        prompt_tokens=12,
                        completion_tokens=3,
                    ),
                ),
            ]
            return _AsyncIterator(chunks)

    class _AsyncIterator:
        def __init__(self, values):
            self._values = iter(values)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._values)
            except StopIteration as error:
                raise StopAsyncIteration from error

    class _Completions:
        def __init__(self, attempt):
            self._attempt = attempt

        async def create(self, **kwargs):
            create_kwargs.append(kwargs)
            if self._attempt == 1:
                raise ConnectionError("remote closed")
            return _Stream()

    class _Client:
        def __init__(self, attempt):
            self.chat = types.SimpleNamespace(
                completions=_Completions(attempt)
            )
            self.closed = False

        async def close(self):
            self.closed = True

    def fake_client(**kwargs):
        assert kwargs == {
            "api_key": "test-key",
            "base_url": "https://ark.example/api/v3",
            "max_retries": 0,
            "timeout": 600.0,
        }
        client = _Client(len(clients) + 1)
        clients.append(client)
        return client

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(module, "AsyncOpenAI", fake_client)
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(module.random, "uniform", lambda _start, _end: 0)

    result = asyncio.run(
        module.call_llm_async(
            "test-key",
            "https://ark.example/api/v3",
            {"id": "e1", "name": "行业话题", "max_tokens": 128},
            "prompt",
            "doubao-seed-evolving",
            max_attempts=2,
        )
    )

    assert result["status"] == "ok"
    assert result["content"] == "洞察完成"
    assert result["input_tokens"] == 12
    assert result["output_tokens"] == 3
    assert result["stop_reason"] == "stop"
    assert len(clients) == 2
    assert all(client.closed for client in clients)
    assert all(kwargs["stream"] is True for kwargs in create_kwargs)
    assert all(
        kwargs["stream_options"] == {"include_usage": True}
        for kwargs in create_kwargs
    )


def test_pipeline_e_checkpoints_each_section_and_resumes_only_failure(
    tmp_path, monkeypatch
):
    module = _load_pipeline_e_module()
    insight_dir = tmp_path / "06_洞察" / "v1"
    insight_dir.mkdir(parents=True)
    (insight_dir / "e1_data.md").write_text("E1 data", encoding="utf-8")
    (insight_dir / "ex_data.md").write_text("EX data", encoding="utf-8")
    checkpoint_path = insight_dir / "pipeline_e_checkpoint_v1.json"
    checkpoint = {
        "checkpoint_version": module.CHECKPOINT_VERSION,
        "run_signature": "test-signature",
        "model": "test-model",
        "mode": "demo",
        "version": "v1",
        "sections": {},
    }
    sections = [
        {
            "id": "e1",
            "name": "E1",
            "prompt_dir": "E1",
            "data_file": "e1_data.md",
            "section_title": "# E1",
            "max_tokens": 128,
            "output_file": "e1_v1.md",
        },
        {
            "id": "ex",  # 通用版块(e3/e4 有专属分步路由,另有测试覆盖)
            "name": "EX",
            "prompt_dir": "EX",
            "data_file": "ex_data.md",
            "section_title": "# EX",
            "max_tokens": 128,
            "output_file": "ex_v1.md",
        },
    ]
    monkeypatch.setenv("LLM_RELAY_API_KEY", "test-key")
    monkeypatch.setenv("LLM_RELAY_BASE_URL", "https://relay.example/v1")
    monkeypatch.setattr(module, "load_prompt_template", lambda _name: "{{data}}")

    output_written = asyncio.Event()
    original_write = module._write_section_output

    def observed_write(sec, result, output_path):
        original_write(sec, result, output_path)
        if sec["id"] == "e1":
            output_written.set()

    calls = []
    active = 0
    max_active = 0

    async def first_call(_key, _url, sec, _prompt, model, *, max_attempts):
        nonlocal active, max_active
        assert model == "test-model"
        assert max_attempts == 3
        calls.append(sec["id"])
        active += 1
        max_active = max(max_active, active)
        try:
            if sec["id"] == "ex":
                await output_written.wait()
                saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                assert saved["sections"]["e1"]["status"] == "ok"
                return {
                    "id": "ex",
                    "name": "EX",
                    "status": "error",
                    "error": "disconnect",
                    "content": "",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "elapsed_s": 0.1,
                    "model": model,
                    "stop_reason": None,
                }
            await asyncio.sleep(0)
            return {
                "id": "e1",
                "name": "E1",
                "status": "ok",
                "content": "E1 result",
                "input_tokens": 10,
                "output_tokens": 2,
                "elapsed_s": 0.1,
                "model": model,
                "stop_reason": "stop",
            }
        finally:
            active -= 1

    monkeypatch.setattr(module, "_write_section_output", observed_write)
    monkeypatch.setattr(module, "call_llm_async", first_call)
    first_results = asyncio.run(
        module.run_all_sections(
            tmp_path,
            insight_dir,
            "test-model",
            sections,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
            max_workers=2,
            max_attempts=3,
        )
    )

    assert [result["status"] for result in first_results] == ["ok", "error"]
    assert (insight_dir / "e1_v1.md").exists()
    assert not (insight_dir / "ex_v1.md").exists()
    assert max_active == 2

    resumed_calls = []

    async def resumed_call(_key, _url, sec, _prompt, model, *, max_attempts):
        resumed_calls.append(sec["id"])
        return {
            "id": sec["id"],
            "name": sec["name"],
            "status": "ok",
            "content": f"{sec['id']} resumed",
            "input_tokens": 8,
            "output_tokens": 2,
            "elapsed_s": 0.1,
            "model": model,
            "stop_reason": "stop",
        }

    monkeypatch.setattr(module, "call_llm_async", resumed_call)
    resumed = asyncio.run(
        module.run_all_sections(
            tmp_path,
            insight_dir,
            "test-model",
            sections,
            checkpoint=checkpoint,
            checkpoint_path=checkpoint_path,
            max_workers=2,
            max_attempts=3,
        )
    )

    assert resumed_calls == ["ex"]
    assert resumed[0]["cached"] is True
    assert resumed[1]["status"] == "ok"
    assert (insight_dir / "ex_v1.md").exists()
    saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert set(saved["sections"]) == {"e1", "ex"}


def test_c0_active_version_is_v13_and_keeps_output_contract():
    """C0 活跃版 = 客户 V2 锁定的 v13;SKILL.md 权威表、版本镜像与 Prompt 文件三处一致,9 字段契约不变。"""
    prompt = C0_V13_PROMPT.read_text(encoding="utf-8")
    for field in ("商业实体", "热点驱动词", "行业归属", "营销触发方式", "营销维度",
                  "平台原生形式", "不可用原因", "是否营销可用", "判断说明"):
        assert f'"{field}"' in prompt
    for placeholder in ("{{platform}}", "{{title}}", "{{desc}}"):
        assert placeholder in prompt
    assert sorted(p.name for p in C0_PROMPT_DIR.glob("*.md")) == ["v13.md"]  # 只保留活跃版,防误取
    ann = C0_PROMPT_DIR.parents[1]
    assert "C0 v13" in (ann / "SKILL.md").read_text(encoding="utf-8")
    status = (ann.parents[1] / "memory" / "topic6" / "_版本状态.md").read_text(encoding="utf-8")
    assert "/mnt/skills/topic6-annotation/prompts/C0_基础事实/v13.md" in status


def test_run_config_state_deep_merges_parallel_task_results(tmp_path):
    module = _load_run_config_state_module()
    config_path = tmp_path / "run_config.yaml"
    config_path.write_text(
        "mode: demo\nstatus:\n  current_phase: c_route_sample\n"
        "  c0_base:\n    status: done\n",
        encoding="utf-8",
    )

    module.update_run_config(
        tmp_path,
        {"r1_platform": {"status": "done", "row_count": 18}},
    )
    config = module.update_run_config(
        tmp_path,
        {"c2_cluster": {"status": "running", "stage": "01_eventness"}},
    )

    assert config["status"]["current_phase"] == "c_route_sample"
    assert config["status"]["c0_base"]["status"] == "done"
    assert config["status"]["r1_platform"]["row_count"] == 18
    assert config["status"]["c2_cluster"]["stage"] == "01_eventness"


def test_coordinator_uses_50_rows_for_demo_and_500_for_full():
    coordinator = COORDINATOR_PROMPT.read_text(encoding="utf-8")

    assert "mode=full" in coordinator
    assert "sample_500.py --size 500" in coordinator
    assert "mode=demo" in coordinator
    assert "sample_500.py --size 50" in coordinator
    assert "mode=skip_sampling" in coordinator


def test_datahub_agents_use_one_worker_and_short_batch_waits():
    coordinator = COORDINATOR_PROMPT.read_text(encoding="utf-8")
    annotator = ANNOTATOR_PROMPT.read_text(encoding="utf-8")

    assert "--launch-background" in annotator
    assert "立即按下方 JSON 契约 `end_turn`" in annotator
    assert "严禁自己等待、轮询、监控或启动第二个 worker" in annotator
    assert "wait_datahub_batch.py" in coordinator
    assert "--tasks c0,c3" in coordinator
    assert "--tasks r1,r2,r3,r4,r5" in coordinator
    assert "--wait-seconds 105" in coordinator
    assert coordinator.count("`timeout=115`") >= 2
    assert "低于方舟 bash 约 120 秒的强制后台阈值" in coordinator


def test_datahub_background_launch_is_idempotent(tmp_path, monkeypatch):
    module = _load_annotate_module()
    calls = []

    class FakeProcess:
        pid = __import__("os").getpid()

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    args = types.SimpleNamespace(
        task="c0",
        project_dir=str(tmp_path),
        input=str(tmp_path / "input.xlsx"),
        prompt_file=str(tmp_path / "prompt.md"),
        model_id="Doubao-Seed-1.6-lite",
        run_id=1,
        mode="full",
    )

    first = module.launch_background(args)
    second = module.launch_background(args)

    assert first["status"] == "submitted"
    assert second["status"] == "running"
    assert len(calls) == 1
    assert "--launch-background" not in calls[0][0]
    assert calls[0][1]["start_new_session"] is True


def test_datahub_background_launch_does_not_reuse_different_input(
    tmp_path, monkeypatch
):
    module = _load_annotate_module()
    task_dir = tmp_path / "04_标注" / "C0_基础事实"
    task_dir.mkdir(parents=True)
    sample = tmp_path / "03_抽样" / "sample_500.xlsx"
    full = tmp_path / "02_标准化" / "hot_topics_normalized.xlsx"
    sample.parent.mkdir(parents=True)
    full.parent.mkdir(parents=True)
    sample.touch()
    full.touch()
    (task_dir / "c0_completion_meta.json").write_text(
        json.dumps(
            {
                "run_id": 1,
                "input_file": str(sample.with_name(".sample_500_prepared.xlsx")),
            }
        ),
        encoding="utf-8",
    )
    calls = []

    class FakeProcess:
        pid = __import__("os").getpid()

    def fake_popen(command, **_kwargs):
        calls.append(command)
        return FakeProcess()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    args = types.SimpleNamespace(
        task="c0",
        project_dir=str(tmp_path),
        input=str(full),
        prompt_file=str(tmp_path / "prompt.md"),
        model_id="Doubao-Seed-1.6-lite",
        run_id=1,
        mode="skip_sampling",
    )

    result = module.launch_background(args)

    assert result["status"] == "submitted"
    assert len(calls) == 1


def test_datahub_worker_allows_day_long_server_tasks():
    module = _load_annotate_module()
    assert module.POLL_TIMEOUT == 24 * 60 * 60


def test_datahub_worker_resumes_matching_submitted_task(tmp_path, monkeypatch):
    module = _load_annotate_module()
    input_path = tmp_path / "input.xlsx"
    prompt_path = tmp_path / "prompt.md"
    pd.DataFrame({"desc": ["example"]}).to_excel(input_path, index=False)
    prompt_path.write_text("prompt", encoding="utf-8")
    task_dir = tmp_path / "04_标注" / "C0_基础事实"
    task_dir.mkdir(parents=True)
    (task_dir / "c0_submit_meta.json").write_text(
        json.dumps(
            {
                "task": "c0",
                "task_id": 5041,
                "run_id": 2,
                "model_id": "Doubao-Seed-1.6-lite",
                "platform": "ByteDance",
                "input_price": 0,
                "output_price": 0,
                "currency": "CNY",
                "prompt_file": str(prompt_path),
                "input_file": str(input_path),
                "source_input_file": str(input_path),
                "created_at": "2026-09-30T22:00:00",
            }
        ),
        encoding="utf-8",
    )
    polled = []

    monkeypatch.setenv("DATAHUB_API_KEY", "test-key")
    monkeypatch.setattr(
        module,
        "_list_models",
        lambda _key: [
            {
                "model_id": "Doubao-Seed-1.6-lite",
                "platform": "ByteDance",
                "input_price": 0,
                "output_price": 0,
            }
        ],
    )
    monkeypatch.setattr(
        module,
        "_upload_file",
        lambda *_args, **_kwargs: pytest.fail("resume must not upload input"),
    )
    monkeypatch.setattr(
        module,
        "_create_task",
        lambda *_args, **_kwargs: pytest.fail("resume must not create a task"),
    )

    def fake_poll(_api_key, task_id):
        polled.append(task_id)
        return {"result_url": "https://example.test/result.xlsx"}

    def fake_download(_url, output):
        pd.DataFrame({"llm_result": ['{"是否营销可用":"是"}']}).to_excel(
            output, index=False
        )

    monkeypatch.setattr(module, "_poll_until_done", fake_poll)
    monkeypatch.setattr(module, "_download_result", fake_download)
    monkeypatch.setattr(
        module,
        "_postprocess",
        lambda frame, _task: (
            frame,
            {
                "status": "pass",
                "total": 1,
                "valid": 1,
                "invalid": 0,
                "valid_rate": 1.0,
            },
        ),
    )
    monkeypatch.setattr(module, "update_run_config", lambda *_args, **_kwargs: None)

    result = module.annotate(
        "c0",
        str(tmp_path),
        str(input_path),
        str(prompt_path),
        "Doubao-Seed-1.6-lite",
        run_id=2,
    )

    assert polled == [5041]
    assert result["task_id"] == 5041
    assert result["source_input_file"] == str(input_path.resolve())


def test_wait_datahub_batch_reports_running_complete_and_failed(tmp_path):
    module = _load_wait_datahub_batch_module()
    c0_dir = tmp_path / "04_标注" / "C0_基础事实"
    c3_dir = tmp_path / "04_标注" / "C3_节点标注"
    c0_dir.mkdir(parents=True)
    c3_dir.mkdir(parents=True)
    (c0_dir / "c0_worker_r1.pid").write_text(
        str(__import__("os").getpid()),
        encoding="utf-8",
    )
    (c3_dir / "c3_worker_r1.pid").write_text(
        str(__import__("os").getpid()),
        encoding="utf-8",
    )

    running = module.inspect_batch(tmp_path, ["c0", "c3"], 1)
    assert running["status"] == "running"

    for task, directory in (("c0", c0_dir), ("c3", c3_dir)):
        (directory / f"{task}_completion_meta.json").write_text(
            json.dumps(
                {"run_id": 1, "total": 500, "valid_rate": 0.99}
            ),
            encoding="utf-8",
        )
    complete = module.inspect_batch(tmp_path, ["c0", "c3"], 1)
    assert complete["status"] == "complete"

    # 单路非 C0 缺失 → degraded(对齐原版 0~1 失败标记缺失继续)
    (c3_dir / "c3_completion_meta.json").unlink()
    (c3_dir / "c3_worker_r1.pid").write_text("99999999", encoding="utf-8")
    degraded = module.inspect_batch(tmp_path, ["c0", "c3"], 1)
    assert degraded["status"] == "degraded"
    assert degraded["tasks"]["c3"]["status"] == "missing"
    assert degraded["missing_tasks"] == ["c3"]

    # C0 缺失 → failed(筛选依赖 C0)
    (c0_dir / "c0_completion_meta.json").unlink()
    (c0_dir / "c0_worker_r1.pid").write_text("99999999", encoding="utf-8")
    failed = module.inspect_batch(tmp_path, ["c0", "c3"], 1)
    assert failed["status"] == "failed"


def test_coordinator_uses_cross_platform_c2_flow_and_merge_contract():
    coordinator = COORDINATOR_PROMPT.read_text(encoding="utf-8")

    required_steps = [
        "run_topic6_c2.py",
        "`run_c2.py run --mode cross-platform --frontend legacy --member-review off`",
        "聚簇语义对齐客户原版 00→09",
        "`--review-policy record`",
        "`--degraded-policy accept`",
        "c2_event_result_r{N}.xlsx",
    ]
    assert all(step in coordinator for step in required_steps)
    assert "Agent 不得直接调用 `run_c2.py` 或编号脚本" in coordinator
    assert "四平台并行 `00_clean_titles.py`" not in coordinator
    assert "禁止把 `00_seed_from_registry.py` 当成 C2 起点" in coordinator
    assert "`run_topic6_c2.py` 和合并脚本一律传 `--mode skip_sampling`" in coordinator
    assert "严禁继续把顶层 `mode=full` 传给 C2" in coordinator
    assert "`feishu_doc_url` 必须是非空的飞书 `/docx/` URL" in coordinator
    assert "严禁用本地 Markdown 路径代替飞书文档并进入 HC3" in coordinator
    assert "output=`04_标注/c2_raw.jsonl`" not in coordinator


def test_coordinator_uses_demo_fast_paths_and_single_insight_entry():
    coordinator = COORDINATOR_PROMPT.read_text(encoding="utf-8")

    assert "run_demo_routes.py" in coordinator
    assert "run_demo_batch2.py" in coordinator
    assert "wait_c2_complete.py" in coordinator
    assert "launch_c2_background.py" in coordinator
    assert "publish_report_to_feishu.py" in coordinator
    assert "Phase E 不委派子 Agent" in coordinator
    assert "--publish-date \"{publish_date}\" --version {N}" in coordinator
    assert "最多重跑一次同一正式入口" in coordinator
    assert "禁止用 inline Python" in coordinator
    assert "禁止精简或改写 R1~R5 Prompt" in coordinator
    assert "流式有限并发、断连重试和版块级 checkpoint" in coordinator
    assert "最多\n  原参数重跑一次上述同一正式入口" in coordinator
    assert "禁止拆成\n  四次 `--sections` 调用" in coordinator


def test_pipeline_overview_documents_demo_execution_differences():
    overview = PIPELINE_OVERVIEW.read_text(encoding="utf-8")

    assert "demo / full / skip_sampling 模式" in overview
    assert "分层抽样 50 条" in overview
    assert "R1~R5 Ark 批量 + C2 单次归并" in overview
    assert "跳过全量与 HC2" in overview
    assert "run_demo_routes.py" in overview
    assert "run_topic6_c2.py --mode demo --demo-fast" in overview
    assert "Phase E · 单入口调度" in overview
    assert "保持脚本单入口" in overview


def test_phase_f_uses_runtime_identity_and_response_url_without_secret_output():
    coordinator = COORDINATOR_PROMPT.read_text(encoding="utf-8")
    prompt = PHASE_F_PROMPT.read_text(encoding="utf-8")

    # 单入口脚本负责 lark-cli 主路径 + fallback，prompt 只描述契约，不再内嵌命令
    assert "publish_report_to_feishu.py" in prompt
    assert "--dry-run` 恒成功" in prompt
    assert "不需要也不得执行 `lark-cli config init`" in prompt
    assert "给发起人授 `edit` 失败" in prompt
    assert "记入 `warnings`，不阻断" in prompt
    assert "删除上传的 md 源文件" in prompt
    assert "OPERATOR_OID" not in prompt
    assert "严禁执行 `lark-cli auth login`" in prompt
    assert "降级为 `--as user`" in prompt
    assert "任一步失败不得输出 HC3" in coordinator or "Phase F 失败，不输出 HC3" in coordinator
    assert "FEISHU_HOTREPORT_FOLDER_TOKEN" not in prompt
    assert "CC_SESSION_KEY" not in prompt
    assert "bluefocus.feishu.cn" not in prompt
    assert "禁止用 `env`、`printenv`、`set`、`export -p`" in coordinator
    assert "禁止开启 `set -x`" in prompt


def test_environment_preinstalls_openai_for_insight_pipeline():
    environment = json.loads(ENVIRONMENT_CONFIG.read_text(encoding="utf-8"))

    assert "openai>=1.0" in environment["config"]["packages"]["pip"]
    assert "httpx>=0.27" in environment["config"]["packages"]["pip"]
    assert environment["config"]["env"]["DATAHUB_MODEL_ID"] == "DeepSeek-V4-Flash"
    assert environment["config"]["env"]["C2_CHAT_MODEL_ID"] == "DeepSeek-V4.1-Flash"
    assert (
        environment["config"]["env"]["EMBEDDING_MODEL_ID"]
        == "Doubao-embedding"
    )
    assert environment["config"]["env"]["EMBEDDING_IS_MULTIMODAL"] == "false"


def test_pipeline_f_does_not_duplicate_date_range_in_period_label():
    module = _load_pipeline_f_module()

    assert module.format_period_display(
        "W39 热点周报 (2026-09-21 ~ 2026-09-27)",
        "2026-09-21",
        "2026-09-27",
    ) == "W39 热点周报 (2026-09-21 ~ 2026-09-27)"


def test_e2_nodes_reads_shared_csv_calendar(monkeypatch):
    module = _load_e2_nodes_module()
    monkeypatch.setattr(module, "CALENDAR_PATH", MARKETING_CALENDAR)
    calendar = dict(
        (name, node_date)
        for node_date, name, _node_type in module._load_calendar_rows()
    )

    assert calendar["世界心脏日"].isoformat() == "2026-09-29"


def test_pipeline_f_marks_demo_report_as_sample_only(tmp_path):
    project = tmp_path / "W39_demo"
    round_dir = project / "06_洞察" / "v1"
    round_dir.mkdir(parents=True)
    for section in ("e1", "e2", "e3", "e4"):
        (round_dir / f"{section}_v1.md").write_text(
            f"# {section}\n内容", encoding="utf-8"
        )

    result = subprocess.run(
        [
            sys.executable,
            str(PIPELINE_F_SCRIPT),
            "--project-dir",
            str(project),
            "--mode",
            "demo",
            "--period-label",
            "2026-W39",
            "--date-start",
            "2026-09-21",
            "--date-end",
            "2026-09-27",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = project / "07_报告" / "热点报告_2026-W39_v1.md"
    report_text = report.read_text(encoding="utf-8")
    assert "基于 50 条分层样本生成" in report_text
    assert "> 数据范围：微博、知乎、抖音、B站" in report_text
    assert "> 数据周期：2026-W39（2026-09-21 ~ 2026-09-27）" in report_text


def test_web_report_uses_actual_period_and_has_no_w32_fallback(tmp_path):
    blocks = json.loads(WEB_REPORT_EXAMPLE.read_text(encoding="utf-8"))
    blocks[0]["text"] = (
        "数据范围:微博、知乎、抖音、B站\n"
        "数据周期:2026-W39 (2026-09-21 ~ 2026-09-27)"
    )
    source = tmp_path / "source.example.json"
    output = tmp_path / "index.html"
    source.write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        ["node", str(WEB_REPORT_BUILDER), str(source), str(output)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    html = output.read_text(encoding="utf-8")
    assert "2026·W39" in html
    assert "2026-09-21 至 2026-09-27" in html
    assert "2026·W32" not in html


def test_web_report_rejects_missing_period_instead_of_using_example_week(tmp_path):
    blocks = json.loads(WEB_REPORT_EXAMPLE.read_text(encoding="utf-8"))
    blocks[0]["text"] = "数据范围：微博、知乎、抖音、B站"
    source = tmp_path / "source.example.json"
    output = tmp_path / "index.html"
    source.write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        ["node", str(WEB_REPORT_BUILDER), str(source), str(output)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Missing report period" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("model", ["Doubao-Seed-Evolving", "doubao-seed-evolving", "DOUBAO-SEED-EVOLVING-260628"])
def test_pricing_lookup_is_case_insensitive(model):
    """环境变量模型 ID 大小写与定价表不一致时，三处计价都不能落到 0。"""
    relay = _load_relay_module()
    pipeline_e = _load_pipeline_e_module()
    demo = _load_demo_routes_module()
    assert relay.price_of(model) == relay.PRICING["doubao-seed-evolving"]
    assert pipeline_e._price_of(model) == pipeline_e.PRICING["doubao-seed-evolving"]
    assert demo._price_cny(model, 1_000_000, 0) == 6.0


def test_pricing_lookup_unknown_model_stays_zero():
    relay = _load_relay_module()
    pipeline_e = _load_pipeline_e_module()
    assert relay.price_of("unknown-model-x") == (0.0, 0.0)
    assert pipeline_e._price_of("unknown-model-x") == (0.0, 0.0)


def test_web_report_renders_placeholder_for_cases_without_images(tmp_path):
    """Phase G 配图非阻断：grid-block 案例缺图时仍渲染左文右图卡片，图片位为「配图待补充」占位。"""
    blocks = json.loads(WEB_REPORT_EXAMPLE.read_text(encoding="utf-8"))
    blocks[0]["text"] = (
        "数据范围:微博、知乎、抖音、B站\n"
        "数据周期:2026-W39 (2026-09-21 ~ 2026-09-27)"
    )
    grid = [b for b in blocks if "docx-grid-block" in str(b.get("className", ""))]
    assert len(grid) == 4, "示例里应含 4 个 grid-block 案例"
    grid[1]["images"] = []  # 案例2 缺图,其余保留源文档图
    source = tmp_path / "source.json"  # 非 example 文件名 → 不允许回落模板图
    output = tmp_path / "index.html"
    source.write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
    for b in grid:
        if b["images"]:
            b["images"][0]["src"] = str(WEB_REPORT_DIR / "assets" / "source" / "assets" / Path(b["images"][0]["src"]).name)
    source.write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")

    check = subprocess.run(
        ["node", str(WEB_REPORT_DIR / "scripts" / "validate-case-image-sources.mjs"), str(source)],
        check=False, capture_output=True, text=True,
    )
    assert check.returncode == 0, check.stderr
    assert "1 case(s) use the image placeholder" in check.stdout

    build = subprocess.run(
        ["node", str(WEB_REPORT_BUILDER), str(source), str(output)],
        check=False, capture_output=True, text=True,
    )
    assert build.returncode == 0, build.stderr
    html = output.read_text(encoding="utf-8")
    assert html.count('class="case-card case-card--visual"') == 4
    assert html.count('class="case-visual case-visual--placeholder"') == 1
    assert "配图待补充" in html


def test_coordinator_phase_g_documents_no_image_rule():
    text = COORDINATOR_PROMPT.read_text(encoding="utf-8")
    phase_g = text[text.index("### Phase G"):text.index("### Phase H")]
    for needle in ("fetch_case_images.py", "--case-images", "配图待补充", "status.g.case_images_missing", "严禁调用生图工具"):
        assert needle in phase_g
