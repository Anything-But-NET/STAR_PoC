import re

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

device = "mps" if torch.backends.mps.is_available() else "cpu"
tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-m3")
model = AutoModel.from_pretrained("BAAI/bge-m3").to(device)

# inputs = tokenizer("Тут твій довгий текст на кілька речень.", return_offsets_mapping=True, return_tensors="pt")
# offset_mapping = inputs.pop("offset_mapping")
# inputs = {k: v.to(device) for k, v in inputs.items()}
# outputs = model(**inputs)
# print(inputs)


def micro_split_chunks(text: str, max_chunk_size: int = 300, chunk_overlap: int = 50):
    res: list[tuple[str, int, int]] = []
    
    sentences = re.split(r"(?<=[.!?])\s+", text)
    lens = [len(x) for x in tokenizer(sentences, add_special_tokens=False)["input_ids"]]
    
    current_len = 0;
    current_sentences: list[tuple[str, int]] = []
    start_token_idx = 1
    for i in range(len(lens)):
        if current_len + lens[i] <= max_chunk_size:
            current_sentences.append((sentences[i], lens[i]))
            current_len += lens[i]
        else:
            chunk_text = " ".join([s for s, _ in current_sentences])
            res.append((chunk_text, start_token_idx, start_token_idx + current_len))
            while (
                current_len > chunk_overlap or current_len + lens[i] > max_chunk_size
            ) and current_sentences:
                _, s_len = current_sentences.pop(0)
                current_len -= s_len
                start_token_idx += s_len
            current_len += lens[i]
            current_sentences.append((sentences[i], lens[i]))
            
    chunk_text = " ".join([s for s, _ in current_sentences])
    res.append((chunk_text, start_token_idx, start_token_idx + current_len))
    
    return res

def macro_split_chunks(text: str, chunk_size: int = 7000, chunk_overlap: int = 500):
    blocks = re.split(r"\n+(?=#{2,6}\s)", text)
    return [item[0] for block in blocks for item in micro_split_chunks(block, chunk_size, chunk_overlap)]

from collections import defaultdict

COMBINE_TRESHOLD = 0.5

with open("README.md", "r", encoding="utf-8") as file:
    data = file.read()

final_database: list[tuple[str, np.ndarray, int]] = []
parent_list: dict[int, str] = {}
sim_scores: list[float] = []  # для збереження схожості з попереднім чанком

big_blocks = macro_split_chunks(data)
parent_id = 0

for block_idx, block in enumerate(big_blocks):
    inputs = tokenizer(block, return_offsets_mapping=True, return_tensors="pt")
    inputs.pop("offset_mapping")
    inputs = {k: v.to(device) for k, v in inputs.items()}
    
    with torch.no_grad():
        embeddings = model(**inputs).last_hidden_state

    macro_mean = embeddings[0].mean(dim=0).cpu().numpy()

    small_chunks = micro_split_chunks(block, max_chunk_size=300, chunk_overlap=50)

    prev_vector = None
    parent_text = ""

    for text, start, end in small_chunks:
        vector = embeddings[0, start:end].mean(dim=0).cpu().numpy()
        # ВАЖЛИВО: нормалізуємо вектор, щоб np.dot повертав саме Cosine Similarity [-1.0 .. 1.0]
        res_vector = vector
        norm = np.linalg.norm(res_vector)
        res_vector = res_vector / norm if norm > 0 else res_vector

        similarity = 0.0
        if prev_vector is not None:
            similarity = float(np.dot(res_vector, prev_vector))
            if similarity <= COMBINE_TRESHOLD:
                if parent_text.strip():
                    parent_list[parent_id] = parent_text.strip()
                    parent_id += 1
                    parent_text = ""

        sim_scores.append(similarity)
        parent_text += " " + text
        prev_vector = res_vector

        # У БД зберігаємо оригінальний контекстуалізований вектор (для RAG пошуку)
        raw_norm = np.linalg.norm(vector)
        db_vector = vector / raw_norm if raw_norm > 0 else vector
        final_database.append((text, db_vector, parent_id))

    if len(parent_text.strip()) != 0:
        parent_list[parent_id] = parent_text.strip()
        parent_id += 1

# ==========================================
# 📊 ЛЮДСЬКИЙ ВИВІД
# ==========================================

print("\n" + "═" * 80)
print("📊 ПІДСУМКОВА СТАТИСТИКА РОЗБИТТЯ")
print("═" * 80)
print(f" 📑 Символів у файлі   : {len(data)}")
print(f" 📦 Макро-блоків        : {len(big_blocks)}")
print(f" 🧩 Мікро-чанків        : {len(final_database)}")
print(f" 👨‍👧 Батьківських груп  : {len(parent_list)}")
print(f" 🎚 Поріг об'єднання    : {COMBINE_TRESHOLD}")
print("═" * 80)

# Групуємо дітей по parent_id
parent_to_children = defaultdict(list)
for idx, ((text, _, p_id), sim) in enumerate(zip(final_database, sim_scores)):
    parent_to_children[p_id].append((idx, text, sim))

print("\n🔍 ДЕТАЛЬНИЙ ОГЛЯД ЧАНКІВ (Parent ➔ Micro-chunks):\n")

for p_id, full_parent_text in parent_list.items():
    children = parent_to_children[p_id]
    
    # Скорочений прев'ю тексту батька
    p_preview = full_parent_text.replace("\n", " ").strip()
    if len(p_preview) > 100:
        p_preview = p_preview[:97] + "..."

    print(f"╔═ 📁 PARENT ID: #{p_id}")
    print(f"║  📄 Довжина: {len(full_parent_text)} симв. | Дітей: {len(children)}")
    print(f"║  📝 Текст: \"{p_preview}\"")
    print(f"║  ─" * 25)
    print(f"║  Дочірні мікро-чанки:")
    
    for local_i, (global_i, ch_text, sim) in enumerate(children, 1):
        clean_ch = ch_text.replace("\n", " ").strip()
        if len(clean_ch) > 75:
            clean_ch = clean_ch[:72] + "..."
        
        sim_badge = f"[Sim: {sim:.2f}]" if local_i > 1 else "[Start ]"
        is_last = (local_i == len(children))
        branch = "╚═" if is_last else "╠═"
        
        print(f"║   {branch} 🔹 Chunk #{global_i:<3} {sim_badge} ({len(ch_text)} симв.): \"{clean_ch}\"")
    print("╚" + "═" * 78 + "\n")