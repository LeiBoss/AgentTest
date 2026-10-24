"""由 LLM 生成产业链配置、校验并落盘。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from .config_loader import load_chain_config, validate_chain_config
from .llm import LLMError, call_llm
from .models import ChainConfig
from .prompts import P5_SYSTEM

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 20
MAX_ATTEMPTS = 2
_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff\- ]+$")
_UNSAFE_RE = re.compile(r"[^\w\u4e00-\u9fff\-]")


class ChainBuildError(RuntimeError):
    """生成或校验产业链配置失败。"""


class ChainExistsError(ChainBuildError):
    """同名产业链已存在。"""


def normalize_name(name: str) -> str:
    """校验用户输入的名称。"""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ChainBuildError("产业链名称不能为空")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ChainBuildError(f"产业链名称最多 {MAX_NAME_LENGTH} 个字符")
    if not _NAME_RE.match(cleaned):
        raise ChainBuildError("产业链名称只能包含中文、字母、数字、空格和连字符")
    return cleaned


def existing_chains(config_dir: Path) -> dict[str, Path]:
    """按 chain_name 收集已存在的配置。"""
    found: dict[str, Path] = {}
    for path in sorted(config_dir.glob("*.yaml")):
        try:
            found[load_chain_config(str(path)).chain_name] = path
        except Exception as exc:  # noqa: BLE001 - 坏配置不阻塞生成
            logger.warning("[chain_builder] skip %s: %s", path.name, exc)
    return found


def _file_path(name: str, config_dir: Path) -> Path:
    base = _UNSAFE_RE.sub("", name).strip() or "chain"
    path = config_dir / f"{base}.yaml"
    index = 2
    while path.exists():
        path = config_dir / f"{base}-{index}.yaml"
        index += 1
    return path


def _generate_once(name: str, previous_errors: list[str] | None) -> dict:
    payload: dict = {"chain_name": name}
    if previous_errors:
        payload["previous_errors"] = previous_errors
        payload["note"] = "上次输出未通过校验，请修正这些问题后重新输出完整 JSON"
    try:
        return call_llm(P5_SYSTEM, payload)
    except LLMError as exc:
        raise ChainBuildError(f"LLM 调用失败：{exc}") from exc


def _validate(raw: dict, name: str) -> ChainConfig:
    if not isinstance(raw, dict):
        raise ChainBuildError("LLM 返回的不是 JSON 对象")
    raw = {**raw, "chain_name": name}
    try:
        config = ChainConfig.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - pydantic 报错文本直接回传用户
        raise ChainBuildError(f"配置结构校验失败：{exc}") from exc
    issues = validate_chain_config(config)
    if issues:
        raise ChainBuildError("配置语义校验失败：" + "；".join(issues))
    return config


def generate_chain(name: str) -> ChainConfig:
    """调用 LLM 生成配置，首次不合法时带着错误再试一次。"""
    cleaned = normalize_name(name)
    errors: list[str] | None = None
    for _ in range(MAX_ATTEMPTS):
        raw = _generate_once(cleaned, errors)
        try:
            return _validate(raw, cleaned)
        except ChainBuildError as exc:
            errors = [str(exc)]
            logger.warning("[chain_builder] 第 %s 次生成校验失败: %s", len(errors), exc)
    raise ChainBuildError(errors[0] if errors else "生成失败")


def save_chain(config: ChainConfig, config_dir: Path) -> Path:
    """写入 YAML 并回读校验，返回文件路径。"""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = _file_path(config.chain_name, config_dir)
    data = config.model_dump(mode="json")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=None),
        encoding="utf-8",
    )
    tmp.replace(path)
    load_chain_config(str(path))
    return path


def create_chain(name: str, config_dir: Path) -> tuple[ChainConfig, Path]:
    """生成并落盘一条新产业链，同名直接拒绝。"""
    cleaned = normalize_name(name)
    if cleaned in existing_chains(config_dir):
        raise ChainExistsError(f"产业链已存在：{cleaned}")
    config = generate_chain(cleaned)
    return config, save_chain(config, config_dir)
