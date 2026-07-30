import io
import unittest

from reportlab.pdfgen import canvas
from unittest.mock import patch

from app.core import config
from app.services.document_chunking_service import (
    create_document_evidence_chunks,
    split_markdown_table_into_chunks,
    split_text_into_word_chunks,
)
from app.services.document_citation_service import validate_and_link_citations
from app.services.document_extraction_service import extract_structured_pdf
from app.services.document_retrieval_service import (
    classify_answer_depth,
    diversify_ranked_nodes,
    reciprocal_rank_fusion,
)
from app.services.document_hierarchy_service import (
    StoredHierarchyNode,
    build_deep_document_hierarchy,
    create_stored_summary_node,
    partition_nodes_by_input_budget,
)
from app.workflows.document_analysis.nodes import choose_analysis_route


def create_single_line_pdf(character_count: int, page_count: int = 1) -> bytes:
    output = io.BytesIO()
    document_canvas = canvas.Canvas(output)

    for page_number in range(page_count):
        text = "a" * character_count if page_number == 0 else "b"
        document_canvas.drawString(36, 760, text)
        document_canvas.showPage()

    document_canvas.save()
    return output.getvalue()


def create_table_pdf() -> bytes:
    output = io.BytesIO()
    document_canvas = canvas.Canvas(output)
    document_canvas.drawString(36, 760, "Storage stability results")
    left = 50
    top = 700
    column_width = 140
    row_height = 24
    rows = [
        ["Condition", "Measured value"],
        ["Frozen <= -20 C", "4.5 mg/mL"],
        ["Refrigerated 2-8 C", "3.9 mg/mL*"],
    ]

    for row_number in range(len(rows) + 1):
        y_position = top - row_number * row_height
        document_canvas.line(
            left,
            y_position,
            left + 2 * column_width,
            y_position,
        )

    for column_number in range(3):
        x_position = left + column_number * column_width
        document_canvas.line(
            x_position,
            top,
            x_position,
            top - len(rows) * row_height,
        )

    for row_number, row in enumerate(rows):
        for column_number, cell in enumerate(row):
            x_position = left + column_number * column_width + 5
            y_position = top - row_number * row_height - 16
            document_canvas.drawString(x_position, y_position, cell)

    document_canvas.drawString(50, 610, "* Mean of three replicates")
    document_canvas.save()
    return output.getvalue()


