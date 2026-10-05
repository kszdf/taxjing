# -*- coding: utf-8 -*-
"""
慧根堂·财税AI智库 — 零依赖 LLM 适配器 (M4+ 大模型接入)

仅用 Python 标准库（urllib），调用 OpenAI 兼容的 /chat/completions 协议。
可接：OpenAI / DeepSeek / 通义千问(Qwen) / 智谱 / 任意兼容网关。

配置（环境变量）：
  LLM_API_KEY   必填。不填则 chat() 抛错，server 自动回退规则引擎。
  LLM_BASE_URL  选填，默认 https://api.openai.com/v1
  LLM_MODEL     选填，默认 gpt-4o-mini

设计铁律：本适配器只负责"把 prompt 发给模型、把文本取回来"，
不决定答什么、不加免责——免责与时效校验由 server 在生成层统一注入。
"""
import os
import json
import urllib.request
import urllib.error


def is_configured() -> bool:
    return bool(os.environ.get("LLM_API_KEY"))


def _cfg() -> dict:
    return {
        "base": (os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
        "key": os.environ.get("LLM_API_KEY", ""),
        "model": os.environ.get("LLM_MODEL") or "gpt-4o-mini",
    }


def chat(system: str, user: str, temperature: float = 0.2, timeout: int = 30) -> str:
    """发一次对话补全，返回模型文本。任何异常都转换为 RuntimeError 抛给上层。"""
    if not is_configured():
        raise RuntimeError(
            "LLM 未配置：请设置环境变量 LLM_API_KEY（及可选 LLM_BASE_URL / LLM_MODEL）")
    c = _cfg()
    url = c["base"] + "/chat/completions"
    payload = {
        "model": c["model"],
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + c["key"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:400]
        except Exception:
            pass
        raise RuntimeError(f"LLM HTTP {e.code}: {detail}")
    except Exception as e:  # 超时 / DNS / 网络
        raise RuntimeError(f"LLM 请求失败：{e}")
    try:
        obj = json.loads(body)
        return obj["choices"][0]["message"]["content"].strip()
    except Exception as e:
        raise RuntimeError(f"LLM 响应解析失败：{e}")
