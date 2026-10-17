# STAR (Self-Attention Tree Auto-Merging Retrieval)

**STAR** is a structure-aware, hierarchical indexing and retrieval framework for Retrieval-Augmented Generation (RAG). It combines **Late Chunking**, **Self-Attention Vector Centrality Pooling (Graph Centrality without LLMs)**, and a **Dual-Trigger Bottom-Up Cascade** at query time.

---

## 💡 Core Philosophy: High Precision at Search, High Context at Generation

Traditional RAG suffers from an inevitable trade-off:
- **Small chunks (50–150 tokens):** High retrieval precision (no irrelevant noise in vector search), but lost context (the LLM cannot understand fragmented sentences).
- **Large chunks (500–1500 tokens):** Rich context, but diluted semantic density (needle-in-a-haystack problem, poor cosine similarity scores).

**STAR solves this dilemma:**
1. **Indexes leaf units (`micro` chunks, ~100 tokens)** for sharp vector matching.
2. **Contextualizes leaves via Late Chunking** within section-level `macro` blocks (~7500 tokens).
3. **Aggregates parent section representations bottom-up using Temperature-Scaled Self-Attention** (zero LLM calls, deterministic, mathematically sound).
4. **Dynamically cascades upwards at query time** using a **Dual-Trigger Merge Rule** (Quorum + Semantic Dominance with an Anti-Dilution Guard).

---

## 🏗️ System Architecture

```
[Markdown Document]
        │
        ▼ (spliter.py: Hierarchical AST Parser)
[Document Tree: H1 -> H2 -> H3]
        │
        ▼ (transformer.py: Macro-Windowing + Late Chunking)
[Contextualized Micro-Chunks (Leaf Nodes)]
        │
        ▼ (transformer.py: Self-Attention Temperature Pooling)
[Parent Vectors: Title Embedding + Merged Children Vectors]
        │
        ├─────────────────────────────┬─────────────────────────────┐
        ▼                             ▼                             ▼
 [SQLite: chunks.db]          [Qdrant Vector DB]           [Runtime Query Engine]
 (Raw Micro-Chunk Text)       (Leaf & Parent Vectors       (Leaf Search ->
                               + Hierarchical Metadata)      Dual-Trigger Merge ->
                                                             SQLite Stitching)
```

---

## ⚙️ Detailed Pipeline

### 1. Structure-Aware Document Tree Parsing (`spliter.py`)
Documents are parsed into an Abstract Syntax Tree (AST) of `SegmentNode` objects based on Markdown heading levels (`#` to `######`):
- Code blocks (` ```...``` `) are strictly shielded from regex splitting so shell commands and source snippets remain intact.
- Each node encapsulates:
  - `name`: Section heading (e.g. `## Running it`).
  - `level`: Heading depth ($1, 2, 3\dots$).
  - `content`: Prose, markdown tables, or lists belonging directly to this section.
  - `childrens`: Ordered list of deeper sub-sections.

---

### 2. Context-Aware Embedding Pipeline (`transformer.py`)

#### A. Macro-Windowing with Overlap Deduplication
If section content exceeds the model's context, it is divided into large `macro` blocks (e.g., $7500$ tokens with a $500$-token sliding overlap). To prevent boundary artifacts and duplicate chunks:

$$
s_{\mathrm{min}} = 1 + \max\left(0, e_{\mathrm{prev}} - s_{\mathrm{macro}}\right)
$$

Only micro-chunks whose start token index $s \ge s_{\mathrm{min}}$ (i.e. `min_micro_start = 1 + max(0, prev_macro_end - macro_start)`) are emitted from subsequent macro windows.

#### B. Late Chunking with CLS Hybridization
Inside each macro block:
1. The entire macro block is passed through the transformer model (e.g., `BAAI/bge-m3`) in **one single forward pass**, generating full token embeddings $H \in \mathbb{R}^{B \times L \times D}$ where tokens attend to surrounding section context.
2. Markdown-aware micro-splitting isolates sentences, code blocks, and bold sub-topics (`**...**`).
3. For each micro-chunk spanning token indices $[s, e]$, we compute its Late Chunking span slice:

$$
\vec{v}_{\mathrm{late}} = \mathrm{normalize}\left(\frac{1}{e - s}\sum_{i=s}^{e-1} H_{0, i}\right)
$$

4. For dense retrieval models optimized on `[CLS]` sentence representations, STAR blends the native sentence CLS embedding with the cross-contextualized span vector:

$$
\vec{v}_{\mathrm{micro}} = \mathrm{normalize}\left(\mathrm{normalize}(\vec{v}_{\mathrm{cls}}) + \vec{v}_{\mathrm{late}}\right)
$$