class DocumentAnalysisTests(unittest.TestCase):
    def test_exactly_230_tokens_routes_to_basic_analysis(self):
        extracted_document = extract_structured_pdf(create_single_line_pdf(690))
        state = {"extracted_token_count": extracted_document.token_count}

        self.assertEqual(extracted_document.token_count, 230)
        self.assertEqual(choose_analysis_route(state), "basic")

    def test_exactly_231_tokens_routes_to_deep_analysis_on_one_page(self):
        extracted_document = extract_structured_pdf(create_single_line_pdf(691))
        state = {"extracted_token_count": extracted_document.token_count}

        self.assertEqual(len(extracted_document.pages), 1)
        self.assertEqual(extracted_document.token_count, 231)
        self.assertEqual(choose_analysis_route(state), "deep")

    def test_page_count_does_not_override_token_routing(self):
        extracted_document = extract_structured_pdf(
            create_single_line_pdf(3, page_count=200)
        )
        state = {"extracted_token_count": extracted_document.token_count}

        self.assertEqual(len(extracted_document.pages), 200)
        self.assertEqual(extracted_document.token_count, 200)
        self.assertEqual(choose_analysis_route(state), "basic")

    def test_supported_fixture_page_scales_are_extractable(self):
        for page_count in (1, 5, 20, 150):
            with self.subTest(page_count=page_count):
                extracted_document = extract_structured_pdf(
                    create_single_line_pdf(3, page_count=page_count)
                )
                self.assertEqual(len(extracted_document.pages), page_count)

    def test_every_nonblank_fixture_page_creates_evidence(self):
        pages = []

        for page_number in range(1, 21):
            pages.append(
                {
                    "page_number": page_number,
                    "narrative_text": f"Evidence on page {page_number}",
                    "headings": [],
                    "tables": [],
                }
            )

        evidence_chunks = create_document_evidence_chunks(pages)
        covered_pages = {chunk.page_start for chunk in evidence_chunks}
        self.assertEqual(covered_pages, set(range(1, 21)))

    def test_document_limits_are_independent_from_legacy_chat_limits(self):
        self.assertEqual(config.MAX_ATTACHED_FILE_LENGTH, 230)
        self.assertEqual(config.DEEP_PDF_ANALYSIS_TRIGGER_TOKENS, 230)
        self.assertEqual(config.MAX_DOCUMENT_MODEL_INPUT_TOKENS, 24_000)
        self.assertEqual(config.MAX_DOCUMENT_EVIDENCE_TOKENS, 18_000)
        self.assertEqual(config.MAX_DOCUMENT_ANSWER_TOKENS, 2_000)
        self.assertNotEqual(
            config.MAX_DOCUMENT_ANSWER_TOKENS,
            config.MAX_RESPONSE_TOKENS,
        )

    def test_table_values_headers_units_and_footnote_are_preserved(self):
        extracted_document = extract_structured_pdf(create_table_pdf())
        page = extracted_document.pages[0]

        self.assertEqual(len(page.tables), 1)
        markdown = page.tables[0].markdown
        self.assertIn("Condition", markdown)
        self.assertIn("Frozen <= -20 C", markdown)
        self.assertIn("4.5 mg/mL", markdown)
        self.assertIn("3.9 mg/mL*", markdown)
        self.assertIn("* Mean of three replicates", page.narrative_text)
        self.assertNotIn("4.5 mg/mL", page.narrative_text)

    def test_evidence_chunks_are_bounded_and_overlap(self):
        words = [f"biology{word_number}" for word_number in range(500)]
        chunks = split_text_into_word_chunks(
            " ".join(words),
            maximum_tokens=120,
            overlap_tokens=20,
        )

        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(chunk) > 0 for chunk in chunks))
        first_chunk_words = set(chunks[0].split())
        second_chunk_words = set(chunks[1].split())
        self.assertTrue(first_chunk_words & second_chunk_words)

    def test_large_table_chunks_repeat_headers_and_preserve_rows(self):
        header = "| Condition | Value |\n| --- | --- |"
        rows = [
            f"| Storage condition {row_number} | {row_number}.5 mg/mL |"
            for row_number in range(120)
        ]
        chunks = split_markdown_table_into_chunks(header + "\n" + "\n".join(rows))

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith(header) for chunk in chunks))
        self.assertIn("| Storage condition 119 | 119.5 mg/mL |", chunks[-1])

    def test_adaptive_depth_phrases_are_deterministic(self):
        self.assertEqual(classify_answer_depth("Briefly summarize the document"), "overview")
        self.assertEqual(classify_answer_depth("Go deeper into storage"), "evidence")
        self.assertEqual(classify_answer_depth("What exact value is in the table?"), "evidence")

    def test_citations_are_validated_against_owned_document_pages(self):
        document_id = "11111111-1111-1111-1111-111111111111"
        documents = [
            {
                "id": document_id,
                "filename": "study.pdf",
                "page_count": 5,
            }
        ]
        evidence_nodes = [
            {
                "document_id": document_id,
                "page_start": 3,
            }
        ]
        raw_answer = (
            f"Supported claim [DOC:{document_id}:PAGE:3]. "
            f"Invalid claim [DOC:{document_id}:PAGE:8]."
        )
        answer = validate_and_link_citations(
            raw_answer,
            documents,
            evidence_nodes,
            user_id=7,
        )

        self.assertIn("study.pdf, p. 3", answer)
        self.assertIn("user_id=7#page=3", answer)
        self.assertIn("[unavailable source]", answer)

    def test_cross_document_citations_resolve_independently(self):
        first_id = "11111111-1111-1111-1111-111111111111"
        second_id = "22222222-2222-2222-2222-222222222222"
        documents = [
            {"id": first_id, "filename": "first.pdf", "page_count": 8},
            {"id": second_id, "filename": "second.pdf", "page_count": 12},
        ]
        evidence_nodes = [
            {"document_id": first_id, "page_start": 2},
            {"document_id": second_id, "page_start": 11},
        ]
        raw_answer = (
            f"First result [DOC:{first_id}:PAGE:2]. "
            f"Second result [DOC:{second_id}:PAGE:11]."
        )
        answer = validate_and_link_citations(
            raw_answer,
            documents,
            evidence_nodes,
            user_id=9,
        )

        self.assertIn("first.pdf, p. 2", answer)
        self.assertIn("second.pdf, p. 11", answer)

    def test_rank_fusion_and_diversification_keep_distant_documents(self):
        first_document_node = {
            "id": 1,
            "document_id": "first",
            "page_start": 2,
        }
        distant_first_document_node = {
            "id": 2,
            "document_id": "first",
            "page_start": 90,
        }
        second_document_node = {
            "id": 3,
            "document_id": "second",
            "page_start": 5,
        }
        vector_results = [
            first_document_node,
            distant_first_document_node,
            second_document_node,
        ]
        lexical_results = [
            distant_first_document_node,
            second_document_node,
            first_document_node,
        ]
        fused_results = reciprocal_rank_fusion(
            [vector_results, lexical_results]
        )
        diversified_results = diversify_ranked_nodes(
            fused_results,
            ["first", "second"],
        )

        selected_ids = {node["id"] for node in diversified_results}
        self.assertEqual(selected_ids, {1, 2, 3})
        self.assertEqual(
            {node["document_id"] for node in diversified_results[:2]},
            {"first", "second"},
        )

    def test_150_page_hierarchy_builds_packet_section_and_major_levels(self):
        leaf = StoredHierarchyNode(
            id=1,
            node_type="evidence",
            title="Evidence",
            content="Scientific evidence",
            page_start=1,
            page_end=150,
            token_count=20,
            leaf_ids=[1],
        )
        root = StoredHierarchyNode(
            id=99,
            node_type="root",
            title="Root",
            content="Root summary",
            page_start=1,
            page_end=150,
            token_count=10,
            leaf_ids=[1],
        )

        with patch(
            "app.services.document_hierarchy_service.build_hierarchy_level",
            side_effect=lambda _document_id, source_nodes, *_args: source_nodes,
        ) as build_level, patch(
            "app.services.document_hierarchy_service.create_stored_summary_node",
            return_value=root,
        ):
            result = build_deep_document_hierarchy("document-id", [leaf], 150)

        self.assertEqual(result, root)
        self.assertEqual(build_level.call_count, 3)
        target_page_spans = [call.args[4] for call in build_level.call_args_list]
        self.assertEqual(target_page_spans, [4, 10, 30])

    def test_summary_node_retains_unique_leaf_provenance(self):
        first_source = StoredHierarchyNode(
            id=10,
            node_type="packet",
            title="First",
            content="First evidence",
            page_start=1,
            page_end=4,
            token_count=10,
            leaf_ids=[1, 2],
        )
        second_source = StoredHierarchyNode(
            id=11,
            node_type="packet",
            title="Second",
            content="Second evidence",
            page_start=5,
            page_end=8,
            token_count=10,
            leaf_ids=[2, 3],
        )

        with patch(
            "app.services.document_hierarchy_service.summarize_nodes_with_bounded_requests",
            return_value="Combined summary",
        ), patch(
            "app.services.document_hierarchy_service.create_document_embeddings",
            return_value=[[0.0] * 384],
        ), patch(
            "app.services.document_hierarchy_service.create_document_node_from_db",
            return_value={"id": 50},
        ), patch(
            "app.services.document_hierarchy_service.set_document_node_parent_from_db"
        ), patch(
            "app.services.document_hierarchy_service.create_document_node_sources_from_db"
        ) as create_sources:
            summary_node = create_stored_summary_node(
                "document-id",
                "section",
                2,
                "Section",
                [first_source, second_source],
                700,
            )

        self.assertEqual(summary_node.leaf_ids, [1, 2, 3])
        create_sources.assert_called_once_with(50, [1, 2, 3])

    def test_summary_partitions_respect_document_input_budget(self):
        nodes = []

        for node_id in range(100):
            nodes.append(
                StoredHierarchyNode(
                    id=node_id,
                    node_type="evidence",
                    title=f"Node {node_id}",
                    content="Evidence",
                    page_start=node_id + 1,
                    page_end=node_id + 1,
                    token_count=450,
                    leaf_ids=[node_id],
                )
            )

        input_budget = config.MAX_DOCUMENT_MODEL_INPUT_TOKENS - 700
        partitions = partition_nodes_by_input_budget(nodes, input_budget)

        for partition in partitions:
            estimated_partition_tokens = sum(
                node.token_count + 25 for node in partition
            )
            self.assertLessEqual(estimated_partition_tokens, input_budget)


if __name__ == "__main__":
    unittest.main()
