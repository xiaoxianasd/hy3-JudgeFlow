from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol import answer_schema_example, extract_json_object, validate_answer_shape


class Hy3APIError(RuntimeError):
    pass


def _load_env_file() -> None:
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class Hy3Config:
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = "EMPTY"
    model: str = "hy3"
    timeout_seconds: float = 180.0
    max_tokens: int = 8192
    temperature: float = 0.9
    top_p: float = 1.0

    @classmethod
    def from_env(cls) -> "Hy3Config":
        _load_env_file()
        return cls(
            base_url=os.getenv("HY3_BASE_URL", cls.base_url).rstrip("/"),
            api_key=os.getenv("HY3_API_KEY", cls.api_key),
            model=os.getenv("HY3_MODEL", cls.model),
            timeout_seconds=float(os.getenv("HY3_TIMEOUT_SECONDS", str(cls.timeout_seconds))),
            max_tokens=int(os.getenv("HY3_MAX_TOKENS", str(cls.max_tokens))),
            temperature=float(os.getenv("HY3_TEMPERATURE", str(cls.temperature))),
            top_p=float(os.getenv("HY3_TOP_P", str(cls.top_p))),
        )


class Hy3Client:
    """Small dependency-free client for Hy3's OpenAI-compatible endpoint."""

    def __init__(self, config: Hy3Config | None = None):
        self.config = config or Hy3Config.from_env()

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = self.config.base_url + path
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="GET" if payload is None else "POST",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1000]
            raise Hy3APIError(f"Hy3 HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise Hy3APIError(f"Hy3 request failed: {exc}") from exc

    def health(self) -> dict[str, Any]:
        started = time.perf_counter()
        response = self._request("/models")
        model_ids = [item.get("id") for item in response.get("data", [])]
        return {
            "ok": self.config.model in model_ids or bool(model_ids),
            "endpoint": self.config.base_url,
            "requested_model": self.config.model,
            "available_models": model_ids,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        reasoning_effort: str = "high",
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": max_tokens or self.config.max_tokens,
        }
        if "tokenhub" in self.config.base_url.lower():
            # Tencent Cloud TokenHub Chat Completions extension.
            payload["reasoning_effort"] = reasoning_effort
        else:
            # Official self-hosted Hy3/vLLM sends this field after the OpenAI
            # SDK merges its ``extra_body`` argument into the raw request.
            payload["chat_template_kwargs"] = {"reasoning_effort": reasoning_effort}
        started = time.perf_counter()
        response = self._request("/chat/completions", payload)
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise Hy3APIError(f"Unexpected Hy3 response: {str(response)[:1000]}") from exc
        content = message.get("content") or message.get("reasoning_content") or ""
        if not content:
            raise Hy3APIError("Hy3 returned neither content nor reasoning_content")
        return {
            "content": content,
            "reasoning_content": message.get("reasoning_content"),
            "usage": response.get("usage", {}),
            "model": response.get("model", self.config.model),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "request_id": response.get("id"),
        }

    def solve(self, problem: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        system = (
            "你是算法竞赛解题器。必须给出可验证的完整过程，不得只给最终答案。"
            "把题意、算法、正确性证明、复杂度和边界分成编号步骤。"
            "只输出一个合法 JSON 对象，不要 Markdown 围栏。代码必须定义题目指定函数，"
            "输入为一个 case 字典，不读写 stdin/stdout。"
        )
        user = (
            f"题目：{problem['title']}\n{problem['statement']}\n"
            f"输入结构：{json.dumps(problem['input_schema'], ensure_ascii=False)}\n"
            f"约束：{json.dumps(problem['constraints'], ensure_ascii=False)}\n"
            f"必须定义函数：{problem['function_name']}(case)\n"
            "输出必须严格符合此示例结构：\n"
            + answer_schema_example(problem["function_name"])
        )
        metadata = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            reasoning_effort="high",
        )
        answer = extract_json_object(metadata["content"])
        errors = validate_answer_shape(answer)
        if errors:
            raise Hy3APIError("Hy3 answer schema invalid: " + "; ".join(errors))
        return answer, metadata

    def review(
        self,
        problem: dict[str, Any],
        answer: dict[str, Any],
        evidence: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        system = (
            "你是严格的过程评估器。逐步核验推理，不因最终答案正确就默认过程正确。"
            "找最早出现的实质错误；若所有步骤可成立则 first_error_step 为 null。"
            "只输出合法 JSON，不要 Markdown。允许的 error_type：problem_misread, "
            "concept_error, algorithm_error, theorem_misuse, condition_omission, "
            "calculation_error, unjustified_jump, circular_reasoning, complexity_error, "
            "hallucination, implementation_error, format_error。"
        )
        compact_evidence = {
            "fixed_tests_all_passed": evidence.get("execution", {}).get("all_passed"),
            "failed_tests": [
                item["name"]
                for item in evidence.get("execution", {}).get("tests", [])
                if not item.get("passed")
            ],
            "hypothesis": evidence.get("hypothesis"),
            "rule_criteria": evidence.get("criteria"),
        }
        schema = {
            "step_reviews": [
                {"step": 1, "valid": True, "error_type": None, "reason": "..."}
            ],
            "process_correct": True,
            "first_error_step": None,
            "error_types": [],
            "confidence": 0.0,
            "rationale": "...",
        }
        user = (
            f"题目：{problem['statement']}\n"
            f"标准解法步骤：{json.dumps(problem['gold_steps'], ensure_ascii=False)}\n"
            f"待评答案：{json.dumps(answer, ensure_ascii=False)}\n"
            f"自动验证证据：{json.dumps(compact_evidence, ensure_ascii=False)}\n"
            f"输出结构：{json.dumps(schema, ensure_ascii=False)}"
        )
        metadata = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            reasoning_effort="high",
        )
        review = extract_json_object(metadata["content"])
        if not isinstance(review.get("process_correct"), bool):
            raise Hy3APIError("Hy3 review missing boolean process_correct")
        return review, metadata

    def review_stage(
        self,
        problem: dict[str, Any],
        answer: dict[str, Any],
        evidence: dict[str, Any],
        *,
        agent_name: str,
        stage: str,
        responsibility: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run one independent specialist judge for a single reasoning stage."""
        target_steps = [
            item for item in answer.get("reasoning_steps", []) if item.get("stage") == stage
        ]
        gold_steps = [item for item in problem["gold_steps"] if item.get("stage") == stage]
        system = (
            f"你是多Agent评估系统中的独立专家 {agent_name}。你的唯一职责是：{responsibility}。"
            "不要因为最终代码通过测试就默认本阶段正确，也不要代替其他专家审查无关阶段。"
            "判断必须引用题目条件、标准过程或可执行证据。若错误来自更早阶段，标记 inherited_from_step，"
            "不要把传播错误冒充新的根因。只输出合法JSON。允许的error_type：problem_misread, "
            "concept_error, algorithm_error, theorem_misuse, condition_omission, calculation_error, "
            "unjustified_jump, circular_reasoning, complexity_error, hallucination, implementation_error。"
        )
        compact_evidence = {
            "fixed_tests_all_passed": evidence.get("execution", {}).get("all_passed"),
            "failed_tests": [
                item["name"]
                for item in evidence.get("execution", {}).get("tests", [])
                if not item.get("passed")
            ],
            "hypothesis": evidence.get("hypothesis"),
            "rule_criteria_for_stage": [
                item for item in evidence.get("criteria", []) if item.get("stage") == stage
            ],
        }
        schema = {
            "agent": agent_name,
            "stage": stage,
            "reviewed_steps": [1],
            "valid": True,
            "first_error_step": None,
            "error_type": None,
            "reason": "...",
            "evidence": ["..."],
            "inherited_from_step": None,
            "confidence": 0.0,
        }
        user = (
            f"题目：{problem['statement']}\n"
            f"约束：{json.dumps(problem['constraints'], ensure_ascii=False)}\n"
            f"本专家目标步骤：{json.dumps(target_steps, ensure_ascii=False)}\n"
            f"本阶段标准过程：{json.dumps(gold_steps, ensure_ascii=False)}\n"
            f"完整待评答案：{json.dumps(answer, ensure_ascii=False)}\n"
            f"自动验证证据：{json.dumps(compact_evidence, ensure_ascii=False)}\n"
            f"输出结构：{json.dumps(schema, ensure_ascii=False)}"
        )
        metadata = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            reasoning_effort="high",
            max_tokens=min(self.config.max_tokens, 4096),
        )
        review = extract_json_object(metadata["content"])
        if not isinstance(review.get("valid"), bool):
            raise Hy3APIError(f"{agent_name} review missing boolean valid")
        review["agent"] = agent_name
        review["stage"] = stage
        return review, metadata

    def arbitrate_reviews(
        self,
        problem: dict[str, Any],
        answer: dict[str, Any],
        evidence: dict[str, Any],
        specialist_reviews: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """One bounded Swarm round: resolve specialist/evidence disagreements."""
        system = (
            "你是受限Swarm的Supervisor仲裁Agent。根据独立专家意见与确定性执行证据，"
            "找最早的根因步骤，而不是重复记录下游传播错误。可执行反例优先于无证据的语言判断。"
            "不得让单个低置信度专家推翻明确测试证据。只输出合法JSON。"
        )
        schema = {
            "process_correct": True,
            "first_error_step": None,
            "error_types": [],
            "downstream_error_steps": [],
            "supporting_agents": [],
            "rationale": "...",
            "confidence": 0.0,
        }
        compact_evidence = {
            "criteria": evidence.get("criteria"),
            "execution": {
                "all_passed": evidence.get("execution", {}).get("all_passed"),
                "failed_tests": [
                    item["name"]
                    for item in evidence.get("execution", {}).get("tests", [])
                    if not item.get("passed")
                ],
            },
            "hypothesis": evidence.get("hypothesis"),
        }
        user = (
            f"题目：{problem['statement']}\n"
            f"待评步骤：{json.dumps(answer.get('reasoning_steps', []), ensure_ascii=False)}\n"
            f"专业Agent意见：{json.dumps(specialist_reviews, ensure_ascii=False)}\n"
            f"检测到的冲突：{json.dumps(conflicts, ensure_ascii=False)}\n"
            f"确定性证据：{json.dumps(compact_evidence, ensure_ascii=False)}\n"
            f"输出结构：{json.dumps(schema, ensure_ascii=False)}"
        )
        metadata = self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            reasoning_effort="high",
            max_tokens=min(self.config.max_tokens, 4096),
        )
        decision = extract_json_object(metadata["content"])
        if not isinstance(decision.get("process_correct"), bool):
            raise Hy3APIError("Supervisor arbitration missing boolean process_correct")
        return decision, metadata
