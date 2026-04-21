"""Tests for wake-up pack feature.

AK7: Unit test: token budget is respected (output <= budget in rough token count)
AK8: Unit test: category mapping works for all 5 categories + fallback
AK9: Unit test: importance-rank ordering is applied within each category
"""

from __future__ import annotations


# ─── Helper to build Memory objects ───────────────────────────────────────────

def _make_memory(
    id: int = 1,
    type: str = "observation",
    title: str | None = None,
    content: str = "some content",
    metadata: dict | None = None,
    importance: str = "medium",
    priority: float = 0.5,
    stability: str = "stable",
    access_count: int = 0,
    updated_at: str = "2026-01-01T00:00:00",
    project_name: str | None = None,
):
    """Build a Memory-like object for testing."""
    from open_brain.data_layer.interface import Memory

    return Memory(
        id=id,
        index_id=1,
        session_id=None,
        type=type,
        title=title,
        subtitle=None,
        narrative=None,
        content=content,
        metadata=metadata or {},
        priority=priority,
        stability=stability,
        access_count=access_count,
        last_accessed_at=None,
        created_at="2026-01-01T00:00:00",
        updated_at=updated_at,
        user_id=None,
        importance=importance,
        project_name=project_name,
    )


# ─── AK8: Category mapping ────────────────────────────────────────────────────

class TestCategoryMapping:
    """classify_memory() correctly maps memories to the 6 buckets."""

    def test_identity_by_type(self):
        """type=='identity' maps to 'identity'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="identity")
        assert classify_memory(m) == "identity"

    def test_identity_by_metadata_category(self):
        """metadata.category=='identity' maps to 'identity'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="observation", metadata={"category": "identity"})
        assert classify_memory(m) == "identity"

    def test_decision_by_type(self):
        """type=='decision' maps to 'decisions'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="decision")
        assert classify_memory(m) == "decisions"

    def test_decision_by_metadata_category(self):
        """metadata.category=='decision' maps to 'decisions'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="observation", metadata={"category": "decision"})
        assert classify_memory(m) == "decisions"

    def test_constraint_by_type(self):
        """type=='constraint' maps to 'constraints'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="constraint")
        assert classify_memory(m) == "constraints"

    def test_constraint_by_metadata_category(self):
        """metadata.category=='constraint' maps to 'constraints'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="observation", metadata={"category": "constraint"})
        assert classify_memory(m) == "constraints"

    def test_constraint_canonical_rule(self):
        """stability=='canonical' AND type=='rule' maps to 'constraints'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="rule", stability="canonical")
        assert classify_memory(m) == "constraints"

    def test_constraint_canonical_policy(self):
        """stability=='canonical' AND type=='policy' maps to 'constraints'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="policy", stability="canonical")
        assert classify_memory(m) == "constraints"

    def test_constraint_rule_not_canonical_goes_to_context(self):
        """stability!='canonical' AND type=='rule' goes to 'context' (not constraints)."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="rule", stability="stable")
        assert classify_memory(m) == "context"

    def test_error_by_type(self):
        """type=='error_resolved' maps to 'errors'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="error_resolved")
        assert classify_memory(m) == "errors"

    def test_error_by_metadata_category(self):
        """metadata.category=='error' maps to 'errors'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="observation", metadata={"category": "error"})
        assert classify_memory(m) == "errors"

    def test_project_by_project_name(self):
        """Any non-empty project_name maps to 'project'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="observation", project_name="open-brain")
        assert classify_memory(m) == "project"

    def test_project_by_metadata_category(self):
        """metadata.category=='project' maps to 'project'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="observation", metadata={"category": "project"})
        assert classify_memory(m) == "project"

    def test_fallback_to_context(self):
        """Unmatched memories fall back to 'context'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="observation", metadata={})
        assert classify_memory(m) == "context"

    def test_no_project_name_goes_to_context(self):
        """Memory with no project_name and no matching type/metadata goes to 'context'."""
        from open_brain.wake_up import classify_memory
        m = _make_memory(type="observation", project_name=None)
        assert classify_memory(m) == "context"


# ─── AK9: Importance ordering within category ─────────────────────────────────

