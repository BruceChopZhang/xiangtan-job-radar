"""后续阶段接口；不在第一阶段运行，也不收集个人身份信息。"""
from typing import Protocol

class AttachmentParser(Protocol):
    def parse(self, file_bytes: bytes, file_type: str) -> list[dict]:
        """返回岗位及原始页码/单元格证据；尚未实现。"""
        ...

class EligibilityMatcher(Protocol):
    def assess(self, position: dict, private_criteria: dict) -> dict:
        """仅初筛；必须返回待核实事项和依据，不得判定确定符合资格。"""
        ...

def parse_attachment(*args, **kwargs):
    raise NotImplementedError('附件内容解析留待第二阶段实现')

def match_position(*args, **kwargs):
    raise NotImplementedError('资格初筛留待后续阶段实现')
