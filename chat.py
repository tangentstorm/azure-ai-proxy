"""
this is an experimental and currently unused plugin
to make the server forward requests for /v1/completions
to /v1/responses. It was basically an experiment to get
cline (which at the time does not yet support /v1/responses)
to work with newer models (which require /v1/responses).

so far i have not gotten this to work.
"""
import json
import time
from typing import Any, Dict, Optional


def is_responses_model(model: Optional[str]) -> bool:
    """
    Identify models that must use the Responses API (e.g., gpt-5.x, codex variants).
    """
    if not model:
        return False
    lm = model.lower()
    return lm.startswith("gpt-5") or "codex" in lm or lm.startswith("o3")


def chat_to_responses_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a chat/completions payload to a Responses payload."""
    converted: Dict[str, Any] = {}
    model = payload.get("model")
    if model:
        converted["model"] = model
    messages = payload.get("messages") or []

    def normalize_content(item: Any, role: str) -> list:
        """
        Accept strings or list of parts; coerce plain text into input_text for user/system,
        output_text for assistant.
        """
        def default_part(text_val: str) -> Dict[str, str]:
            if role == "assistant":
                return {"type": "output_text", "text": text_val}
            return {"type": "input_text", "text": text_val}

        if isinstance(item, str):
            return [default_part(item)]
        if isinstance(item, list):
            parts = []
            for part in item:
                if isinstance(part, dict):
                    ptype = part.get("type")
                    if ptype == "text":
                        parts.append(default_part(part.get("text", "")))
                    else:
                        parts.append(part)
                elif isinstance(part, str):
                    parts.append(default_part(part))
            return parts
        return [default_part(str(item))]

    converted_input = []
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else msg
        converted_input.append(
            {
                "role": role or "user",
                "content": normalize_content(content, role or "user"),
            }
        )
    converted["input"] = converted_input
    if "stream" in payload:
        converted["stream"] = payload["stream"]
    if "temperature" in payload:
        converted["temperature"] = payload["temperature"]
    if "top_p" in payload:
        converted["top_p"] = payload["top_p"]
    if "max_tokens" in payload:
        converted["max_output_tokens"] = payload["max_tokens"]
    if "stop" in payload:
        converted["stop_sequences"] = payload["stop"]
    if "response_format" in payload:
        converted["response_format"] = payload["response_format"]
    if "tools" in payload:
        tools = []
        for t in payload.get("tools", []) or []:
            if not isinstance(t, dict):
                continue
            # Expect either {type: function, function: {name, description, parameters}} or already in Responses shape
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                fn = t["function"]
                if "name" not in fn:
                    continue
                tools.append(
                    {
                        "type": "function",
                        "name": fn.get("name"),
                        "description": fn.get("description"),
                        "parameters": fn.get("parameters"),
                    }
                )
            else:
                # Pass through if it already has a name/type
                if t.get("name") and t.get("type"):
                    tools.append(t)
        if tools:
            converted["tools"] = tools
    if "metadata" in payload:
        converted["metadata"] = payload["metadata"]
    return converted


def responses_to_chat_payload(responses_json: Dict[str, Any]) -> Dict[str, Any]:
    """Map a Responses API response to a chat.completions-like shape for clients."""
    model = responses_json.get("model") or ""
    usage = responses_json.get("usage")
    text_parts: list[str] = []
    output_text = responses_json.get("output_text")
    if isinstance(output_text, str):
        text_parts.append(output_text)
    elif isinstance(output_text, list):
        for item in output_text:
            if isinstance(item, str):
                text_parts.append(item)

    output = responses_json.get("output") or {}
    # Handle output as dict/list/str
    outputs_iter = []
    if isinstance(output, dict):
        outputs_iter = [output]
    elif isinstance(output, list):
        outputs_iter = [o for o in output if isinstance(o, (dict, str))]
    elif isinstance(output, str):
        text_parts.append(output)
    for out in outputs_iter:
        if isinstance(out, str):
            text_parts.append(out)
            continue
        if out.get("type") == "output_text" and "text" in out:
            text_parts.append(str(out.get("text", "")))
            continue
        if out.get("type") == "function_call":
            args = out.get("arguments")
            if isinstance(args, str):
                try:
                    parsed_args = json.loads(args)
                    if isinstance(parsed_args, dict) and parsed_args.get("response"):
                        text_parts.append(str(parsed_args.get("response")))
                except json.JSONDecodeError:
                    pass
        content_list = out.get("content") or []
        if isinstance(content_list, list):
            for part in content_list:
                if isinstance(part, dict):
                    ptype = part.get("type")
                    if ptype in ("output_text", "text") and "text" in part:
                        text_parts.append(str(part.get("text", "")))
                elif isinstance(part, str):
                    text_parts.append(part)
    message_text = "".join(text_parts)
    content_parts = [{"type": "output_text", "text": message_text or ""}]
    return {
        "id": responses_json.get("id"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content_parts,
                    "content_text": message_text,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def normalize_responses_output(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Responses payload contains a message-style output with content parts.
    Some clients expect output[0].content[0].type to exist.
    """
    output = payload.get("output")
    if not isinstance(output, list):
        output = []
    has_text_content = False
    for out in output:
        if not isinstance(out, dict):
            continue
        content_list = out.get("content")
        if isinstance(content_list, list):
            for part in content_list:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    has_text_content = True
                    break
        if has_text_content:
            break
        if out.get("type") == "output_text" and isinstance(out.get("text"), str):
            has_text_content = True
            break

    if has_text_content:
        payload["output"] = output
        return payload

    text_val: Optional[str] = None
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        text_val = output_text

    if text_val is None:
        for out in output:
            if isinstance(out, dict) and out.get("type") == "function_call":
                args = out.get("arguments")
                if isinstance(args, str):
                    try:
                        parsed_args = json.loads(args)
                        if isinstance(parsed_args, dict) and parsed_args.get("response"):
                            text_val = str(parsed_args.get("response"))
                            break
                    except json.JSONDecodeError:
                        continue

    if text_val is None:
        text_val = ""

    message_output = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text_val}],
    }
    payload["output"] = [message_output] + output
    if not isinstance(payload.get("output_text"), str):
        payload["output_text"] = text_val
    return payload