class TestImportanceOrdering:
    """build_wake_up_pack() applies importance-rank ordering within categories."""

    def test_critical_before_low(self):
        """Critical importance appears before low importance in output."""
        from open_brain.wake_up import build_wake_up_pack
        memories = [
            _make_memory(id=1, type="identity", content="Low entry", importance="low",
                         title="Low entry", updated_at="2026-01-03T00:00:00"),
            _make_memory(id=2, type="identity", content="Critical entry", importance="critical",
                         title="Critical entry", updated_at="2026-01-01T00:00:00"),
        ]
        result = build_wake_up_pack(memories, token_budget=2000)
        critical_pos = result.find("Critical entry")
        low_pos = result.find("Low entry")
        assert critical_pos != -1, "Critical entry must appear in output"
        assert low_pos != -1, "Low entry must appear in output"
        assert critical_pos < low_pos, "Critical entry must come before low entry"

    def test_high_before_medium(self):
        """High importance appears before medium importance in output."""
        from open_brain.wake_up import build_wake_up_pack
        memories = [
            _make_memory(id=1, type="decision", content="Medium decision", importance="medium",
                         title="Medium decision", updated_at="2026-01-02T00:00:00"),
            _make_memory(id=2, type="decision", content="High decision", importance="high",
                         title="High decision", updated_at="2026-01-01T00:00:00"),
        ]
        result = build_wake_up_pack(memories, token_budget=2000)
        high_pos = result.find("High decision")
        medium_pos = result.find("Medium decision")
        assert high_pos != -1, "High entry must appear in output"
        assert medium_pos != -1, "Medium entry must appear in output"
        assert high_pos < medium_pos, "High importance must come before medium importance"

    def test_same_importance_priority_tiebreak(self):
        """Higher priority float breaks tie between equal-importance entries."""
        from open_brain.wake_up import build_wake_up_pack
        memories = [
            _make_memory(id=1, type="constraint", content="Low priority constraint",
                         importance="medium", priority=0.1, title="Low priority",
                         updated_at="2026-01-01T00:00:00"),
            _make_memory(id=2, type="constraint", content="High priority constraint",
                         importance="medium", priority=0.9, title="High priority",
                         updated_at="2026-01-01T00:00:00"),
        ]
        result = build_wake_up_pack(memories, token_budget=2000)
        high_pos = result.find("High priority")
        low_pos = result.find("Low priority")
        assert high_pos != -1
        assert low_pos != -1
        assert high_pos < low_pos, "Higher priority float must come first within same importance"


# ─── AK7: Token budget enforcement ────────────────────────────────────────────

class TestTokenBudget:
    """build_wake_up_pack() respects the token budget."""

    def test_output_within_budget(self):
        """Output token estimate must not exceed token_budget."""
        from open_brain.wake_up import build_wake_up_pack, token_estimate

        # Create many memories that would exceed a small budget
        memories = [
            _make_memory(
                id=i,
                type="identity",
                content="x" * 400,  # ~100 tokens each
                title=f"Memory {i}",
                importance="medium",
            )
            for i in range(20)
        ]
        budget = 200
        result = build_wake_up_pack(memories, token_budget=budget)
        actual_tokens = token_estimate(result)
        assert actual_tokens <= budget, (
            f"Output tokens {actual_tokens} exceeded budget {budget}"
        )

    def test_empty_output_when_budget_zero(self):
        """With budget=0, output should be empty or minimal."""
        from open_brain.wake_up import build_wake_up_pack, token_estimate

        memories = [_make_memory(id=1, type="identity", content="some content")]
        result = build_wake_up_pack(memories, token_budget=0)
        assert token_estimate(result) == 0, "Budget=0 should produce empty output"

    def test_all_memories_included_when_budget_large(self):
        """All memories are included when budget is very large."""
        from open_brain.wake_up import build_wake_up_pack

        memories = [
            _make_memory(id=1, type="identity", content="Identity memory", title="Identity"),
            _make_memory(id=2, type="decision", content="Decision memory", title="Decision"),
            _make_memory(id=3, type="constraint", content="Constraint memory", title="Constraint"),
        ]
        result = build_wake_up_pack(memories, token_budget=99999)
        assert "Identity" in result
        assert "Decision" in result
        assert "Constraint" in result

    def test_lowest_ranked_dropped_first(self):
        """When budget is tight, lowest-ranked (low importance) entries are dropped first."""
        from open_brain.wake_up import build_wake_up_pack

        # One critical entry (~20 tokens content), one low entry (~20 tokens content).
        # Budget is sized to fit exactly the header + critical entry but NOT the low entry.
        # header = "## Identity\n" ~3 tokens
        # critical line = "- **Critical entry** (critical): CCC..." ~ 25 tokens
        # Total for critical ~28 tokens. Budget=35 fits critical but not both.
        critical_content = "C" * 80  # ~20 tokens
        low_content = "L" * 80       # ~20 tokens

        memories = [
            _make_memory(id=1, type="identity", content=critical_content,
                         title="Critical entry", importance="critical"),
            _make_memory(id=2, type="identity", content=low_content,
                         title="Low entry", importance="low"),
        ]

        result = build_wake_up_pack(memories, token_budget=35)

        # The critical entry MUST appear; the low entry MUST NOT.
        assert "Critical entry" in result, "Critical entry must be included within budget"
        assert "Low entry" not in result, "Low entry must be dropped when budget is tight"

    def test_context_bucket_only_if_budget_remains(self):
        """Context (fallback) bucket is omitted when named categories have consumed the budget."""
        from open_brain.wake_up import build_wake_up_pack, token_estimate, _format_entry

        # _format_entry truncates content to 200 chars, so actual token cost is deterministic.
        # "## Identity\n" = 3 tokens; identity entry line ≈ 58 tokens → section ≈ 61 tokens.
        # "## Context\n" = 2 tokens; context entry line ≈ 11 tokens → section ≈ 13 tokens.
        # Total if both fit: ~74 tokens. Budget=70 fits identity (61) but not context (74).
        big_content = "A" * 1600  # content is truncated to 200 chars in _format_entry
        memories = [
            _make_memory(id=1, type="identity", content=big_content,
                         title="Identity entry", importance="critical"),
            _make_memory(id=2, type="observation", content="Context fallback",
                         title="Context entry", importance="medium"),
        ]
        result = build_wake_up_pack(memories, token_budget=70)

        # Budget invariant must hold.
        assert token_estimate(result) <= 70, "Total output must not exceed budget"
        # The identity section must be present (it fits within 70 tokens).
        assert "## Identity" in result, "Identity section must appear"
        # The context section must NOT appear — budget is exhausted by the identity section.
        assert "## Context" not in result, (
            "Context section must be omitted when budget is exhausted by named categories"
        )