This provides the best of both worlds: sharp lexical/semantic focus without losing macro-block section context.

#### C. Self-Attention Tree Pooling (STAR Centrality)
Unlike **RAPTOR**, which invokes expensive LLMs to summarize text at every tree level, STAR computes parent vectors purely through vector-space self-attention.

Given $N$ vectors $X = [\vec{x}_1, \vec{x}_2, \dots, \vec{x}_N]^T \in \mathbb{R}^{N \times D}$:
1. Compute the normalized pairwise cosine similarity matrix:

$$
A = \hat{X}\hat{X}^T, \quad \text{where } \hat{X}_i = \frac{\vec{x}_i}{\|\vec{x}_i\|_2}
$$

2. Calculate the mean semantic centrality (average affinity) for each child across all siblings:

$$
s_i = \frac{1}{N}\sum_{j=1}^N A_{ij}
$$

*(Using the **mean** rather than the raw sum normalizes centrality scores to $[-1, 1]$ regardless of $N$. This ensures the temperature $\tau$ behaves consistently whether a section has $2$ chunks or $25$ chunks, avoiding softmax saturation on large sections).*

3. Apply temperature-scaled softmax ($\tau = 0.1$ by default) to emphasize semantic consensus and suppress isolated noise/boilerplate:

$$
w_i = \frac{\exp\left(\frac{s_i - \max(s)}{\tau}\right)}{\sum_{k=1}^N \exp\left(\frac{s_k - \max(s)}{\tau}\right)}
$$

4. Compute the parent vector over the normalized child representations:

$$
\vec{v}_{\mathrm{parent}} = \mathrm{normalize}\left(\sum_{i=1}^N w_i \hat{X}_i\right)
$$

In `vectorize_node`, a section's embedding is built by pooling:
- The section's own title embedding $\vec{v}_{\mathrm{title}}$ (anchoring the section's identity).
- The pooled vector of its immediate micro-chunks $\vec{v}_{\mathrm{content}}$.
- The embeddings of all its recursive child sub-sections.

---

### 3. Decoupled Storage Architecture (`db.py`)

- **Vector Store (Qdrant):**
  Stores all vectors (leaves and intermediate nodes) with structural metadata:
  - `point_id`: UUID.
  - `chunks`: List of SQLite chunk UUIDs aggregated under this node.
  - `parent_id`: UUID of parent section (or `None` for root).
  - `children_count`: Number of direct children under this point ($0$ for leaves).
  - `parent_children_count`: Total direct siblings/children under this node's parent.
- **Relational Store (SQLite):**
  Stores `(id, text)` pairs. Raw text is stored **strictly once at the leaf level**. Parent nodes simply maintain lists of leaf UUIDs, completely eliminating text redundancy in the database.

---

### 4. Dynamic Dual-Trigger Retrieval (`db.py` & `main_new.py`)

Retrieval operates bottom-up through a multi-pass cascade:

```
[User Query] -> embed_query()
      │
      ▼
1. Fetch Top-K Leaf Points (children_count == 0) from Qdrant
      │
      ▼
2. Group Candidate Points by parent_id
      │
      ▼
3. Evaluate Merge Conditions:
   ├─ Condition A: Quorum Trigger
   │    (len(matched_children) / total_children > 50%)
   │    AND (best_child_score - parent_score <= 0.06)  <-- Anti-Dilution Guard
   │
   └─ Condition B: Semantic Dominance Trigger
        (parent_score > best_child_score)               <-- Broad Query Trigger
      │
      ▼
4. If either condition holds:
   - Evict matched child points
   - Inject parent point with score = max(best_child_score, parent_score)
   - Loop back to step 2 (Iterative Cascade upwards)
      │
      ▼
5. Fetch & Stitch Text Chunks from SQLite for all surviving points
```

#### Why the Dual-Trigger Matters:
1. **Pinpoint Fact Query (e.g., *"What port does dotnet run listen on?"*):**
   - The leaf chunk mentioning `http://localhost:5066` scores $0.74$.
   - The parent section `## Running it` scores $0.64$ (diluted by Docker instructions and fixture docs).
   - The **Anti-Dilution Guard** detects $\Delta = 0.74 - 0.64 = 0.10 > 0.06$ and **blocks merging**. The user receives the exact 1-sentence answer without reading pages of Docker configs.
2. **Broad Section Query (e.g., *"What it covers"*):**
   - Individual markdown table rows only score moderately ($\approx 0.62$).
   - The parent vector (anchored by title and consensus pooling) scores $\approx 0.90$.
   - **Semantic Dominance** triggers immediate consolidation into the full parent section, returning the complete feature matrix.

---

