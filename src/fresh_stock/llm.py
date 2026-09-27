"""LLM 호출 계층.

앱 코드는 LLMClient.generate_json만 쓴다. 백엔드를 바꿔도 나머지 코드는 그대로다.

- ClaudeCodeClient: 임시 백엔드. 로컬 Claude Code CLI(`claude -p`)를 호출해서 API 키 없이
  Claude Code 로그인으로 동작한다.
- API 키가 생기면 Anthropic SDK 백엔드를 추가해 갈아 끼운다.
"""

import base64
import json
import shutil
import subprocess
import threading
import time
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
                cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=self.timeout
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
        # 재고표 평가처럼 여러 스레드에서 동시에 부를 수 있다
        with _log_lock:
            self.calls.append(call)
            if self.log_path:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(call.__dict__, ensure_ascii=False) + "\n")
