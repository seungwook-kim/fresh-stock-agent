"""LLM 호출 계층.

앱 코드는 LLMClient.generate_json만 쓴다. 백엔드를 바꿔도 나머지 코드는 그대로다.

- ClaudeCodeClient: 로컬 Claude Code CLI(`claude -p`)를 호출해서 API 키 없이 Claude Code 로그인으로
  동작한다. 개발 중 임시 백엔드이자 기본값이다.
- AnthropicClient: Anthropic 공식 Python SDK. API 키가 생기면 이쪽으로 바꾼다.
"""

import base64
import json
import shutil
import subprocess
import threading
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

_log_lock = threading.Lock()


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageInput:
    data: bytes
    media_type: str = "image/jpeg"


@dataclass(frozen=True)
class LLMCall:
    """호출 한 번의 기록. 비용·지연 추적과 실패 분석에 쓴다."""

    purpose: str
    system: str
    prompt: str
    images: int  # 함께 보낸 이미지 수 (원본은 기록에 남기지 않는다)
    output: dict[str, Any] | None
    error: str | None
    input_tokens: int
    output_tokens: int
    seconds: float
    at: str = ""  # 호출 시각


class LLMClient(Protocol):
    calls: list[LLMCall]

    def generate_json(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        purpose: str = "",
        images: tuple[ImageInput, ...] = (),
    ) -> dict[str, Any]:
        """schema에 맞는 JSON 객체를 돌려준다. 실패하면 LLMError."""
        ...


@dataclass
class ClaudeCodeClient:
    model: str = "sonnet"
    effort: str | None = "low"  # 추론 강도. 발주 조정처럼 짧은 판단은 low로 충분하다
    timeout: float = 300
    log_path: Path | None = None  # 지정하면 호출마다 JSONL로 남긴다
    calls: list[LLMCall] = field(default_factory=list)

    def generate_json(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        purpose: str = "",
        images: tuple[ImageInput, ...] = (),
    ) -> dict[str, Any]:
        exe = shutil.which("claude")
        if exe is None:
            raise LLMError("claude CLI를 찾을 수 없습니다. Claude Code가 설치돼 있어야 합니다.")
        # --safe-mode --strict-mcp-config: 사용자 설정·MCP 도구가 프롬프트에 섞이지 않게 한다
        cmd = [
            exe, "-p",
            "--safe-mode", "--strict-mcp-config", "--no-session-persistence",
            "--tools", "",
            "--model", self.model,
            "--system-prompt", system,
            "--json-schema", json.dumps(schema, ensure_ascii=False),
        ]  # fmt: skip
        if self.effort:
            cmd += ["--effort", self.effort]
        if images:
            # 이미지는 stream-json 입력으로만 넘길 수 있다
            cmd += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.media_type,
                        "data": base64.b64encode(img.data).decode(),
                    },
                }
                for img in images
            ]
            content.append({"type": "text", "text": prompt})
            stdin = json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"
        else:
            cmd += ["--output-format", "json"]
            stdin = prompt

        started = time.monotonic()
        output, error, usage = None, None, {}
        try:
            proc = subprocess.run(
                cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=self.timeout, check=False
            )
            data = self._result(proc.stdout)
            usage = data.get("usage", {})
            output = data.get("structured_output")
            if data.get("is_error") or output is None:
                error = str(data.get("result") or proc.stderr)[:500]
        except (subprocess.TimeoutExpired, json.JSONDecodeError, LLMError) as e:
            error = f"{type(e).__name__}: {e}"[:500]
        self._record(
            LLMCall(
                purpose=purpose,
                system=system,
                prompt=prompt,
                images=len(images),
                output=output,
                error=error,
                input_tokens=usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                seconds=round(time.monotonic() - started, 2),
                at=datetime.now().isoformat(timespec="seconds"),
            )
        )
        if error:
            raise LLMError(error)
        return output

    @staticmethod
    def _result(stdout: str) -> dict[str, Any]:
        """json 출력은 객체 하나, stream-json 출력은 줄마다 이벤트이고 마지막 result 이벤트에 결과가 있다."""
        for line in reversed(stdout.strip().splitlines()):
            event = json.loads(line)
            if event.get("type") == "result":
                return event
        raise LLMError("CLI 출력에 result가 없습니다.")

    def _record(self, call: LLMCall) -> None:
        _record(self.calls, self.log_path, call)


