"""ProseMirror(TipTap) JSON doc → 섹션별 평문/마크다운 변환."""
from __future__ import annotations

from typing import Any


def _inline_text(node: dict) -> str:
    t = node.get("type")
    if t == "text":
        txt = node.get("text", "")
        marks = {m.get("type") for m in node.get("marks", [])}
        if "bold" in marks:
            txt = f"**{txt}**"
        return txt
    if t == "hardBreak":
        return "\n"
    return "".join(_inline_text(c) for c in node.get("content", []))


def _block_text(node: dict, depth: int = 0) -> str:
    t = node.get("type")
    kids = node.get("content", [])
    if t == "paragraph":
        return "".join(_inline_text(c) for c in kids).strip()
    if t == "heading":
        level = int(node.get("attrs", {}).get("level", 2))
        return "#" * level + " " + "".join(_inline_text(c) for c in kids).strip()
    if t in ("bulletList", "orderedList"):
        lines = []
        for i, li in enumerate(kids, 1):
            marker = f"{i}." if t == "orderedList" else "-"
            inner = [_block_text(c, depth + 1) for c in li.get("content", [])]
            inner = [s for s in inner if s]
            if inner:
                first, rest = inner[0], inner[1:]
                lines.append("  " * depth + f"{marker} {first}")
                lines.extend("  " * (depth + 1) + s for s in rest)
        return "\n".join(lines)
    if t == "blockquote":
        return "\n".join("> " + _block_text(c, depth) for c in kids)
    if t == "codeBlock":
        return "```\n" + "".join(_inline_text(c) for c in kids) + "\n```"
    if t == "table":
        rows = []
        for row in kids:
            cells = ["".join(_block_text(c) for c in cell.get("content", [])) for cell in row.get("content", [])]
            rows.append("| " + " | ".join(cells) + " |")
        return "\n".join(rows)
    if t in ("image", "horizontalRule"):
        return ""
    # 알 수 없는 블록 → 자식들 평탄화
    return "\n".join(s for s in (_block_text(c, depth) for c in kids) if s)


def doc_to_sections(doc: dict | list | None) -> list[dict[str, Any]]:
    """doc → [{heading, level, text}], 첫 heading 전 내용은 heading=''."""
    if not doc:
        return []
    blocks = doc.get("content", []) if isinstance(doc, dict) else doc
    sections: list[dict[str, Any]] = []
    cur = {"heading": "", "level": 0, "text": []}
    for b in blocks:
        if b.get("type") == "heading":
            if cur["text"] or cur["heading"]:
                sections.append(cur)
            cur = {
                "heading": "".join(_inline_text(c) for c in b.get("content", [])).strip(),
                "level": int(b.get("attrs", {}).get("level", 2)),
                "text": [],
            }
        else:
            s = _block_text(b)
            if s:
                cur["text"].append(s)
    if cur["text"] or cur["heading"]:
        sections.append(cur)
    for s in sections:
        s["text"] = "\n".join(s["text"]).strip()
    return [s for s in sections if s["text"] or s["heading"]]


def doc_to_text(doc: dict | list | None) -> str:
    return "\n\n".join(
        (("#" * s["level"] + " " + s["heading"] + "\n") if s["heading"] else "") + s["text"]
        for s in doc_to_sections(doc)
    ).strip()
