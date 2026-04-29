#!/usr/bin/env python3
"""一次性工具：为 ztierfs 包内缺少 ast docstring 的模块、类与函数插入简短中文 docstring。"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


def _doc(node: ast.AST) -> str | None:
    return ast.get_docstring(node, clean=False)


def _module_body_insert_line(lines: list[str]) -> int:
    i = 0
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    if i < len(lines) and ("coding" in lines[i] or lines[i].strip().startswith("# -*-")):
        i += 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    return i


def _describe_dunder(name: str) -> str | None:
    m = {
        "__init__": "初始化实例。",
        "__post_init__": "dataclass 构造后校验或派生状态。",
        "__repr__": "返回调试友好字符串表示。",
        "__str__": "返回面向用户的字符串表示。",
        "__enter__": "上下文管理器入口。",
        "__exit__": "上下文管理器出口；处理异常与清理。",
        "__call__": "使实例可调用。",
        "__len__": "返回容器长度。",
        "__bool__": "返回布尔真值。",
        "__iter__": "返回迭代器。",
        "__next__": "迭代下一项。",
        "__getitem__": "按索引或键读取元素。",
        "__setitem__": "按索引或键写入元素。",
        "__delitem__": "按索引或键删除元素。",
        "__contains__": "判断是否包含元素。",
        "__hash__": "返回哈希值（若可哈希）。",
        "__eq__": "相等比较。",
        "__lt__": "小于比较。",
        "__le__": "小于等于比较。",
        "__gt__": "大于比较。",
        "__ge__": "大于等于比较。",
    }
    return m.get(name)


def _camel_to_parts(name: str) -> list[str]:
    return re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", name)


def _describe_class(node: ast.ClassDef) -> str:
    base_names = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            base_names.append(b.id)
        elif isinstance(b, ast.Attribute):
            base_names.append(b.attr)
    base_hint = f"，继承 {', '.join(base_names)}" if base_names else ""
    parts = _camel_to_parts(node.name)
    label = "".join(parts) or node.name
    return f"{label}：领域对象或辅助类型{base_hint}。"


def _stem_verb(name: str) -> tuple[str | None, str]:
    verbs = (
        ("should_", "判断是否应"),
        ("ensure_", "确保"),
        ("prepare_", "准备"),
        ("commit_", "提交"),
        ("decode_", "解码"),
        ("encode_", "编码"),
        ("digest_", "计算摘要"),
        ("demote_", "将块降级到冷层"),
        ("promote_", "将块提升到热层"),
        ("schedule_", "异步调度"),
        ("record_", "记录"),
        ("flush_", "刷写"),
        ("defer_", "延迟登记"),
        ("resolve_", "解析"),
        ("normalize_", "规范化"),
        ("split_", "切分"),
        ("probe_", "探测"),
        ("unlink_", "删除链接或文件"),
        ("rename_", "重命名"),
        ("truncate_", "截断"),
        ("clone_", "克隆"),
        ("lookup_", "查找"),
        ("touch_", "更新访问/修改时间"),
        ("collect_", "汇总"),
        ("execute_", "执行"),
        ("plan_", "规划"),
        ("remove_", "移除"),
        ("delete_", "删除"),
        ("create_", "创建"),
        ("open_", "打开"),
        ("close_", "关闭"),
        ("read_", "读取"),
        ("write_", "写入"),
        ("get_", "返回"),
        ("set_", "设置"),
        ("has_", "判断是否具备"),
        ("is_", "判断是否为"),
        ("take_", "取走"),
        ("note_", "记录"),
        ("apply_", "应用"),
        ("release_", "释放"),
        ("acquire_", "获取"),
        ("run_", "运行"),
        ("check_", "检查"),
        ("validate_", "校验"),
        ("setup_", "配置"),
        ("emit_", "输出日志或事件"),
        ("visit_", "访问 AST 节点"),
    )
    for prefix, zh in verbs:
        if name.startswith(prefix):
            rest = name[len(prefix) :].replace("_", " ").strip()
            return zh, rest or "相关状态"
    return None, name.replace("_", " ").strip()


def _describe_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    du = _describe_dunder(node.name)
    if du:
        return du
    name = node.name
    if name == "main":
        return "CLI 入口。"
    internal = name.startswith("_")
    stem, tail = _stem_verb(name)
    if stem:
        role = f"{stem}{tail}。"
    else:
        role = f"处理 {tail or name}。"
    if internal:
        return f"内部：{role}"
    return role


def _is_protocol_ellipsis_stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    if len(node.body) != 1:
        return False
    stmt = node.body[0]
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return stmt.value.value is Ellipsis
    return False


def _walk(
    nodes: list[ast.stmt],
    prefix: str,
    out: list[tuple[str, ast.AST]],
) -> None:
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            out.append(("class", node))
            _walk(node.body, f"{prefix}{node.name}.", out)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(("func", node))
            _walk(node.body, f"{prefix}{node.name}.", out)


def _collect_targets(tree: ast.Module) -> list[tuple[str, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef]]:
    targets: list[tuple[str, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef]] = []
    if _doc(tree) is None:
        targets.append(("module", tree))
    flat: list[tuple[str, ast.AST]] = []
    _walk(tree.body, "", flat)
    for kind, node in flat:
        if kind == "class" and isinstance(node, ast.ClassDef):
            if _doc(node) is None:
                targets.append(("class", node))
        elif kind == "func" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _doc(node) is None:
                if _is_protocol_ellipsis_stub(node):
                    continue
                targets.append(("func", node))
    return targets


def _insert_lines_for_node(
    lines: list[str],
    node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    kind: str,
    text: str,
) -> tuple[int, list[str]]:
    if kind == "module":
        insert_idx = _module_body_insert_line(lines)
        indent = ""
    else:
        if not node.body:
            raise RuntimeError("empty body cannot insert docstring")
        insert_idx = node.body[0].lineno - 1
        indent_line = lines[insert_idx]
        indent = indent_line[: len(indent_line) - len(indent_line.lstrip())]
    doc_lines = [f'{indent}"""{text}"""']
    return insert_idx, doc_lines


def process_file(path: Path, *, root: Path) -> bool:
    raw = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(raw)
    except SyntaxError:
        return False
    if not isinstance(tree, ast.Module):
        return False
    targets = _collect_targets(tree)
    if not targets:
        return False

    lines = raw.splitlines(keepends=True)
    jobs: list[tuple[int, list[str]]] = []
    for kind_key, node in targets:
        if kind_key == "module":
            rel = path.relative_to(root)
            mod_name = rel.with_suffix("").as_posix().replace("/", ".")
            text = f"{mod_name}：包内实现模块。"
            jobs.append(_insert_lines_for_node(lines, node, "module", text))
            continue
        if kind_key == "class":
            assert isinstance(node, ast.ClassDef)
            text = _describe_class(node)
            jobs.append(_insert_lines_for_node(lines, node, "class", text))
            continue
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        text = _describe_function(node)
        jobs.append(_insert_lines_for_node(lines, node, "func", text))

    jobs.sort(key=lambda x: x[0], reverse=True)
    for insert_idx, doc_lines in jobs:
        # normalize doc_lines to use same line endings as file
        nl = "\n" if not lines else ("\r\n" if lines[0].endswith("\r\n") else "\n")
        fixed = [d if d.endswith("\n") else d + nl for d in doc_lines]
        lines[insert_idx:insert_idx] = fixed

    new_raw = "".join(lines)
    if new_raw != raw:
        path.write_text(new_raw, encoding="utf-8")
        return True
    return False


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else "ztierfs").resolve()
    changed = 0
    for path in sorted(root.rglob("*.py")):
        if process_file(path, root=root):
            changed += 1
            print("updated", path)
    print("files changed:", changed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
