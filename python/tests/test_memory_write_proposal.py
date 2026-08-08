"""Public Memory Write Proposal contract (open-brain-ekn.2)."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROPOSAL_DOC = PROJECT_ROOT / "docs" / "standards" / "memory-write-proposal.md"
EVAL_SUITE = PROJECT_ROOT / "agents" / "memory-write-judge-eval.json"
JUDGE_MODULE = PROJECT_ROOT / "python" / "src" / "open_brain" / "memory_write_judge.py"
PUBLIC_MODULE = PROJECT_ROOT / "python" / "src" / "open_brain" / "memory_write_proposal.py"
AGENT_DOC = PROJECT_ROOT / "agents" / "memory-write-judge.md"


def _valid_proposal(**overrides: object) -> dict:
    base = {
        "intended_memory_content": "User prefers concise status updates.",
        "category": "preference",
        "source_citation": {
            "ref": "conversation://current/preference",
            "label": "observed",
        },
        "authorization_basis": {
            "ref": "conversation://current/preference",
            "label": "observed",
            "granted_by": "user",
        },
        "expected_use": "instruction",
        "retention_scope": "personal",
        "risk_flags": [],
    }
    base.update(overrides)
    return base


def _schema_valid(payload: object) -> bool:
    from open_brain.memory_write_proposal import memory_write_proposal_json_schema

    validator = Draft202012Validator(memory_write_proposal_json_schema())
    return not list(validator.iter_errors(payload))


def _parser_valid(payload: object) -> bool:
    from open_brain.memory_write_proposal import parse_memory_write_proposal

    proposal, errors = parse_memory_write_proposal(payload)
    return proposal is not None and errors == []


# ─── AC1: versioned public schema module ───────────────────────────────────────


class TestPublicProposalContract:
    def test_schema_version_and_seven_fields_are_public(self) -> None:
        from open_brain.memory_write_proposal import (
            MEMORY_WRITE_PROPOSAL_SCHEMA_VERSION,
            REQUIRED_PROPOSAL_FIELDS,
            memory_write_proposal_json_schema,
        )

        assert MEMORY_WRITE_PROPOSAL_SCHEMA_VERSION == "memory-write-proposal.v1"
        assert REQUIRED_PROPOSAL_FIELDS == {
            "intended_memory_content",
            "category",
            "source_citation",
            "authorization_basis",
            "expected_use",
            "retention_scope",
            "risk_flags",
        }
        schema = memory_write_proposal_json_schema()
        assert schema["$id"].endswith("memory-write-proposal.v1")
        assert set(schema["required"]) == REQUIRED_PROPOSAL_FIELDS
        assert set(schema["properties"]) == REQUIRED_PROPOSAL_FIELDS

    def test_parse_accepts_canonical_seven_field_wire_shape(self) -> None:
        from open_brain.memory_write_proposal import (
            parse_memory_write_proposal,
            raw_proposal_payload,
        )

        raw = _valid_proposal()
        proposal, errors = parse_memory_write_proposal(raw)

        assert errors == []
        assert proposal is not None
        assert raw_proposal_payload(proposal) == {
            "intended_memory_content": "User prefers concise status updates.",
            "category": "preference",
            "source_citation": {
                "ref": "conversation://current/preference",
                "label": "observed",
            },
            "authorization_basis": {
                "ref": "conversation://current/preference",
                "label": "observed",
                "granted_by": "user",
            },
            "expected_use": "instruction",
            "retention_scope": "personal",
            "risk_flags": [],
        }

    def test_raw_payload_validates_against_json_schema_for_auth_shapes(self) -> None:
        from open_brain.memory_write_proposal import (
            AuthorizationBasis,
            MemoryWriteProposal,
            ProvenanceRef,
            raw_proposal_payload,
        )

        source = ProvenanceRef(ref="conversation://current/preference", label="observed")
        cases = [
            MemoryWriteProposal(
                intended_memory_content="With granted_by",
                category="preference",
                source_citation=source,
                authorization_basis=AuthorizationBasis(
                    ref="conversation://current/preference",
                    label="observed",
                    granted_by="user",
                ),
                expected_use="instruction",
                retention_scope="personal",
                risk_flags=(),
            ),
            MemoryWriteProposal(
                intended_memory_content="Without granted_by",
                category="preference",
                source_citation=source,
                authorization_basis=AuthorizationBasis(
                    ref="conversation://current/preference",
                    label="observed",
                ),
                expected_use="evidence",
                retention_scope="personal",
                risk_flags=(),
            ),
            MemoryWriteProposal(
                intended_memory_content="Null authorization",
                category="observation",
                source_citation=source,
                authorization_basis=None,
                expected_use="evidence",
                retention_scope="session",
                risk_flags=(),
            ),
        ]

        for proposal in cases:
            payload = raw_proposal_payload(proposal)
            auth = payload["authorization_basis"]
            if isinstance(auth, dict):
                assert "granted_by" not in auth or auth["granted_by"]
            assert _schema_valid(payload), payload
            assert _parser_valid(payload), payload

    def test_validation_errors_are_machine_readable_with_stable_codes(self) -> None:
        from open_brain.memory_write_proposal import parse_memory_write_proposal

        raw = {
            "intended_memory_content": " ",
            "category": "habit",
            "expected_use": "advice",
            "retention_scope": "forever",
            "risk_flags": "secret",
            "source_citation": {"ref": "somewhere", "label": "trusted"},
        }
        proposal, errors = parse_memory_write_proposal(raw)

        assert proposal is None
        codes = {error.code for error in errors}
        assert "missing_required_field" in codes
        assert "invalid_intended_memory_content" in codes
        assert "invalid_category" in codes
        assert "invalid_expected_use" in codes
        assert "unclear_retention" in codes
        assert "vague_source" in codes
        assert "invalid_risk_flags" in codes
        assert all(error.field and error.message for error in errors)
        assert all(isinstance(error.to_dict(), dict) for error in errors)

    def test_one_missing_required_field_issue_per_field(self) -> None:
        from open_brain.memory_write_proposal import parse_memory_write_proposal

        proposal, errors = parse_memory_write_proposal({})
        assert proposal is None
        missing = [error for error in errors if error.code == "missing_required_field"]
        assert {error.field for error in missing} == {
            "intended_memory_content",
            "category",
            "source_citation",
            "authorization_basis",
            "expected_use",
            "retention_scope",
            "risk_flags",
        }
        assert len(missing) == 7

    def test_null_authorization_is_schema_valid_but_preflight_flags_it(self) -> None:
        from open_brain.memory_write_proposal import (
            parse_memory_write_proposal,
            proposal_preflight_issues,
        )

        raw = _valid_proposal(authorization_basis=None)
        proposal, errors = parse_memory_write_proposal(raw)
        assert proposal is not None
        assert errors == []
        assert _schema_valid(raw)

        issues = proposal_preflight_issues(raw)
        codes = {issue.code for issue in issues}
        assert "missing_authorization" in codes

    def test_source_locator_grammar_accepts_and_rejects_documented_forms(self) -> None:
        from open_brain.memory_write_proposal import (
            SOURCE_LOCATOR_PATTERN,
            is_source_locator,
            memory_write_proposal_json_schema,
            parse_memory_write_proposal,
        )

        accepted = [
            "conversation://current/preference",
            "agent://summary-draft",
            "file://.env",
            "agent-session:codex:session-123",
            "python/src/open_brain/session_summary.py",
            "docs/adr/0002-credentials-and-privacy.md",
            "docs/meeting notes/decision.md",
        ]
        rejected = [
            "somewhere",
            "unknown",
            "n/a",
            "the conversation",
            "user said so earlier today",
            " conversation://leading-space",
        ]
        for ref in accepted:
            assert is_source_locator(ref), ref
            proposal, errors = parse_memory_write_proposal(
                _valid_proposal(source_citation={"ref": ref, "label": "observed"})
            )
            assert proposal is not None, (ref, errors)
        for ref in rejected:
            assert not is_source_locator(ref), ref
            proposal, errors = parse_memory_write_proposal(
                _valid_proposal(source_citation={"ref": ref, "label": "observed"})
            )
            assert proposal is None, ref
            assert any(error.code == "vague_source" for error in errors), ref

        schema = memory_write_proposal_json_schema()
        assert schema["properties"]["source_citation"]["properties"]["ref"]["pattern"] == (
            SOURCE_LOCATOR_PATTERN
        )

    def test_illegal_epistemic_combination_is_preflight_not_authority_matrix(self) -> None:
        from open_brain.memory_write_proposal import (
            parse_memory_write_proposal,
            proposal_preflight_issues,
        )

        raw = _valid_proposal(
            source_citation={"ref": "agent://draft", "label": "generated"},
            expected_use="instruction",
        )
        proposal, errors = parse_memory_write_proposal(raw)
        assert proposal is not None
        assert errors == []

        issues = proposal_preflight_issues(raw)
        assert any(issue.code == "illegal_epistemic_combination" for issue in issues)
        assert not any(issue.code == "invalid_category_use" for issue in issues)

    def test_parse_never_returns_none_with_empty_errors(self) -> None:
        from open_brain.memory_write_proposal import parse_memory_write_proposal

        samples: list[object] = [
            None,
            [],
            "proposal",
            {},
            _valid_proposal(source_citation=None),
            _valid_proposal(category=None),
            _valid_proposal(risk_flags=["pii", "pii"]),
            _valid_proposal(extra=True),
        ]
        for sample in samples:
            proposal, errors = parse_memory_write_proposal(sample)
            if proposal is None:
                assert errors, sample

    def test_provenance_label_aliases_epistemic_label(self) -> None:
        from open_brain.epistemic_provenance import EpistemicLabel
        from open_brain.memory_write_judge import ProvenanceLabel as JudgeLabel
        from open_brain.memory_write_proposal import ProvenanceLabel

        assert ProvenanceLabel is EpistemicLabel
        assert JudgeLabel is EpistemicLabel

    def test_judge_module_has_no_export_restriction(self) -> None:
        source = JUDGE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assert not any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
            for node in tree.body
        )
        from open_brain.memory_write_judge import (
            EVIDENCE_ONLY_LABELS,
            FAILED_EVIDENCE_LABELS,
            INSTRUCTION_GRADE_LABELS,
            ReasonedGate,
        )

        assert "inferred" in EVIDENCE_ONLY_LABELS
        assert "disputed" in FAILED_EVIDENCE_LABELS
        assert "observed" in INSTRUCTION_GRADE_LABELS
        assert ReasonedGate is not None

    def test_build_helper_constructs_validated_proposal(self) -> None:
        from open_brain.memory_write_proposal import (
            build_memory_write_proposal,
            raw_proposal_payload,
        )

        proposal = build_memory_write_proposal(
            intended_memory_content="Deploy only after confirmed review.",
            category="policy",
            source_citation={
                "ref": "docs/adr/0002-credentials-and-privacy.md",
                "label": "confirmed",
            },
            authorization_basis={
                "ref": "docs/adr/0002-credentials-and-privacy.md",
                "label": "confirmed",
                "granted_by": "adr",
            },
            expected_use="instruction",
            retention_scope="project",
            risk_flags=["policy-sensitive"],
        )
        payload = raw_proposal_payload(proposal)
        assert payload["category"] == "policy"
        assert payload["risk_flags"] == ["policy-sensitive"]

    def test_build_helper_raises_validation_error_for_malformed_auth_mappings(
        self,
    ) -> None:
        from open_brain.memory_write_proposal import (
            MemoryWriteProposalValidationError,
            build_memory_write_proposal,
        )

        base = dict(
            intended_memory_content="Builder must validate auth mappings.",
            category="fact",
            source_citation={"ref": "conversation://current", "label": "observed"},
            expected_use="evidence",
            retention_scope="personal",
            risk_flags=[],
        )

        with pytest.raises(MemoryWriteProposalValidationError) as missing_ref:
            build_memory_write_proposal(
                **base,
                authorization_basis={"label": "observed", "granted_by": "user"},
            )
        assert any(
            issue.code == "invalid_authorization_basis"
            and issue.field == "authorization_basis.ref"
            for issue in missing_ref.value.issues
        )

        with pytest.raises(MemoryWriteProposalValidationError) as missing_label:
            build_memory_write_proposal(
                **base,
                authorization_basis={
                    "ref": "conversation://current",
                    "granted_by": "user",
                },
            )
        assert any(
            issue.code == "invalid_authorization_basis"
            and issue.field == "authorization_basis.label"
            for issue in missing_label.value.issues
        )

        with pytest.raises(MemoryWriteProposalValidationError) as unknown_key:
            build_memory_write_proposal(
                **base,
                authorization_basis={
                    "ref": "conversation://current",
                    "label": "observed",
                    "note": "drop-me-not",
                },
            )
        assert any(
            issue.code == "unexpected_field"
            and issue.field == "authorization_basis.note"
            for issue in unknown_key.value.issues
        )

    def test_judge_reexports_expected_use_and_epistemic_labels(self) -> None:
        from open_brain.epistemic_provenance import (
            EPISTEMIC_LABELS as SharedLabels,
            ExpectedUse as SharedExpectedUse,
        )
        from open_brain.memory_write_judge import EPISTEMIC_LABELS, ExpectedUse

        assert ExpectedUse is SharedExpectedUse
        assert EPISTEMIC_LABELS is SharedLabels


class TestSchemaRuntimeDifferential:
    def test_draft_2020_12_schema_matches_parser_on_reviewed_corpus(self) -> None:
        corpus: list[tuple[str, dict]] = [
            ("valid_with_granted_by", _valid_proposal()),
            (
                "valid_without_granted_by",
                _valid_proposal(
                    authorization_basis={
                        "ref": "conversation://current/preference",
                        "label": "observed",
                    }
                ),
            ),
            ("valid_null_authorization", _valid_proposal(authorization_basis=None)),
            (
                "valid_path_multiword",
                _valid_proposal(
                    source_citation={
                        "ref": "docs/meeting notes/decision.md",
                        "label": "observed",
                    }
                ),
            ),
            (
                "valid_namespaced",
                _valid_proposal(
                    source_citation={
                        "ref": "agent-session:codex:session-123",
                        "label": "observed",
                    }
                ),
            ),
            ("unknown_top_level", _valid_proposal(actor_note="prose")),
            (
                "unknown_nested_source_key",
                _valid_proposal(
                    source_citation={
                        "ref": "conversation://current",
                        "label": "observed",
                        "note": "extra",
                    }
                ),
            ),
            ("duplicate_risk_flags", _valid_proposal(risk_flags=["pii", "pii"])),
            (
                "whitespace_only_content",
                _valid_proposal(intended_memory_content="   "),
            ),
            (
                "vague_single_token",
                _valid_proposal(
                    source_citation={"ref": "somewhere", "label": "observed"}
                ),
            ),
            (
                "actor_prose_source",
                _valid_proposal(
                    source_citation={
                        "ref": "user said so earlier today",
                        "label": "observed",
                    }
                ),
            ),
            ("null_category", _valid_proposal(category=None)),
            ("boolean_expected_use", _valid_proposal(expected_use=True)),
            ("null_source_citation", _valid_proposal(source_citation=None)),
            ("risk_flags_string", _valid_proposal(risk_flags="secret")),
            (
                "trailing_newline_locator",
                _valid_proposal(
                    source_citation={
                        "ref": "conversation://current/preference\n",
                        "label": "observed",
                    }
                ),
            ),
            (
                "non_ascii_path_locator",
                _valid_proposal(
                    source_citation={
                        "ref": "docs/caf\u00e9/note.md",
                        "label": "observed",
                    }
                ),
            ),
        ]

        mismatches: list[str] = []
        for name, payload in corpus:
            schema_ok = _schema_valid(payload)
            parser_ok = _parser_valid(payload)
            if schema_ok != parser_ok:
                mismatches.append(
                    f"{name}: schema_valid={schema_ok} parser_valid={parser_ok}"
                )

        assert mismatches == []
        assert not _parser_valid(corpus[-2][1])
        assert not _parser_valid(corpus[-1][1])

    def test_source_locator_pattern_is_cross_engine_equivalent(self) -> None:
        import subprocess

        from open_brain.memory_write_proposal import (
            SOURCE_LOCATOR_PATTERN,
            is_source_locator,
        )

        samples = {
            "conversation://current/preference": True,
            "agent-session:codex:session-123": True,
            "docs/meeting notes/decision.md": True,
            "python/src/open_brain/session_summary.py": True,
            "somewhere": False,
            "conversation://current/preference\n": False,
            "docs/caf\u00e9/note.md": False,
        }

        for value, expected in samples.items():
            assert is_source_locator(value) is expected, value
            schema_probe = _valid_proposal(
                source_citation={"ref": value, "label": "observed"}
            )
            assert _schema_valid(schema_probe) is expected, value

        node = subprocess.run(
            [
                "node",
                "-e",
                (
                    "const pattern = process.argv[1];"
                    "const samples = JSON.parse(process.argv[2]);"
                    "const re = new RegExp(pattern);"
                    "const out = {};"
                    "for (const [value, expected] of Object.entries(samples)) {"
                    "  out[value] = re.test(value) === expected;"
                    "}"
                    "console.log(JSON.stringify(out));"
                ),
                SOURCE_LOCATOR_PATTERN,
                json.dumps(samples),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert node.returncode == 0, node.stderr
        node_results = json.loads(node.stdout)
        assert all(node_results.values()), node_results


# ─── AC3/AC4: owned docs + examples + boundary catalog ─────────────────────────


class TestProposalDocumentationAndExamples:
    def test_standard_documents_matrix_failure_catalog_and_version(self) -> None:
        text = PROPOSAL_DOC.read_text(encoding="utf-8")
        assert "memory-write-proposal.v1" in text
        assert "docs/standards/" in text
        assert "Source locator" in text or "source locator" in text
        assert "Failure catalog" in text or "failure catalog" in text
        lowered = text.lower()
        for token in (
            "missing_authorization",
            "vague_source",
            "unclear_retention",
            "illegal_epistemic_combination",
            "source_locator_pattern",
            "scheme-qualified",
            "namespaced",
            "path-like",
            "docs/meeting notes/decision.md",
            "ascii",
        ):
            assert token.lower() in lowered
        assert "invalid_category_use" not in text

    def test_documented_json_examples_bound_to_headings_and_outcomes(self) -> None:
        from open_brain.memory_write_judge import judge_memory_write_proposal
        from open_brain.memory_write_proposal import (
            parse_memory_write_proposal,
            proposal_preflight_issues,
        )

        text = PROPOSAL_DOC.read_text(encoding="utf-8")
        all_json_blocks = re.findall(r"```json\n(.*?)```", text, flags=re.DOTALL)
        pattern = re.compile(
            r"### (?P<heading>[^\n]+)\n+"
            r"Outcome:\s*(?P<outcome>[^\n]+)\n+"
            r"```json\n(?P<body>.*?)```",
            flags=re.DOTALL,
        )
        examples = list(pattern.finditer(text))
        assert len(examples) == len(all_json_blocks)
        assert {match.group("body") for match in examples} == set(all_json_blocks)

        for match in examples:
            heading = match.group("heading").strip()
            outcome = match.group("outcome").strip()
            payload = json.loads(match.group("body"))
            proposal, errors = parse_memory_write_proposal(payload)
            preflight = proposal_preflight_issues(payload)

            if outcome == "valid":
                assert proposal is not None, heading
                assert errors == []
                assert preflight == [], heading
            elif outcome.startswith("parse:"):
                code = outcome.split(":", 1)[1]
                assert proposal is None, heading
                assert any(error.code == code for error in errors), (heading, errors)
            elif outcome.startswith("preflight:"):
                code = outcome.split(":", 1)[1]
                assert proposal is not None, heading
                assert any(issue.code == code for issue in preflight), (
                    heading,
                    preflight,
                )
            elif outcome.startswith("judge:"):
                decision, reason_category = outcome.split(":", 1)[1].split("/", 1)
                result = judge_memory_write_proposal(payload)
                assert result.decision == decision, heading
                assert result.reason_category == reason_category, heading
            else:
                raise AssertionError(f"unknown outcome marker for {heading}: {outcome}")

    def test_eval_suite_proposals_remain_schema_parseable_except_schema_cases(self) -> None:
        from open_brain.memory_write_judge import judge_memory_write_proposal
        from open_brain.memory_write_proposal import parse_memory_write_proposal

        cases = json.loads(EVAL_SUITE.read_text(encoding="utf-8"))["cases"]
        assert len(cases) >= 20
        for case in cases:
            proposal, errors = parse_memory_write_proposal(case["proposal"])
            outcome = judge_memory_write_proposal(case["proposal"])
            assert outcome.decision == case["expected_decision"], case["case_id"]
            assert outcome.reason_category == case["expected_reason_category"], case[
                "case_id"
            ]
            if case["expected_reason_category"] == "schema":
                assert proposal is None
                assert errors
            elif errors and case["expected_reason_category"] == "risk":
                # Risk-first malformed envelopes may be schema-invalid.
                assert outcome.issues
                assert case["case_id"].startswith("block-")
            else:
                assert proposal is not None, case["case_id"]
                assert errors == [], case["case_id"]
                assert _schema_valid(case["proposal"]), case["case_id"]


# ─── Compatibility: judge imports public contract (no second copy) ─────────────


class TestJudgeUsesPublicContract:
    def test_judge_module_does_not_redefine_proposal_dataclass(self) -> None:
        source = JUDGE_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        class_names = {
            node.name for node in tree.body if isinstance(node, ast.ClassDef)
        }
        assert "MemoryWriteProposal" not in class_names
        assert "AuthorizationBasis" not in class_names
        assert "ProvenanceRef" not in class_names
        assert "from open_brain.memory_write_proposal import" in source

    def test_judge_reexports_remain_importable(self) -> None:
        from open_brain.memory_write_judge import (
            MemoryWriteProposal,
            ProvenanceRef,
            judge_memory_write_proposal,
            parse_memory_write_proposal,
            raw_proposal_payload,
        )
        from open_brain.memory_write_proposal import (
            MemoryWriteProposal as PublicProposal,
            parse_memory_write_proposal as public_parse,
        )

        assert MemoryWriteProposal is PublicProposal
        assert parse_memory_write_proposal is public_parse

        raw = _valid_proposal(
            source_citation={"ref": "agent://summary-draft", "label": "generated"},
            expected_use="evidence",
            category="observation",
            retention_scope="session",
            authorization_basis={
                "ref": "policy://session/evidence-write",
                "label": "observed",
                "granted_by": "system",
            },
        )
        proposal, errors = parse_memory_write_proposal(raw)
        assert errors == []
        assert isinstance(proposal, PublicProposal)
        outcome = judge_memory_write_proposal(raw)
        assert outcome.decision == "ALLOW"
        assert "granted_by" in raw_proposal_payload(proposal)["authorization_basis"]
        assert isinstance(ProvenanceRef(ref="x://y", label="observed").ref, str)

    def test_inferred_instruction_still_reaches_judge_revise_not_schema_block(self) -> None:
        from open_brain.memory_write_judge import judge_memory_write_proposal

        outcome = judge_memory_write_proposal(
            _valid_proposal(
                source_citation={"ref": "agent://intent-inference", "label": "inferred"},
                expected_use="instruction",
            )
        )
        assert outcome.decision == "REVISE"
        assert outcome.reason_category == "evidence"

    def test_risk_first_blocks_secret_before_schema_escalate(self) -> None:
        from open_brain.memory_write_judge import judge_memory_write_proposal

        cases = [
            _valid_proposal(risk_flags=["secret", "secret"]),
            _valid_proposal(risk_flags=["secret"], actor_note="store this"),
            _valid_proposal(
                source_citation={"ref": "somewhere", "label": "observed"},
                risk_flags=["credential"],
            ),
        ]
        for raw in cases:
            outcome = judge_memory_write_proposal(raw)
            assert outcome.decision == "BLOCK"
            assert outcome.reason_category == "risk"
            assert outcome.issues is not None
            assert outcome.issues
            assert "intended_memory_content" not in outcome.reason
            payload = outcome.to_payload()
            assert "issues" in payload
            assert "User prefers" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_schema_failure_exposes_structured_issues_on_outcome_and_tool(
        self,
    ) -> None:
        from unittest.mock import AsyncMock, patch

        from open_brain.data_layer.interface import SaveMemoryResult
        from open_brain.memory_write_judge import judge_memory_write_proposal

        bad = _valid_proposal(source_citation={"ref": "somewhere", "label": "observed"})
        outcome = judge_memory_write_proposal(bad)
        assert outcome.decision == "ESCALATE"
        assert outcome.reason_category == "schema"
        assert outcome.issues is not None
        assert any(issue["code"] == "vague_source" for issue in outcome.issues)
        payload = outcome.to_payload()
        assert "issues" in payload
        assert payload["issues"][0]["field"]

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=42, message="Memory saved")
        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import save_memory

            result = await save_memory(
                text="bad",
                proposal=bad,
                provenance={
                    "producer": "test-suite",
                    "source_ref": "test-suite:schema-issues",
                },
            )
        data = json.loads(result)
        assert data["error"] == "memory_write_judge_rejected"
        assert data["judge"]["issues"]
        assert any(issue["code"] == "vague_source" for issue in data["judge"]["issues"])
        mock_dl.save_memory.assert_not_called()

    def test_agent_contract_documents_locator_grammar_and_issues(self) -> None:
        text = AGENT_DOC.read_text(encoding="utf-8")
        assert "source locator" in text.lower() or "Source locator" in text
        assert "scheme-qualified" in text
        assert "namespaced" in text
        assert "path-like" in text
        assert "issues" in text
        assert "vague_source" in text


# ─── save_memory compatibility probe (AC2) ─────────────────────────────────────


class TestSaveMemoryProposalCompatibility:
    @pytest.mark.asyncio
    async def test_save_memory_accepts_public_module_payload(self) -> None:
        from unittest.mock import AsyncMock, patch

        from open_brain.data_layer.interface import SaveMemoryResult
        from open_brain.memory_write_proposal import (
            build_memory_write_proposal,
            raw_proposal_payload,
        )

        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=42, message="Memory saved")
        proposal = raw_proposal_payload(
            build_memory_write_proposal(
                intended_memory_content="User prefers concise status updates.",
                category="preference",
                source_citation={
                    "ref": "conversation://current/preference",
                    "label": "observed",
                },
                authorization_basis={
                    "ref": "conversation://current/preference",
                    "label": "observed",
                    "granted_by": "user",
                },
                expected_use="instruction",
                retention_scope="personal",
                risk_flags=[],
            )
        )

        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.classify_and_extract", return_value={}),
            patch("open_brain.server._extract_entities", return_value={}),
        ):
            from open_brain.server import save_memory

            result = await save_memory(
                text="User prefers concise status updates.",
                proposal=proposal,
                provenance={
                    "producer": "test-suite",
                    "source_ref": "test-suite:test_memory_write_proposal",
                },
            )

        data = json.loads(result)
        assert data["id"] == 42
        call_args = mock_dl.save_memory.call_args[0][0]
        assert call_args.metadata["memory_write_judge"]["decision"] == "ALLOW"
        assert call_args.provenance["producer"] == "test-suite"

    def test_public_module_path_exists_without_judge_import_cycle(self) -> None:
        source = PUBLIC_MODULE.read_text(encoding="utf-8")
        assert "memory_write_judge" not in source
