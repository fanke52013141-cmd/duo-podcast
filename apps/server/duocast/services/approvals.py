"""确认记录（approvals）的撤销语义。

F2：确认是审计记录，不能物理删除。重新合成/换画面时应把旧确认标为「已撤销」，
保留「谁在什么时候因为什么被作废」，派生"当前有效确认"统一走 active()。
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..domain.project import Approval


def active(approvals: list[Approval]) -> list[Approval]:
    """当前有效（未被撤销）的确认。"""
    return [a for a in approvals if not a.revoked_at]


def revoke(approvals: list[Approval], kinds: tuple[str, ...], reason: str) -> list[Approval]:
    """把指定类型的有效确认标记为已撤销，其余原样返回。"""
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    out: list[Approval] = []
    for approval in approvals:
        if approval.kind in kinds and not approval.revoked_at:
            out.append(approval.model_copy(update={"revoked_at": stamp, "revoked_reason": reason}))
        else:
            out.append(approval)
    return out
