from dataclasses import dataclass
import time
import uuid

import torch
import torch.nn.functional as F
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from fact_check.store import InMemoryStarStore, MemVectorNode


@dataclass
class ConflictTicket:
    """Тікет для адмін-дашборду, коли при інжесті нового документа знайдено розбіжність із базою."""

    incoming_doc: str
    incoming_chunk_id: str
    incoming_text: str
    existing_doc: str
    existing_chunk_id: str
    existing_text: str
    star_score: float
    star_merge_reason: str
    p_contradicted: float
    p_supported: float
    p_unsupported: float


@dataclass
class CandidatePairCheck:
    incoming_doc: str
    incoming_chunk_id: str
    incoming_text: str
    existing_doc: str
    existing_chunk_id: str
    existing_text: str
    star_score: float
    star_merge_reason: str
    p_contradicted: float
    p_supported: float
    p_unsupported: float
    is_ticket: bool


@dataclass
class IngestCheckReport:
    incoming_doc: str
    total_incoming_leaves: int
    checked_pairs: int
    skipped_low_similarity: int
    tickets: list[ConflictTicket]
    all_checked_pairs: list[CandidatePairCheck]
    total_latency_ms: float


class IngestConflictChecker:
    """
    Фоновий чекер конфліктів під час інжесту нового документа:
      1. Індексує новий документ у STAR (листові чанки вже мають Late Chunking вектори).
      2. Для кожного листового чанка нового документа шукає найближчі ноди в ІНШИХ документах
         через STAR (`search_points` + `auto_merge_points`).
      3. Відсіює нерелевантні пари за порогом косинусної схожості (`min_star_score`).
      4. Збирає ієрархічний контекст для обох сторін (новий лист + контекст його секції vs
         знайдений STAR-вузол в існуючому документі) і перевіряє пару через 3-класовий NLI
         в обох напрямках (або прямому), щоб не плодити фейкові тікети.
      5. Якщо виявлено пряме протиріччя (`p_contradicted >= contradict_threshold`) — створює `ConflictTicket`.
    """

    def __init__(
        self,
        store: InMemoryStarStore,
        nli_model_name: str = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7",
    ) -> None:
        self.store = store
        self.nli_model_name = nli_model_name
        self.device = (
            "mps"
            if torch.backends.mps.is_available()
            else ("cuda" if torch.cuda.is_available() else "cpu")
        )
        print(f"⏳ Завантажуємо NLI модель {nli_model_name} на [{self.device}]...")
        self.tokenizer = AutoTokenizer.from_pretrained(nli_model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(nli_model_name).to(
            self.device
        )
        self.model.eval()

        id2label = {int(k): v.lower() for k, v in self.model.config.id2label.items()}
        self.idx_entail = next(i for i, l in id2label.items() if "entail" in l)
        self.idx_contra = next(i for i, l in id2label.items() if "contra" in l)
        self.idx_neutral = next(i for i, l in id2label.items() if "neutral" in l)

    def _score_nli_batch(
        self, pairs: list[tuple[str, str]]
    ) -> list[tuple[float, float, float]]:
        """Проганяє батч пар (premise, hypothesis) і повертає [(p_entail, p_contra, p_neutral), ...]."""
        if not pairs:
            return []

        premises = [p for p, _ in pairs]
        hypotheses = [h for _, h in pairs]

        inputs = self.tokenizer(
            premises,
            hypotheses,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        ).to(self.device)

        with torch.no_grad():
            logits = self.model(**inputs).logits
            probs_batch = F.softmax(logits, dim=-1).cpu().tolist()

        return [
            (probs[self.idx_entail], probs[self.idx_contra], probs[self.idx_neutral])
            for probs in probs_batch
        ]

    def _build_incoming_leaf_text(self, leaf_node: MemVectorNode) -> str:
        """
        Якщо листовий чанк нового документа короткий і містить займенники ("Він / It"),
        додає зверху рядок заголовка його безпосередньої секції (якщо його ще немає в тексті).
        """
        leaf_text = leaf_node.chunks[0].text
        if leaf_node.parent_id is not None:
            parent = self.store.nodes.get(leaf_node.parent_id)
            if parent and parent.chunks:
                first_line = parent.chunks[0].text.splitlines()[0].strip()
                if first_line.startswith("#") and first_line not in leaf_text:
                    return f"{first_line}\n{leaf_text}"
        return leaf_text

    def check_document_on_ingest(
        self,
        file_name: str,
        md_text: str,
        min_star_score: float = 0.78,
        contradict_threshold: float = 0.80,
        top_k_leaves: int = 6,
    ) -> IngestCheckReport:
        """
        Симулює фоновий інжест документа `file_name`:
        індексує його у STAR і шукає протиріччя з усіма іншими документами в базі.
        """
        t0 = time.perf_counter()

        incoming_leaves = self.store.index_document(file_name=file_name, md_text=md_text)

        candidate_records: list[dict] = []
        nli_pairs: list[tuple[str, str]] = []
        skipped = 0
        seen_chunk_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()

        for leaf in incoming_leaves:
            # 1. Використовуємо ВЖЕ порахований Late Chunking вектор листа (без повторного ембедінгу!)
            q_vec = leaf.embedding

            # 2. Шукаємо перетини тільки в ІНШИХ документах у БД
            matches = self.store.search_points(
                q_vec,
                exclude_file_name=file_name,
                limit=top_k_leaves,
                only_leaves=True,
            )
            if not matches:
                skipped += 1
                continue

            merged = self.store.auto_merge_points(matches, query_vector=q_vec)
            top_existing_node, star_score, merge_reason = merged[0]

            if star_score < min_star_score:
                skipped += 1
                continue

            # 3. Збираємо ієрархічний контекст знайденої ноди в існуючому документі
            existing_context, existing_leaf_chunk = self.store.build_hierarchical_context(
                top_existing_node, query_vec=q_vec
            )
            incoming_leaf_chunk = leaf.chunks[0]

            # Пропускаємо абсолютно ідентичні тексти (наприклад, незмінений копіпаст між версіями документа)
            if incoming_leaf_chunk.text.strip() == existing_leaf_chunk.text.strip():
                skipped += 1
                continue

            pair_key = (incoming_leaf_chunk.id, existing_leaf_chunk.id)
            if pair_key in seen_chunk_pairs:
                continue
            seen_chunk_pairs.add(pair_key)

            incoming_hypothesis = self._build_incoming_leaf_text(leaf)
            nli_pairs.append((existing_context, incoming_hypothesis))
            candidate_records.append(
                {
                    "incoming_chunk": incoming_leaf_chunk,
                    "existing_chunk": existing_leaf_chunk,
                    "existing_context": existing_context,
                    "star_score": star_score,
                    "merge_reason": merge_reason,
                }
            )

        # 4. Батчева перевірка кандидатів у NLI в ОБОХ напрямках (A->B та B->A):
        # протиріччя симетричне, але NLI-модель часто краще бачить конфлікт, коли
        # старий чіткий ліміт ("не більше 14 днів", "не передбачена") іде як Hypothesis або Premise.
        reverse_pairs = [(h, p) for p, h in nli_pairs]
        fwd_scores: list[tuple[float, float, float]] = []
        rev_scores: list[tuple[float, float, float]] = []
        batch_size = 16
        for i in range(0, len(nli_pairs), batch_size):
            fwd_scores.extend(self._score_nli_batch(nli_pairs[i : i + batch_size]))
            rev_scores.extend(self._score_nli_batch(reverse_pairs[i : i + batch_size]))

        # 5. Створюємо тікети в дашборд на впевнені протиріччя
        tickets: list[ConflictTicket] = []
        checked_details: list[CandidatePairCheck] = []
        for rec, (fe, fc, fn), (re_, rc, rn) in zip(candidate_records, fwd_scores, rev_scores):
            # Беремо сильніший сигнал протиріччя з двох напрямків, якщо при цьому немає високого entailment
            if rc > fc:
                p_ent, p_con, p_neu = re_, rc, rn
            else:
                p_ent, p_con, p_neu = fe, fc, fn

            is_ticket = (
                p_con >= contradict_threshold
                and p_con > max(fe, re_)
            )

            checked_details.append(
                CandidatePairCheck(
                    incoming_doc=file_name,
                    incoming_chunk_id=str(rec["incoming_chunk"].id),
                    incoming_text=rec["incoming_chunk"].text,
                    existing_doc=rec["existing_chunk"].file_name,
                    existing_chunk_id=str(rec["existing_chunk"].id),
                    existing_text=rec["existing_chunk"].text,
                    star_score=rec["star_score"],
                    star_merge_reason=rec["merge_reason"],
                    p_contradicted=p_con,
                    p_supported=max(fe, re_),
                    p_unsupported=p_neu,
                    is_ticket=is_ticket,
                )
            )

            if is_ticket:
                tickets.append(
                    ConflictTicket(
                        incoming_doc=file_name,
                        incoming_chunk_id=str(rec["incoming_chunk"].id),
                        incoming_text=rec["incoming_chunk"].text,
                        existing_doc=rec["existing_chunk"].file_name,
                        existing_chunk_id=str(rec["existing_chunk"].id),
                        existing_text=rec["existing_chunk"].text,
                        star_score=rec["star_score"],
                        star_merge_reason=rec["merge_reason"],
                        p_contradicted=p_con,
                        p_supported=max(fe, re_),
                        p_unsupported=p_neu,
                    )
                )

        tickets.sort(key=lambda t: t.p_contradicted, reverse=True)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        return IngestCheckReport(
            incoming_doc=file_name,
            total_incoming_leaves=len(incoming_leaves),
            checked_pairs=len(candidate_records),
            skipped_low_similarity=skipped,
            tickets=tickets,
            all_checked_pairs=checked_details,
            total_latency_ms=elapsed_ms,
        )