def _record(calls: list[LLMCall], log_path: Path | None, call: LLMCall) -> None:
    # 재고표 평가처럼 여러 스레드에서 동시에 부를 수 있다
    with _log_lock:
        calls.append(call)
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(call.__dict__, ensure_ascii=False) + "\n")


# Claude Code 모델 별칭을 API 모델 ID로 바꿀 때 쓴다
API_MODEL_ALIASES = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}


@dataclass
class AnthropicClient:
    """Anthropic 공식 Python SDK 백엔드. ANTHROPIC_API_KEY나 `ant auth login` 인증으로 동작한다.

    claude-opus-5는 안전 분류기가 요청을 거절하면 서버가 다른 모델로 다시 처리하도록
    fallbacks="default"를 켠다.
    """

    model: str = "claude-opus-5"
    effort: str | None = "low"
    max_tokens: int = 16000
    log_path: Path | None = None
    calls: list[LLMCall] = field(default_factory=list)
    client: Any = None  # 테스트에서 가짜 클라이언트를 넣을 수 있다

    def generate_json(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        purpose: str = "",
        images: tuple[ImageInput, ...] = (),
    ) -> dict[str, Any]:
        import anthropic

        if self.client is None:
            self.client = anthropic.Anthropic()
        content: Any = prompt
        if images:
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.media_type,
                        "data": base64.standard_b64encode(img.data).decode("utf-8"),
                    },
                }
                for img in images
            ] + [{"type": "text", "text": prompt}]
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        request = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "output_config": output_config,
        }

        started = time.monotonic()
        output, error, usage = None, None, None
        try:
            if self.model == "claude-opus-5":
                response = self.client.beta.messages.create(
                    **request, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
            else:
                response = self.client.messages.create(**request)
            usage = response.usage
            if response.stop_reason == "refusal":
                error = "모델이 요청을 거절했습니다 (refusal)."
            elif response.stop_reason == "max_tokens":
                error = "출력이 max_tokens에서 잘렸습니다."
            else:
                text = next(b.text for b in response.content if b.type == "text")
                output = json.loads(text)
        except (anthropic.APIStatusError, anthropic.APIConnectionError, json.JSONDecodeError, StopIteration) as e:
            error = f"{type(e).__name__}: {e}"[:500]
        _record(
            self.calls,
            self.log_path,
            LLMCall(
                purpose=purpose,
                system=system,
                prompt=prompt,
                images=len(images),
                output=output,
                error=error,
                input_tokens=(usage.input_tokens + (usage.cache_creation_input_tokens or 0)
                              + (usage.cache_read_input_tokens or 0)) if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                seconds=round(time.monotonic() - started, 2),
                at=datetime.now().isoformat(timespec="seconds"),
            ),
        )  # fmt: skip
        if error:
            raise LLMError(error)
        return output


def make_llm(backend: str, model: str | None, effort: str | None, log_path: Path | None) -> LLMClient:
    """backend: "claude-code" (로컬 Claude Code CLI, 기본) 또는 "anthropic" (API)."""
    if backend == "anthropic":
        api_model = API_MODEL_ALIASES.get(model or "opus", model or "claude-opus-5")
        return AnthropicClient(model=api_model, effort=effort, log_path=log_path)
    if backend == "claude-code":
        return ClaudeCodeClient(model=model or "sonnet", effort=effort, log_path=log_path)
    raise ValueError(f"알 수 없는 LLM 백엔드: {backend}")
