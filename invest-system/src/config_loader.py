"""YAML 链配置加载与语义校验。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import ChainConfig

_MIN_TRIGGERS = 8
_MAX_TRIGGERS = 10
_MIN_LAYERS = 5
_MIN_RULES = 2


def load_chain_config(path: str) -> ChainConfig:
    """读取 YAML 并做结构性校验，失败抛出 ValueError。"""
    data = _read_yaml(path)
    try:
        return ChainConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(_format_validation_error(path, exc)) from exc


def validate_chain_config(config: ChainConfig) -> list[str]:
    """返回语义问题列表，空列表表示通过。"""
    issues: list[str] = []
    issues.extend(_check_trigger_count(config))
    issues.extend(_check_layers(config))
    issues.extend(_check_rules(config))
    issues.extend(_check_paradox(config))
    issues.extend(_check_stages(config))
    return issues


def _read_yaml(path: str) -> dict[str, Any]:
    """读取 YAML 文件并确保顶层是映射。"""
    file = Path(path)
    if not file.is_file():
        raise ValueError(f"配置文件不存在: {path}")
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 解析失败: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"配置文件顶层必须是映射对象: {path}")
    return data


def _format_validation_error(path: str, exc: ValidationError) -> str:
    """把校验错误拼成带字段路径的文本。"""
    lines = [f"配置校验失败: {path}"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def _check_trigger_count(config: ChainConfig) -> list[str]:
    count = len(config.triggers)
    if not _MIN_TRIGGERS <= count <= _MAX_TRIGGERS:
        return [f"triggers 数量应为 {_MIN_TRIGGERS}-{_MAX_TRIGGERS} 个，当前 {count} 个"]
    return []


def _check_layers(config: ChainConfig) -> list[str]:
    issues: list[str] = []
    layers = {trigger.layer for trigger in config.triggers}
    if len(layers) < _MIN_LAYERS:
        issues.append(f"triggers 至少覆盖 {_MIN_LAYERS} 个 layer，当前 {len(layers)} 个")
    if not any(trigger.layer == "demand" for trigger in config.triggers):
        issues.append("triggers 必须包含至少 1 个 layer=demand")
    if not any(trigger.layer == "margin" for trigger in config.triggers):
        issues.append("triggers 必须包含至少 1 个 layer=margin")
    return issues


def _check_rules(config: ChainConfig) -> list[str]:
    issues: list[str] = []
    if len(config.rules) < _MIN_RULES:
        issues.append(f"至少需要 {_MIN_RULES} 条 rules，当前 {len(config.rules)} 条")
    for index, rule in enumerate(config.rules):
        if "AND" not in rule.cond:
            issues.append(f"rules[{index}].cond 必须包含 AND，当前: {rule.cond}")
    return issues


def _check_paradox(config: ChainConfig) -> list[str]:
    if not any(choke_point.paradox_flag for choke_point in config.choke_points):
        return ["至少需要 1 个 choke_point 的 paradox_flag=true"]
    return []


def _check_stages(config: ChainConfig) -> list[str]:
    if not config.stages:
        return ["stages 不能为空"]
    if not config.stages[-1].current:
        return ["stages 最后一条必须 current=true"]
    return []
