from __future__ import annotations
import os
import json
import re
import threading
from dotenv import load_dotenv
from openai import OpenAI
from .schemas import QAExample, JudgeResult, ReflectionEntry
from .utils import normalize_answer
from .prompts import ACTOR_SYSTEM, EVALUATOR_SYSTEM, REFLECTOR_SYSTEM

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3-8b-instruct:free")
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

FAILURE_MODE_BY_QID = {"hp2": "incomplete_multi_hop", "hp4": "wrong_final_answer", "hp6": "entity_drift", "hp8": "entity_drift"}

client = None
if OPENROUTER_API_KEY:
    client = OpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=OPENROUTER_API_KEY,
    )

import time
import random

# Use thread-local to track the last predicted answer across actor_answer -> evaluator -> reflector calls
_state = threading.local()

def reset_metrics():
    _state.tokens = 0
    _state.latency_ms = 0

def add_metrics(tokens: int, latency_ms: int):
    if not hasattr(_state, "tokens"):
        _state.tokens = 0
    if not hasattr(_state, "latency_ms"):
        _state.latency_ms = 0
    _state.tokens += tokens
    _state.latency_ms += latency_ms

def get_metrics() -> tuple[int, int]:
    tokens = getattr(_state, "tokens", 0)
    latency_ms = getattr(_state, "latency_ms", 0)
    return tokens, latency_ms

def safe_chat_completion(messages, temperature=0.0, max_retries=5):
    if not client:
        raise RuntimeError("OpenRouter API Key not set in environment or .env file.")
        
    base_delay = 2.0
    for attempt in range(max_retries):
        try:
            t0 = time.perf_counter()
            response = client.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=messages,
                temperature=temperature,
            )
            t1 = time.perf_counter()
            latency_ms = int((t1 - t0) * 1000)
            
            if response and getattr(response, "choices", None) is not None and len(response.choices) > 0:
                choice = response.choices[0]
                if getattr(choice, "message", None) is not None and getattr(choice.message, "content", None) is not None:
                    tokens = response.usage.total_tokens if (response.usage and response.usage.total_tokens) else 0
                    return choice.message.content.strip(), tokens, latency_ms
            
            print(f"[Warning] Empty or invalid LLM response: {response}. Retrying...")
        except Exception as e:
            print(f"[Warning] LLM API error on attempt {attempt+1}: {e}. Retrying...")
            
        # Exponential backoff with jitter
        delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
        time.sleep(delay)
        
    raise RuntimeError(f"Failed to get a valid response from LLM after {max_retries} attempts.")

def extract_json(text: str) -> dict:
    # Try to find markdown json block
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Parse directly
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # Try to find raw { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Failed to extract JSON from LLM response: {text}")

def actor_answer(example: QAExample, attempt_id: int, agent_type: str, reflection_memory: list[str]) -> str:
    # Formulate context text
    context_str = "\n\n".join(f"Title: {chunk.title}\n{chunk.text}" for chunk in example.context)
    
    # Formulate reflection history text
    reflection_str = ""
    if reflection_memory:
        reflection_str = "\n### Reflection History:\n" + "\n".join(
            f"- Attempt {i+1} reflection: {ref}" for i, ref in enumerate(reflection_memory)
        )

    user_content = f"Context:\n{context_str}\n\nQuestion: {example.question}{reflection_str}\n\nProvide the final concise answer:"
    
    messages = [
        {"role": "system", "content": ACTOR_SYSTEM},
        {"role": "user", "content": user_content}
    ]
    
    answer, tokens, latency_ms = safe_chat_completion(messages, temperature=0.0)
    add_metrics(tokens, latency_ms)
    
    # Store in thread-local state
    _state.last_answer = answer
    return answer

def evaluator(example: QAExample, answer: str) -> JudgeResult:
    context_str = "\n\n".join(f"Title: {chunk.title}\n{chunk.text}" for chunk in example.context)
    user_content = f"Context:\n{context_str}\n\nQuestion: {example.question}\nGold Answer: {example.gold_answer}\nPredicted Answer: {answer}"

    messages = [
        {"role": "system", "content": EVALUATOR_SYSTEM},
        {"role": "user", "content": user_content}
    ]
    
    content, tokens, latency_ms = safe_chat_completion(messages, temperature=0.0)
    add_metrics(tokens, latency_ms)
    
    try:
        data = extract_json(content)
        return JudgeResult.model_validate(data)
    except Exception as e:
        is_correct = normalize_answer(example.gold_answer) == normalize_answer(answer)
        return JudgeResult(
            score=1 if is_correct else 0,
            reason=f"Failed to parse evaluator response: {e}. Raw content: {content}",
            missing_evidence=[],
            spurious_claims=[]
        )

def reflector(example: QAExample, attempt_id: int, judge: JudgeResult) -> ReflectionEntry:
    wrong_answer = getattr(_state, "last_answer", "Unknown incorrect answer")

    user_content = (
        f"Question: {example.question}\n"
        f"Incorrect Predicted Answer: {wrong_answer}\n"
        f"Evaluator Critique:\n"
        f"- Score: {judge.score}\n"
        f"- Reason: {judge.reason}\n"
        f"- Missing Evidence: {judge.missing_evidence}\n"
        f"- Spurious Claims: {judge.spurious_claims}"
    )

    messages = [
        {"role": "system", "content": REFLECTOR_SYSTEM},
        {"role": "user", "content": user_content}
    ]
    
    content, tokens, latency_ms = safe_chat_completion(messages, temperature=0.0)
    add_metrics(tokens, latency_ms)

    try:
        data = extract_json(content)
        data["attempt_id"] = attempt_id
        return ReflectionEntry.model_validate(data)
    except Exception as e:
        return ReflectionEntry(
            attempt_id=attempt_id,
            failure_reason=judge.reason,
            lesson=f"Failed to parse reflector response: {e}. Raw content: {content}",
            next_strategy="Analyze the context carefully and verify the second-hop relationship again."
        )
