from pathlib import Path
import sys

from fact_check.checker import IngestConflictChecker
from fact_check.store import InMemoryStarStore, load_document_text

DUMP_DIR = Path(__file__).resolve().parent.parent.parent / "sharepoint_dump"


def print_report(report, verbose: bool = False) -> None:
    print("\n" + "═" * 100)
    print(f"📄 ІНЖЕСТ ДОКУМЕНТА: {report.incoming_doc}")
    print(
        f"   Листових чанків у новому файлі : {report.total_incoming_leaves}\n"
        f"   Відсіяно без NLI (low sim/dup) : {report.skipped_low_similarity}\n"
        f"   Перевірено пар через NLI       : {report.checked_pairs}\n"
        f"   Створено тікетів у дашборд     : {len(report.tickets)}\n"
        f"   Загальний час (інжест + NLI)   : {report.total_latency_ms:.0f} ms"
    )
    print("═" * 100)

    if verbose and report.all_checked_pairs:
        print("\n🔎 Усі перевірені пари кандидатів:")
        for i, p in enumerate(report.all_checked_pairs, 1):
            flag = "🎫 TICKET" if p.is_ticket else "   ok    "
            print(
                f"  [{i:02d}] {flag} | Con={p.p_contradicted:.2f} Sup={p.p_supported:.2f} "
                f"| STAR={p.star_score:.3f} [{p.star_merge_reason}]"
            )
            print(f"       NEW: {p.incoming_text.replace(chr(10), ' ')[:140]}")
            print(f"       OLD: ({p.existing_doc}) {p.existing_text.replace(chr(10), ' ')[:140]}")

    if not report.tickets:
        print("\n✅ Конфліктів із існуючими документами в БД не знайдено.")
        return

    for idx, t in enumerate(report.tickets, 1):
        print(
            f"\n🎫 TICKET #{idx} | Contradiction: {t.p_contradicted:.2f} "
            f"(Sup: {t.p_supported:.2f}, Neu: {t.p_unsupported:.2f}) "
            f"| STAR: cosine={t.star_score:.3f} [{t.star_merge_reason}]"
        )
        print(f"   🆕 Новий файл ({t.incoming_doc} | chunk {t.incoming_chunk_id[:8]}):")
        print(f"      {t.incoming_text.replace(chr(10), ' ')[:220]}")
        print(f"   📚 Існуючий у БД ({t.existing_doc} | chunk {t.existing_chunk_id[:8]}):")
        print(f"      {t.existing_text.replace(chr(10), ' ')[:220]}")


def main() -> None:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    store = InMemoryStarStore()

    # 1. Базова база знань, яка ВЖЕ лежить у БД
    baseline_docs = [
        DUMP_DIR / "Public" / "Policy.md",
        DUMP_DIR / "Public" / "remote_work_hardware_allowance_old.md",
        DUMP_DIR / "Public" / "q3_2026_company_holiday_policy.docx",
        DUMP_DIR / "Public" / "vacation_remote_policy_2025.docx",
        DUMP_DIR / "Restricted" / "Engineering-Only" / "backend_deployment_pipeline_architecture.md",
    ]

    print("📥 Завантажуємо базові документи в STAR БД...")
    for doc_path in baseline_docs:
        store.index_document(doc_path.name, load_document_text(doc_path))
    print(f"✅ У базі STAR: {len(store.nodes)} векторних нод ({len(store.chunks)} чанків).")

    checker = IngestConflictChecker(store)

    # 2. Симуляція фонового інжесту НОВИХ / ОНОВЛЕНИХ документів із реальними розбіжностями:
    incoming_docs = [
        DUMP_DIR / "Public" / "q3_2026_company_holiday_policy_v2_updated.docx",
        DUMP_DIR / "Public" / "vacation_remote_policy_q4_2026.docx",
    ]

    for doc_path in incoming_docs:
        report = checker.check_document_on_ingest(
            file_name=doc_path.name,
            md_text=load_document_text(doc_path),
            min_star_score=0.75,
            contradict_threshold=0.75,
        )
        print_report(report, verbose=verbose)


if __name__ == "__main__":
    main()
