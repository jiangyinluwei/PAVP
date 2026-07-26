"""PAVP 设置加载 - 从 ~/.pavp/settings.json 读取密钥与运行配置

文件位置:
  Windows: C:\\Users\\<user>\\.pavp\\settings.json
  Linux/Mac: ~/.pavp/settings.json

Schema:
{
  "litellm_master_key": "sk-pavp-local",          # 编排器调用代理时用的 key
  "proxy_port": 5401,                              # 代理监听端口
  "plan_0": {
    "model": "deepseek/deepseek-reasoner",         # Plan/Verify 模型标识
    "openai_api": "sk-xxx",                        # Plan/Verify OpenAI API 密钥
    "openai_base_url": "https://.../v1",           # Plan/Verify OpenAI API 地址
    "anthropic_api": "sk-xxx",                     # Plan/Verify Anthropic API 密钥
    "anthropic_base_url": "https://..."            # Plan/Verify Anthropic API 地址
  },
  "act_0": {
    "model": "openai/qwen2.5-coder-32b-instruct", # Act 模型标识
    "openai_api": "sk-xxx",                        # Act OpenAI API 密钥
    "openai_base_url": "https://.../v1",           # Act OpenAI API 地址
    "anthropic_api": "sk-xxx",                     # Act Anthropic API 密钥
    "anthropic_base_url": "https://..."            # Act Anthropic API 地址
  },
  "current_plan_id": 0,                            # 当前选中的 Plan 配置 ID
  "current_act_id": 0,                             # 当前选中的 Act 配置 ID
  "cc_bin": "claude",                              # Claude Code 可执行文件
  "act_max_budget": 3.0,                           # 单次 Act 预算上限(美元)
  "act_max_turns": 40,                             # 单次 Act 轮数上限
  "act_timeout": 600,                              # 单次 Act 超时秒数
  "loop_mode": "auto"                              # Loop 循环模式: auto(自动) / manual(手动)
}

模型标识格式: provider/model，如 deepseek/deepseek-reasoner、openai/gpt-4o-mini。

向后兼容:
  - V2: 旧的 plan_model / plan_openai_api / ... 扁平字段在加载时自动迁移为 plan_0/act_0 结构
  - V1: 更旧的 plan_api / plan_base_url / act_api / act_base_url 自动迁移为 openai_* 字段
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

DEFAULT_PORT = 5401

# 模型配置子字段名（plan_X/act_X 内部的键）
_MODEL_CONFIG_KEYS = ("model", "openai_api", "openai_base_url", "anthropic_api", "anthropic_base_url")

def _make_model_config(model: str = "") -> dict[str, str]:
    return {k: "" for k in _MODEL_CONFIG_KEYS} | {"model": model}

DEFAULTS: dict[str, Any] = {
    "litellm_master_key": "sk-pavp-local",
    "proxy_port": DEFAULT_PORT,
    "plan_0": _make_model_config(),
    "act_0": _make_model_config(),
    "current_plan_id": 0,
    "current_act_id": 0,
    "cc_bin": "claude",
    "act_max_budget": 3.0,
    "act_max_turns": 40,
    "act_timeout": 600,
    "loop_mode": "auto",
    "auto_start": True,
    "auto_start_ui": False,
}

TEMPLATE: dict[str, Any] = dict(DEFAULTS)

# Old → new field migration map for backward compatibility
_FIELD_MIGRATION: dict[str, str] = {
    "plan_api": "plan_openai_api",
    "plan_base_url": "plan_openai_base_url",
    "act_api": "act_openai_api",
    "act_base_url": "act_openai_base_url",
}


def settings_path() -> Path:
    """返回 settings.json 路径（用户主目录下 .pavp/settings.json）"""
    return Path.home() / ".pavp" / "settings.json"


class SettingsError(RuntimeError):
    """设置文件相关错误"""


def _is_old_flat_format(data: dict) -> bool:
    """检测是否为旧的扁平格式 (包含 plan_model 或 act_model 顶层字段)。"""
    return "plan_model" in data or "act_model" in data


def _migrate_flat_to_structured(data: dict) -> dict:
    """将旧的扁平格式迁移到新的 plan_X/act_X 结构化格式。

    旧格式:
        plan_model -> plan_0.model
        plan_openai_api -> plan_0.openai_api
        ...
        act_model -> act_0.model
        ...
    新格式:
        plan_0: { model, openai_api, openai_base_url, anthropic_api, anthropic_base_url }
        act_0: { ... }
    """
    _OLD_TO_NEW_KEY = {
        "model": "model",
        "openai_api": "openai_api",
        "openai_base_url": "openai_base_url",
        "anthropic_api": "anthropic_api",
        "anthropic_base_url": "anthropic_base_url",
    }

    for prefix in ("plan", "act"):
        new_key = f"{prefix}_0"
        if new_key in data:
            continue  # 已有新格式，跳过
        # 从旧字段构建配置，确保所有键都存在
        cfg = dict(_make_model_config())
        for old_suffix, new_suffix in _OLD_TO_NEW_KEY.items():
            old_field = f"{prefix}_{old_suffix}"
            if old_field in data:
                cfg[new_suffix] = data.pop(old_field)
        data[new_key] = cfg

    # 添加 current_plan_id / current_act_id（如果尚不存在）
    data.setdefault("current_plan_id", 0)
    data.setdefault("current_act_id", 0)
    return data


def load() -> dict[str, Any]:
    """读取并合并默认值后的设置。文件不存在则抛 SettingsError。

    自动迁移:
      - V2: 旧的 plan_model / plan_openai_api / ... 扁平字段 -> plan_0/act_0 结构
      - V1: 更旧的 plan_api / plan_base_url / act_api / act_base_url -> openai_* 字段
    """
    p = settings_path()
    if not p.exists():
        raise SettingsError(
            f"未找到设置文件 {p}。请手动创建该文件并填入密钥。"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise SettingsError(f"设置文件 {p} 不是合法 JSON: {e}") from e

    # Step 1: V1 迁移 (plan_api -> plan_openai_api)
    migrated = False
    for old_key, new_key in _FIELD_MIGRATION.items():
        if old_key in data and new_key not in data:
            data[new_key] = data.pop(old_key)
            migrated = True

    # Step 2: V2 迁移 (扁平字段 -> plan_X/act_X 结构)
    if _is_old_flat_format(data):
        data = _migrate_flat_to_structured(data)
        migrated = True

    if migrated:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8-sig",
        )

    return {**DEFAULTS, **data}


def get_plan_config(s: dict, plan_id: int | None = None) -> dict[str, str]:
    """从设置字典中获取指定 plan_id 的模型配置。"""
    if plan_id is None:
        plan_id = s.get("current_plan_id", 0)
    key = f"plan_{plan_id}"
    cfg = s.get(key, {})
    # 确保所有键都存在
    return {k: cfg.get(k, "") for k in _MODEL_CONFIG_KEYS}


def get_act_config(s: dict, act_id: int | None = None) -> dict[str, str]:
    """从设置字典中获取指定 act_id 的模型配置。"""
    if act_id is None:
        act_id = s.get("current_act_id", 0)
    key = f"act_{act_id}"
    cfg = s.get(key, {})
    return {k: cfg.get(k, "") for k in _MODEL_CONFIG_KEYS}


def get_current_plan_id(s: dict) -> int:
    """获取当前选中的 Plan 配置 ID。"""
    return int(s.get("current_plan_id", 0))


def get_current_act_id(s: dict) -> int:
    """获取当前选中的 Act 配置 ID。"""
    return int(s.get("current_act_id", 0))


def get_plan_config_ids(s: dict | None = None) -> list[int]:
    """获取所有可用的 Plan 配置 ID 列表。"""
    s = s or load()
    ids = set()
    for key in s:
        m = re.match(r"^plan_(\d+)$", str(key))
        if m:
            ids.add(int(m.group(1)))
    return sorted(ids) or [0]


def get_act_config_ids(s: dict | None = None) -> list[int]:
    """获取所有可用的 Act 配置 ID 列表。"""
    s = s or load()
    ids = set()
    for key in s:
        m = re.match(r"^act_(\d+)$", str(key))
        if m:
            ids.add(int(m.group(1)))
    return sorted(ids) or [0]


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
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    else:
        data = dict(TEMPLATE)
    data[key] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8-sig",
    )



