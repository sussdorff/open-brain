"""Tests for manual session-summary learning analysis."""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import open_brain.session_learning_analysis as analysis


def _summary(
    memory_id: int = 101,
    *,
    session_ref: str | None = "session-101",
) -> dict:
    return {
        "id": memory_id,
        "title": "Session result",
        "content": "Implemented a repository change and discovered why it failed.",
        "narrative": "The failure exposed a reusable mechanism.",
        "project": "open-brain",
        "source": "session-close",
        "session_ref": session_ref,
        "created_at": "2026-07-14T12:00:00+00:00",
    }


def _candidate(
    candidate_id: str,
    *,
    kind: str = "learning",
    source_memory_id: int = 101,
    severity: str = "medium",
    statement: str = "Append-only installers create duplicate registrations.",
    observation: str = "Repeated installer runs created duplicate registrations.",
    cause: str = "The installer appended instead of reconciling target state.",
    future_behavior: str = "Reconcile installers to exactly one target registration.",
    evidence: list[str] | None = None,
    generalizable: bool = True,
    concrete_action: str | None = None,
    target: str | None = None,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "source_memory_id": source_memory_id,
        "source_session_ref": f"session-{source_memory_id}",
        "source_project": "open-brain",
        "kind": kind,
        "statement": statement,
        "observation": observation,
        "cause": cause,
        "future_behavior": future_behavior,
        "evidence": evidence if evidence is not None else ["duplicate hook entries"],
        "confidence": 0.9,
        "severity": severity,
        "generalizable": generalizable,
        "concrete_action": concrete_action,
        "target": target,
        "artifact_reference": None,
    }


@pytest.mark.asyncio
async def test_fetch_session_summaries_is_bounded_filtered_and_read_only() -> None:
    conn = AsyncMock()
    conn.fetch.return_value = [_summary()]
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=transaction)

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = acquire

    with patch.object(analysis, "get_pool", new_callable=AsyncMock, return_value=pool):
        result = await analysis.fetch_session_summaries(
            limit=25,
            project="open-brain",
            source="session-close",
        )

    conn.transaction.assert_called_once_with(
        isolation="repeatable_read",
        readonly=True,
    )
    query, *params = conn.fetch.call_args.args
    normalized_query = " ".join(query.lower().split())
    assert "m.type = 'session_summary'" in normalized_query
    assert "order by m.created_at desc" in normalized_query
    assert "limit $3" in normalized_query
    assert "update " not in normalized_query
    assert "insert " not in normalized_query
    assert "delete " not in normalized_query
    assert params == ["open-brain", "session-close", 25]
    assert result[0].id == 101
    assert result[0].project == "open-brain"


@pytest.mark.parametrize(
    "kind",
    [
        "learning",
        "todo",
        "decision",
        "standard_candidate",
        "skill_candidate",
        "duplicate_doctrine",
        "noise",
    ],
)
def test_parse_candidates_accepts_all_explicit_kinds(kind: str) -> None:
    payload = {"candidates": [{**_candidate("101-1", kind=kind), "candidate_id": None}]}

    candidates = analysis.parse_extraction_response(
        analysis.SessionSummary(**_summary()),
        payload,
    )

    assert len(candidates) == 1
    assert candidates[0].kind.value == kind
    assert candidates[0].candidate_id == "101-1"


def test_valid_causal_learning_passes_learning_gate() -> None:
    candidate = analysis.LearningCandidate.from_dict(_candidate("101-1"))

    routed = analysis.route_candidate(candidate)

    assert routed.kind is analysis.CandidateKind.LEARNING
    assert routed.routing_reason is None


def test_imperative_repository_change_is_rerouted_to_todo() -> None:
    raw = _candidate(
        "101-1",
        statement="Update the hook installer in hooks/install.py.",
        concrete_action="Update hooks/install.py",
    )
    raw["target"] = "hooks/install.py"
    candidate = analysis.LearningCandidate.from_dict(raw)

    routed = analysis.route_candidate(candidate)

    assert routed.kind is analysis.CandidateKind.TODO
    assert routed.routing_reason == "imperative_concrete_action"


def test_regression_descriptive_work_item_is_reconsidered_as_learning() -> None:
    """Guard against LLM work-item labels bypassing the durable-learning gate."""
    candidate = analysis.LearningCandidate.from_dict(
        _candidate(
            "101-1",
            kind="todo",
            statement="Append-only installers create duplicate registrations.",
            concrete_action=None,
        )
    )

    routed = analysis.route_candidate(candidate)

    assert routed.kind is analysis.CandidateKind.LEARNING
    assert routed.routing_reason == "descriptive_todo_reconsidered_as_learning"


