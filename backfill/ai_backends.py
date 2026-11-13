"""Ways to reach the ad-classifying model: each returns an `ask_model(prompt, model) -> str`.

    openai    POST {PA_CLEAN_API_URL}/chat/completions on any OpenAI-compatible server:
              a local `mlx_lm.server` on this Mac, OpenRouter, OpenCode Zen, ... No CLI,
              no agent, no tools: one HTTP request per transcript chunk.
    hermes    the Hermes CLI, as on the Pi (clean_transcript_ai.hermes_ask).
    opencode  `opencode run`, with the user's global opencode config kept out.

`PA_CLEAN_BACKEND=auto` (default) picks openai when PA_CLEAN_API_URL is set, else Hermes
when it is installed, else opencode. The prompt carries untrusted transcript text, so no
backend here gives the model tools.
"""
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

from clean_transcript_ai import hermes_ask

CLASSIFIER_AGENT = "pa-ad-classifier"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
THINKING = re.compile(r"<think>.*?</think>", re.DOTALL)


def http_post_json(url, body, headers, timeout):
    """POST JSON; returns (status, text). The only network call here."""
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def openai_ask(config, base_url, api_key="", *, max_tokens=4096, post=http_post_json):
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def ask(prompt, model):
        status, raw = post(url, {"model": model, "temperature": 0, "max_tokens": max_tokens,
                                 "messages": [{"role": "user", "content": prompt}]},
                           headers, config.timeout)
        if status != 200:
            raise RuntimeError(f"HTTP {status} from {url}: {raw[:500]}")
        try:
            content = json.loads(raw)["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected response from {url}: {raw[:500]}") from exc
        return THINKING.sub("", content)

    return ask


def opencode_supports(opencode_path, flag, *, run=subprocess.run):
    try:
        out = run([opencode_path, "run", "--help"], capture_output=True, text=True, timeout=60)
        return flag in (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return False


def opencode_ask(config, opencode_path, workdir, *, run=subprocess.run):
    """`opencode run` with a private, empty config directory, so the user's global plugins
    (Hindsight, ...), MCP servers and AGENTS.md are not loaded; login lives in opencode's
    data directory and is unaffected. The classifier agent is defined inline with every
    permission denied and every tool disabled, and runs in an empty directory."""
    agent = {"agent": {CLASSIFIER_AGENT: {
        "mode": "primary", "description": "tool-less podcast ad-segment classifier",
        "permission": {"*": "deny"}, "tools": {"*": False}}}}
    config_home, cwd = workdir / "opencode-config", workdir / "opencode-empty"
    env = {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(agent),
           "XDG_CONFIG_HOME": str(config_home), "HINDSIGHT_DISABLE_HOOKS": "1"}
    # --archived keeps these sessions out of opencode's session list; older builds lack it.
    archived = ["--archived"] if opencode_supports(opencode_path, "--archived", run=run) else []

    def ask(prompt, model):
        (config_home / "opencode").mkdir(parents=True, exist_ok=True)
        cwd.mkdir(parents=True, exist_ok=True)
        cmd = [opencode_path, "run", "--standalone", *archived, "--agent", CLASSIFIER_AGENT,
               "-m", f"{config.provider}/{model}", prompt]
        proc = run(cmd, capture_output=True, text=True, timeout=config.timeout, cwd=cwd, env=env)
        out = ANSI.sub("", proc.stdout or "")
        if proc.returncode or "Error:" in out[:200]:
            detail = ANSI.sub("", (proc.stderr or "") + out).strip()
            raise RuntimeError(f"opencode rc={proc.returncode}: {detail[-800:]}")
        return out

    return ask


def choose(config, workdir, env=None):
    """(name, ask_model) for PA_CLEAN_BACKEND = auto | openai | hermes | opencode."""
    env = os.environ if env is None else env
    choice = env.get("PA_CLEAN_BACKEND", "auto")
    api_url = env.get("PA_CLEAN_API_URL", "")
    opencode_path = env.get("PA_CLEAN_OPENCODE") or shutil.which("opencode")
    if choice == "openai" or (choice == "auto" and api_url):
        if not api_url:
            raise RuntimeError("PA_CLEAN_BACKEND=openai needs PA_CLEAN_API_URL")
        return "openai", openai_ask(config, api_url, env.get("PA_CLEAN_API_KEY", ""),
                                    max_tokens=int(env.get("PA_CLEAN_MAX_TOKENS", "4096")))
    if choice == "hermes" or (choice == "auto" and os.access(config.hermes_path, os.X_OK)):
        return "hermes", hermes_ask(config)
    if choice in ("opencode", "auto") and opencode_path:
        return f"opencode ({opencode_path})", opencode_ask(config, opencode_path, workdir)
    raise RuntimeError("no AI cleaner: set PA_CLEAN_API_URL, or install Hermes or opencode")
