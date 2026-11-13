import json
import subprocess

import pytest

import ai_backends
from clean_transcript_ai import CleanerConfig

CONFIG = CleanerConfig(provider="opencode-go", model="m1", timeout=5,
                       hermes_path="/nonexistent/hermes")


class FakeRun:
    """Stands in for subprocess.run: answers `run --help`, records classifier calls."""

    def __init__(self, help_text, rc=0, stdout='{"ad_segment_ids":[]}', stderr=""):
        self.help_text, self.rc, self.stdout, self.stderr = help_text, rc, stdout, stderr
        self.calls = []

    def __call__(self, cmd, **kw):
        if cmd[1:] == ["run", "--help"]:
            return subprocess.CompletedProcess(cmd, 0, self.help_text, "")
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, self.rc, self.stdout, self.stderr)


def test_opencode_without_archived_flag_still_works(tmp_path):
    """v2.2.5 has no --archived: passing it was the 'Unrecognized flag' failure."""
    run = FakeRun("FLAGS\n  --standalone\n  --agent string\n")
    ask = ai_backends.opencode_ask(CONFIG, "/bin/opencode", tmp_path, run=run)
    assert ask("PROMPT", "beam-free") == '{"ad_segment_ids":[]}'
    cmd, kw = run.calls[0]
    assert "--archived" not in cmd
    assert cmd[-3:] == ["-m", "opencode-go/beam-free", "PROMPT"]


def test_opencode_runs_without_the_users_global_config_or_tools(tmp_path):
    run = FakeRun("  --archived   Create the session archived\n  --standalone\n")
    ai_backends.opencode_ask(CONFIG, "/bin/opencode", tmp_path, run=run)("P", "m1")
    cmd, kw = run.calls[0]
    assert "--archived" in cmd and "--standalone" in cmd
    env = kw["env"]
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "opencode-config")
    assert (tmp_path / "opencode-config" / "opencode").is_dir()
    assert env["HINDSIGHT_DISABLE_HOOKS"] == "1"
    agent = json.loads(env["OPENCODE_CONFIG_CONTENT"])["agent"]["pa-ad-classifier"]
    assert agent["permission"] == {"*": "deny"} and agent["tools"] == {"*": False}
    assert kw["cwd"] == tmp_path / "opencode-empty"


def test_opencode_failure_reports_stderr(tmp_path):
    run = FakeRun("", rc=1, stdout="", stderr="ERROR\n  Unrecognized flag: --x")
    ask = ai_backends.opencode_ask(CONFIG, "/bin/opencode", tmp_path, run=run)
    with pytest.raises(RuntimeError, match="Unrecognized flag"):
        ask("P", "m1")


class FakeServer:
    def __init__(self, status=200, content='{"ad_segment_ids":[3]}'):
        self.status, self.content, self.requests = status, content, []

    def __call__(self, url, body, headers, timeout):
        self.requests.append((url, body, headers))
        if self.status != 200:
            return self.status, '{"error": "overloaded"}'
        return 200, json.dumps({"choices": [{"message": {"content": self.content}}]})


def test_openai_backend_sends_one_chat_request_and_strips_thinking():
    server = FakeServer(content="<think>{not this}</think>\n{\"ad_segment_ids\":[3]}")
    ask = ai_backends.openai_ask(CONFIG, "http://127.0.0.1:8080/v1/", "sk-test", post=server)
    assert ask("PROMPT", "qwen") == '\n{"ad_segment_ids":[3]}'
    url, body, headers = server.requests[0]
    assert url == "http://127.0.0.1:8080/v1/chat/completions"
    assert body["model"] == "qwen" and body["temperature"] == 0
    assert body["messages"] == [{"role": "user", "content": "PROMPT"}]
    assert headers == {"Authorization": "Bearer sk-test"}


def test_openai_backend_without_key_sends_no_auth_and_raises_on_errors():
    server = FakeServer(status=503)
    ask = ai_backends.openai_ask(CONFIG, "http://127.0.0.1:8080/v1", post=server)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        ask("P", "m")
    assert server.requests[0][2] == {}


def test_choice_of_backend(tmp_path):
    assert ai_backends.choose(CONFIG, tmp_path, {"PA_CLEAN_API_URL": "http://x/v1"})[0] \
        == "openai"
    assert ai_backends.choose(CONFIG, tmp_path, {"PA_CLEAN_OPENCODE": "/bin/true"})[0] \
        .startswith("opencode")
    with pytest.raises(RuntimeError, match="PA_CLEAN_API_URL"):
        ai_backends.choose(CONFIG, tmp_path, {"PA_CLEAN_BACKEND": "openai"})
