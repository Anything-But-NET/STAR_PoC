from dataclasses import dataclass, field
import json
import re
from typing import Self

HEADER_PATTERN = re.compile(r"^[ ]{0,3}(#{1,6})\s+(.+?)(?:\s+#+)?$")
FENCE_PATTERN = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")


@dataclass
class SegmentNode:
    name: str
    level: int
    childrens: list[Self] = field(default_factory=list)
    content: str = ""

    def add_content(self, data: str) -> None:
        if not self.content:
            self.content = data
        else:
            self.content += "\n" + data


def _clean_tree(node: SegmentNode) -> None:
    node.content = node.content.strip()
    for child in node.childrens:
        _clean_tree(child)


def parse_markdown_to_tree(md_text: str) -> SegmentNode:
    root = SegmentNode("Root", 0)
    stack = [root]

    in_code_block = False
    fence_char: str | None = None
    fence_len = 0

    preamble_node: SegmentNode | None = None
    first_header_seen = False

    def append_line(line_text: str) -> None:
        nonlocal preamble_node
        if not first_header_seen:
            if preamble_node is None:
                preamble_node = SegmentNode("Preamble", 1)
            preamble_node.add_content(line_text)
        else:
            stack[-1].add_content(line_text)

    for line in md_text.splitlines():
        if not in_code_block:
            fence_match = FENCE_PATTERN.match(line)
            if fence_match:
                in_code_block = True
                fence_char = fence_match.group(1)[0]
                fence_len = len(fence_match.group(1))
                append_line(line)
                continue
        else:
            fence_match = FENCE_PATTERN.match(line)
            if (
                fence_match
                and fence_match.group(1)[0] == fence_char
                and len(fence_match.group(1)) >= fence_len
            ):
                in_code_block = False
                fence_char = None
                fence_len = 0
            append_line(line)
            continue

        header_match = HEADER_PATTERN.match(line)
        if header_match:
            if not first_header_seen:
                first_header_seen = True
                if preamble_node and preamble_node.content.strip():
                    root.childrens.append(preamble_node)

            level = len(header_match.group(1))
            title = header_match.group(2).strip()
            new_node = SegmentNode(title, level)

            while stack and stack[-1].level >= level:
                stack.pop()

            stack[-1].childrens.append(new_node)
            stack.append(new_node)
        else:
            append_line(line)

    if not first_header_seen and preamble_node and preamble_node.content.strip():
        root.childrens.append(preamble_node)

    _clean_tree(root)
    return root


if __name__ == "__main__":
    with open("README.md", "r") as file:
        md_doc = file.read()
        tree = parse_markdown_to_tree(md_doc)
        print(json.dumps(tree, indent=2, ensure_ascii=False))