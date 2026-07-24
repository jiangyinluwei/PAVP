"""PAVP 设置加载 - 从 ~/.pavp/settings.json 读取密钥与运行配置

文件位置:
  Windows: C:\\Users\\<user>\\.pavp\\settings.json
  Linux/Mac: ~/.pavp/settings.json

Schema:
{
  "litellm_master_key": "sk-pavp-local",   # 编排器调用代理时用的 key
  "proxy_port": 4001,                       # 代理监听端口
  "plan_api":       "sk-xxx",              # Plan/Verify 模型 API 密钥
  "plan_base_url":  "https://.../v1",      # Plan/Verify 模型 API 地址
  "plan_model":     "deepseek/deepseek-reasoner",  # Plan/Verify 模型标识
  "act_api":        "sk-xxx",              # Act 执行模型 API 密钥
  "act_base_url":   "https://.../v1",      # Act 执行模型 API 地址
  "act_model":      "openai/qwen2.5-coder-32b-instruct",  # Act 模型标识
  "cc_bin": "claude",                       # Claude Code 可执行文件
  "act_max_budget": 3.0,                    # 单次 Act 预算上限(美元)
  "act_max_turns": 40,                      # 单次 Act 轮数上限
  "act_timeout": 600,                       # 单次 Act 超时秒数
  "loop_mode": "auto"                       # Loop 循环模式: auto(自动) / manual(手动)
}

模型标识格式: provider/model，如 deepseek/deepseek-reasoner、openai/gpt-4o-mini。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_PORT = 5401

DEFAULTS: dict[str, Any] = {
    "litellm_master_key": "sk-pavp-local",
    "proxy_port": DEFAULT_PORT,
    "plan_api": "",
    "plan_base_url": "",
    "plan_model": "",
    "act_api": "",
    "act_base_url": "",
    "act_model": "",
    "cc_bin": "claude",
    "act_max_budget": 3.0,
    "act_max_turns": 40,
    "act_timeout": 600,
    "loop_mode": "auto",
    "auto_start": True,
    "auto_start_ui": False,
}

TEMPLATE: dict[str, Any] = dict(DEFAULTS)


def settings_path() -> Path:
    """返回 settings.json 路径（用户主目录下 .pavp/settings.json）"""
    return Path.home() / ".pavp" / "settings.json"


class SettingsError(RuntimeError):
    """设置文件相关错误"""


def load() -> dict[str, Any]:
    """读取并合并默认值后的设置。文件不存在则抛 SettingsError。"""
    p = settings_path()
    if not p.exists():
        raise SettingsError(
            f"未找到设置文件 {p}。请手动创建该文件并填入密钥。"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SettingsError(f"设置文件 {p} 不是合法 JSON: {e}") from e

    return {**DEFAULTS, **data}


def proxy_url(s: dict[str, Any] | None = None) -> str:
    """PAVP 代理的 chat/completions 端点 URL"""
    s = s or load()
    port = s.get("proxy_port", DEFAULT_PORT)
    return f"http://localhost:{port}/v1/chat/completions"


def proxy_key(s: dict[str, Any] | None = None) -> str:
    """编排器调用代理时用的 master key"""
    s = s or load()
    return s.get("litellm_master_key", "sk-pavp-local")


def save_field(key: str, value: Any) -> None:
    """更新 settings.json 中的单个字段（保留其他字段不变）。"""
    p = settings_path()
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = dict(TEMPLATE)
    data[key] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )



