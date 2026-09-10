"""domain 统一配置（01 §11：pydantic 字段 snake_case，对外 camelCase 别名双向兼容）。"""

from __future__ import annotations

from pydantic import ConfigDict
from pydantic.alias_generators import to_camel


def dc_config(**extra) -> ConfigDict:
    return ConfigDict(alias_generator=to_camel, populate_by_name=True, **extra)