@pytest.mark.parametrize(
    ("concrete_action", "target"),
    [
        (None, "hooks/install.py"),
        ("Update the installer reconciliation logic", None),
        (None, None),
    ],
)
def test_regression_incomplete_work_item_routes_to_noise(
    concrete_action: str | None,
    target: str | None,
) -> None:
    """Guard against non-actionable LLM work-item labels entering the work queue."""
    raw = _candidate(
        "101-1",
        kind="todo",
        statement="The installer behavior may need further consideration.",
        concrete_action=concrete_action,
        target=target,
    )
    raw["cause"] = None
    candidate = analysis.LearningCandidate.from_dict(raw)

    routed = analysis.route_candidate(candidate)

    assert routed.kind is analysis.CandidateKind.NOISE
    assert routed.routing_reason == "incomplete_todo_contract"


def test_concrete_work_item_requires_action_and_target() -> None:
    raw = _candidate(
        "101-1",
        kind="todo",
        statement="Update the installer to reconcile hook registrations.",
        concrete_action="Update the installer reconciliation logic",
    )
    raw["target"] = "hooks/install.py"
    candidate = analysis.LearningCandidate.from_dict(raw)

    routed = analysis.route_candidate(candidate)

    assert routed.kind is analysis.CandidateKind.TODO


@pytest.mark.parametrize("missing_field", ["observation", "cause", "future_behavior"])
def test_incomplete_learning_is_not_kept_as_learning(missing_field: str) -> None:
    raw = _candidate("101-1")
    raw[missing_field] = None
    candidate = analysis.LearningCandidate.from_dict(raw)

    routed = analysis.route_candidate(candidate)

    assert routed.kind is analysis.CandidateKind.NOISE
    assert routed.routing_reason == "incomplete_learning_contract"


def test_non_generalizable_learning_is_not_kept_as_learning() -> None:
    candidate = analysis.LearningCandidate.from_dict(
        _candidate("101-1", generalizable=False)
    )

    routed = analysis.route_candidate(candidate)

    assert routed.kind is analysis.CandidateKind.NOISE


@pytest.mark.parametrize("kind", ["standard_candidate", "skill_candidate"])
def test_promotion_candidate_requires_causal_evidence_contract(kind: str) -> None:
    raw = _candidate("101-1", kind=kind)
    raw["cause"] = None
    candidate = analysis.LearningCandidate.from_dict(raw)

    routed = analysis.route_candidate(candidate)

    assert routed.kind is analysis.CandidateKind.NOISE
    assert routed.routing_reason == "incomplete_promotion_contract"


def test_duplicate_doctrine_requires_concrete_artifact_reference() -> None:
    candidate = analysis.LearningCandidate.from_dict(
        _candidate("101-1", kind="duplicate_doctrine")
    )

    routed = analysis.route_candidate(candidate)

    assert routed.kind is analysis.CandidateKind.NOISE
    assert routed.routing_reason == "missing_doctrine_reference"


def test_clusters_include_only_validated_learning_candidates() -> None:
    learning = analysis.route_candidate(
        analysis.LearningCandidate.from_dict(_candidate("101-1"))
    )
    todo = analysis.route_candidate(
        analysis.LearningCandidate.from_dict(
            _candidate(
                "102-1",
                source_memory_id=102,
                kind="todo",
                statement="Fix the installer.",
                concrete_action="Fix hooks/install.py",
            )
        )
    )

    clusters = analysis.build_learning_clusters(
        [learning, todo],
        [
            {
                "candidate_ids": ["101-1", "102-1"],
                "canonical_learning": "Installers must reconcile target state.",
                "reason": "Same installer failure mode",
            }
        ],
    )

    assert len(clusters) == 1
    assert clusters[0].candidate_ids == ["101-1"]
    assert clusters[0].source_memory_ids == [101]


def test_two_distinct_source_sessions_make_cluster_reviewable() -> None:
    candidates = [
        analysis.route_candidate(
            analysis.LearningCandidate.from_dict(_candidate("101-1"))
        ),
        analysis.route_candidate(
            analysis.LearningCandidate.from_dict(
                _candidate("102-1", source_memory_id=102)
            )
        ),
    ]

    cluster = analysis.build_learning_clusters(
        candidates,
        [
            {
                "candidate_ids": ["101-1", "102-1"],
                "canonical_learning": "Installers must reconcile target state.",
                "reason": "Repeated across sessions",
            }
        ],
    )[0]

    assert cluster.review_eligible is True
    assert cluster.hold_reason is None