# ─── AK2: Category grouping ───────────────────────────────────────────────────

class TestCategoryGrouping:
    """build_wake_up_pack() groups memories into the 5 named categories + context fallback."""

    def test_all_five_categories_present(self):
        """All 5 named categories appear as sections when memories exist for each."""
        from open_brain.wake_up import build_wake_up_pack

        memories = [
            _make_memory(id=1, type="identity", content="I am Claude", title="Identity"),
            _make_memory(id=2, type="decision", content="Use asyncpg", title="Decision"),
            _make_memory(id=3, type="constraint", content="No SQL injection", title="Constraint"),
            _make_memory(id=4, type="error_resolved", content="Fixed bug", title="Error"),
            _make_memory(id=5, type="observation", project_name="project:myapp",
                         content="Project context", title="Project"),
        ]
        result = build_wake_up_pack(memories, token_budget=9999)
        assert "## Identity" in result
        assert "## Decisions" in result
        assert "## Constraints" in result
        assert "## Errors" in result
        assert "## Project" in result

    def test_empty_category_omitted(self):
        """Categories with no memories are omitted from output."""
        from open_brain.wake_up import build_wake_up_pack

        memories = [
            _make_memory(id=1, type="identity", content="I am Claude", title="Identity"),
        ]
        result = build_wake_up_pack(memories, token_budget=9999)
        assert "## Identity" in result
        assert "## Decisions" not in result
        assert "## Constraints" not in result
        assert "## Errors" not in result
        assert "## Project" not in result
        assert "## Context" not in result

    def test_context_fallback_appears_last(self):
        """Context (fallback) section appears after all named categories."""
        from open_brain.wake_up import build_wake_up_pack

        memories = [
            _make_memory(id=1, type="identity", content="Identity entry", title="Identity"),
            _make_memory(id=2, type="observation", content="Fallback entry", title="Fallback"),
        ]
        result = build_wake_up_pack(memories, token_budget=9999)
        identity_pos = result.find("## Identity")
        context_pos = result.find("## Context")
        assert identity_pos != -1
        assert context_pos != -1
        assert identity_pos < context_pos, "Context section must come after Identity"

    def test_entry_format(self):
        """Each entry is formatted as '- **title** (importance): content'."""
        from open_brain.wake_up import build_wake_up_pack

        memories = [
            _make_memory(id=1, type="identity", title="My Identity",
                         content="I am an AI assistant", importance="critical"),
        ]
        result = build_wake_up_pack(memories, token_budget=9999)
        assert "**My Identity**" in result
        assert "(critical)" in result
        assert "I am an AI assistant" in result
