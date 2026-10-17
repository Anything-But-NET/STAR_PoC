import re

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

from spliter import SegmentNode, parse_markdown_to_tree

device = "mps" if torch.backends.mps.is_available() else "cpu"
tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-m3")
model = AutoModel.from_pretrained("BAAI/bge-m3").to(device)


def micro_split_chunks(text: str, max_chunk_size: int = 100, chunk_overlap: int = 10):
    res: list[tuple[str, int, int]] = []

    sentences = [
        s.strip()
        for s in re.split(r"([^\n]*\n(?:```[\s\S]*?```|~~~[\s\S]*?~~~))|(?<=[.!?])\s+|\n+", text)
        if s and s.strip()
    ]
    if not sentences:
        return res

    lens = [len(x) for x in tokenizer(sentences, add_special_tokens=False)["input_ids"]]

    current_len = 0
    current_sentences: list[tuple[str, int]] = []
    start_token_idx = 1
    for i in range(len(lens)):
        is_subtopic = sentences[i].startswith("**") and current_len >= 25
        if not is_subtopic and current_len + lens[i] <= max_chunk_size:
            current_sentences.append((sentences[i], lens[i]))
            current_len += lens[i]
        else:
            if current_sentences:
                chunk_text = "\n".join([s for s, _ in current_sentences])
                res.append((chunk_text, start_token_idx, start_token_idx + current_len))
            while (
                is_subtopic or current_len > chunk_overlap or current_len + lens[i] > max_chunk_size
            ) and current_sentences:
                _, s_len = current_sentences.pop(0)
                current_len -= s_len
                start_token_idx += s_len
                if is_subtopic and current_len == 0:
                    break
            current_len += lens[i]
            current_sentences.append((sentences[i], lens[i]))

    if current_sentences:
        chunk_text = "\n".join([s for s, _ in current_sentences])
        res.append((chunk_text, start_token_idx, start_token_idx + current_len))

    return res


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def get_embedding(
    data: str,
    macro_chunk_size: int = 7500,
    macro_chunk_overlap: int = 500,
) -> list[tuple[str, np.ndarray]]:
    if not data.strip():
        return []

    macro_chunks = micro_split_chunks(
        data,
        max_chunk_size=macro_chunk_size,
        chunk_overlap=macro_chunk_overlap,
    )

    res: list[tuple[str, np.ndarray]] = []
    prev_macro_end = 1

    for macro_idx, (macro_text, macro_start, macro_end) in enumerate(macro_chunks):
        if not macro_text.strip():
            continue

        inputs = tokenizer(macro_text, return_tensors="pt")
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            embeddings = model(**inputs).last_hidden_state  # (1, seq_len, 1024)

        seq_len = embeddings.shape[1]

        # Беремо мікро-чанки тільки після перетину з попереднім макро-блоком
        overlap_offset = (prev_macro_end - macro_start) if macro_idx > 0 else 0
        min_micro_start = 1 + max(0, overlap_offset)

        micro_chunks = [
            (m_text, m_start, m_end)
            for m_text, m_start, m_end in micro_split_chunks(macro_text)
            if m_text.strip() and m_start >= min_micro_start
        ]
        if not micro_chunks:
            prev_macro_end = macro_end
            continue

        batch_inputs = tokenizer(
            [m_text for m_text, _, _ in micro_chunks],
            padding=True,
            truncation=True,
            return_tensors="pt",
        ).to(device)
        with torch.no_grad():
            batch_cls = model(**batch_inputs).last_hidden_state[:, 0].cpu().numpy()

        for (micro_text, micro_start, micro_end), chunk_cls in zip(micro_chunks, batch_cls):
            s_idx = min(max(micro_start, 1), seq_len - 1)
            e_idx = min(max(micro_end, s_idx + 1), seq_len)

            late_span_vec = _l2_normalize(embeddings[0, s_idx:e_idx].mean(dim=0).cpu().numpy())
            vec = _l2_normalize(_l2_normalize(chunk_cls) + late_span_vec)
            res.append((micro_text, vec))

        prev_macro_end = macro_end

    return res

def merge_vectors(vectors: list[np.ndarray], temperature: float = 0.1) -> np.ndarray:
    X = np.array(vectors)
    
    X_norms = np.linalg.norm(X, axis=1, keepdims=True)
    X_normalized = X / np.where(X_norms == 0, 1, X_norms)
    
    attention_matrix = X_normalized @ X_normalized.T 
    
    scores = np.mean(attention_matrix, axis=1)
    
    scores = scores.flatten()
    exp_s = np.exp((scores - np.max(scores)) / temperature)
    w = exp_s / np.sum(exp_s)
    
    parent_vector = w @ X_normalized 
    
    norm = np.linalg.norm(parent_vector)
    if norm > 0:
        parent_vector = parent_vector / norm
        
    return parent_vector

class VectorizedSegmentNode:
    def __init__(
        self, 
        name: str, 
        level: int, 
        embedding: np.ndarray | None, 
        content: list[tuple[str, np.ndarray]],
        childrens: list["VectorizedSegmentNode"] | None = None
    ):
        self.name = name
        self.level = level
        self.embedding = embedding
        self.content = content
        self.childrens = childrens if childrens is not None else []

def vectorize_node(node: SegmentNode) -> VectorizedSegmentNode:
    raw_content = node.content.strip()
    if raw_content and node.level > 0:
        raw_content = f"{'#' * node.level} {node.name}\n{raw_content}"
    content = get_embedding(raw_content)
    
    content_vectors = [embedding for _, embedding in content]
    all_vectors = []
    
    if node.level > 0 and node.name.strip():
        all_vectors.append(get_embedding(node.name)[0][1])

    if content_vectors:
        all_vectors.append(merge_vectors(content_vectors))

    children = [vectorize_node(child) for child in node.childrens]
    all_vectors.extend([child.embedding for child in children if child.embedding is not None])

    result = merge_vectors(all_vectors) if all_vectors else None

    return VectorizedSegmentNode(node.name, node.level, result, content, childrens=children)

def print_tree(node: VectorizedSegmentNode, indent: int = 0):
    prefix = "  " * indent
    emb_info = f"shape={node.embedding.shape}" if node.embedding is not None else "None"
    print(f"{prefix}- [{node.level}] {node.name} (Emb: {emb_info}, Children: {len(node.childrens)})")
    
    for text, vec in node.content:
        short_text = (text[:40] + "...") if len(text) > 40 else text
        print(f"{prefix}    * chunk: '{short_text}' (emb: {vec.shape})")

    for child in node.childrens:
        print_tree(child, indent + 1)

if __name__ == "__main__":
    with open("README.md", "r") as file:
        md_doc = file.read()
        tree = parse_markdown_to_tree(md_doc)
        print_tree(vectorize_node(tree))