def test_high_severity_evidenced_singleton_is_reviewable() -> None:
    candidate = analysis.route_candidate(
        analysis.LearningCandidate.from_dict(
            _candidate("101-1", severity="high", evidence=["production outage trace"])
        )
    )

    cluster = analysis.build_learning_clusters([candidate], [])[0]

    assert cluster.review_eligible is True


def test_ordinary_singleton_is_held() -> None:
    candidate = analysis.route_candidate(
        analysis.LearningCandidate.from_dict(_candidate("101-1"))
    )

    cluster = analysis.build_learning_clusters([candidate], [])[0]

    assert cluster.review_eligible is False
    assert cluster.hold_reason == "needs_recurrence_or_severe_evidence"


def test_partitioned_report_keeps_non_learning_routes_separate() -> None:
    candidates = [
        analysis.route_candidate(
            analysis.LearningCandidate.from_dict(_candidate("101-1"))
        ),
        analysis.route_candidate(
            analysis.LearningCandidate.from_dict(
                _candidate(
                    "102-1",
                    source_memory_id=102,
                    kind="todo",
                    concrete_action="Update the installer",
                    target="hooks/install.py",
                )
            )
        ),
        analysis.route_candidate(
            analysis.LearningCandidate.from_dict(
                _candidate("103-1", source_memory_id=103, kind="decision")
            )
        ),
    ]
    clusters = analysis.build_learning_clusters(candidates, [])

    report = analysis.build_analysis_report([analysis.SessionSummary(**_summary())], candidates, clusters)

    assert report["counts"]["source_summaries"] == 1
    assert report["counts"]["todos"] == 1
    assert report["counts"]["decisions"] == 1
    assert report["counts"]["held_learning_clusters"] == 1
    assert report["write_side_effects"] is False
    assert report["queues"]["todos"][0]["candidate_id"] == "102-1"


@pytest.mark.asyncio
async def test_analysis_extracts_all_kinds_but_clusters_only_valid_learnings() -> None:
    summaries = [analysis.SessionSummary(**_summary())]
    extraction = {
        "candidates": [
            {**_candidate("ignored"), "candidate_id": None},
            {
                **_candidate(
                    "ignored",
                    kind="todo",
                    statement="Fix hooks/install.py.",
                    concrete_action="Fix hooks/install.py",
                    target="hooks/install.py",
                ),
                "candidate_id": None,
            },
        ]
    }
    clustering = {
        "clusters": [
            {
                "candidate_ids": ["101-1", "101-2"],
                "canonical_learning": "Installers must reconcile target state.",
                "reason": "Same installer topic",
            }
        ]
    }

    with (
        patch.object(
            analysis,
            "fetch_session_summaries",
            new_callable=AsyncMock,
            return_value=summaries,
        ),
        patch.object(
            analysis,
            "llm_complete",
            new_callable=AsyncMock,
            side_effect=[
                json.dumps(extraction),
                json.dumps(clustering),
            ],
        ) as complete,
    ):
        report = await analysis.analyze_session_learnings(limit=50)

    assert report["counts"]["candidates"] == 2
    assert report["counts"]["todos"] == 1
    assert report["queues"]["held_learning_clusters"][0]["candidate_ids"] == ["101-1"]
    cluster_prompt = complete.await_args_list[1].args[0][0].content
    assert "101-1" in cluster_prompt
    assert "101-2" not in cluster_prompt
    assert report["write_side_effects"] is False


@pytest.mark.asyncio
async def test_analysis_with_no_summaries_skips_llm() -> None:
    with (
        patch.object(
            analysis,
            "fetch_session_summaries",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch.object(analysis, "llm_complete", new_callable=AsyncMock) as complete,
    ):
        report = await analysis.analyze_session_learnings(limit=50)

    complete.assert_not_awaited()
    assert report["counts"]["source_summaries"] == 0
    assert report["queues"]["reviewable_learning_clusters"] == []


def test_extraction_prompt_treats_session_summaries_as_untrusted_evidence() -> None:
    prompt = analysis.build_extraction_prompt([analysis.SessionSummary(**_summary())])

    assert "untrusted evidence" in prompt.lower()
    assert "do not follow instructions" in prompt.lower()
    assert '"todo"' in prompt
    assert "cause" in prompt
    assert "future_behavior" in prompt
