import json
import re
from typing import Self


class SegmentNode:
    name: str
    content: str
    childrens: list[Self]
    level: int

    def __init__(self, name: str, level: int):
        self.name = name
        self.level = level
        self.childrens = []
        self.content = ""

    def add_content(self, data: str):
        if not self.content:
            self.content = data
        else:
            self.content += "\n" + data


def parse_markdown_to_tree(md_text: str) -> SegmentNode:
    header_pattern = re.compile(r"^[ ]{0,3}(#{1,6})\s+(.+?)(?:\s+#+)?$")
    fence_pattern = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")

    root = SegmentNode("Root", 0)
    stack = [root]

    in_code_block = False
    fence_char = None
    fence_len = 0

    preamble_node: SegmentNode | None = None
    first_header_seen = False

    for line in md_text.splitlines():
        if not in_code_block:
            m = fence_pattern.match(line)
            if m:
                in_code_block = True
                fence_char = m.group(1)[0]
                fence_len = len(m.group(1))
                if not first_header_seen:
                    if preamble_node is None:
                        preamble_node = SegmentNode("Preamble", 1)
                    preamble_node.add_content(line)
                else:
                    stack[-1].add_content(line)
                continue
        else:
            m = fence_pattern.match(line)
            if m and m.group(1)[0] == fence_char and len(m.group(1)) >= fence_len:
                in_code_block = False
                fence_char = None
                fence_len = 0
            if not first_header_seen:
                if preamble_node is None:
                    preamble_node = SegmentNode("Preamble", 1)
                preamble_node.add_content(line)
            else:
                stack[-1].add_content(line)
            continue

        match = header_pattern.match(line) if not in_code_block else None

        if match:
            if not first_header_seen:
                first_header_seen = True
                if preamble_node and preamble_node.content.strip():
                    root.childrens.append(preamble_node)

            level = len(match.group(1))
            title = match.group(2).strip()

            new_node = SegmentNode(title, level)

            while stack and stack[-1].level >= level:
                stack.pop()

            stack[-1].childrens.append(new_node)
            stack.append(new_node)
        else:
            if not first_header_seen:
                if preamble_node is None:
                    preamble_node = SegmentNode("Preamble", 1)
                preamble_node.add_content(line)
            else:
                stack[-1].add_content(line)

    if not first_header_seen and preamble_node and preamble_node.content.strip():
        root.childrens.append(preamble_node)

    def _clean_tree(n: SegmentNode):
        n.content = n.content.strip()
        for c in n.childrens:
            _clean_tree(c)

    _clean_tree(root)

    return root


if __name__ == "__main__":
    with open("README.md", "r") as file:
        md_doc = file.read()
        tree = parse_markdown_to_tree(md_doc)
        print(json.dumps(tree, indent=2, ensure_ascii=False))