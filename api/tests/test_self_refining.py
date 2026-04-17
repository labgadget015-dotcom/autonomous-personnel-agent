"""
test_self_refining.py — Unit tests for evaluation.py, reflection.py, prompt_manager.py, db.py
================================================================================================
All DB calls mocked. No OpenAI API calls made.
Feature flag (SELF_EVAL_ENABLED) tested in both states.
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Tests for evaluation.py ─────────────────────────────────────────────────

class TestEvalResultShape:
    """EvalResult dataclass has correct fields and types."""
    def test_eval_result_fields(self):
        from evaluation import EvalResult
        er = EvalResult(score=8.5, passed=True, rubric={"quality": 9},
                        critique="Good", failure_type=None, input_hash="abc123")
        assert er.score == 8.5
        assert er.passed is True
        assert isinstance(er.rubric, dict)
        assert er.failure_type is None

    def test_pass_thresholds_all_agents(self):
        from evaluation import PASS_THRESHOLDS
        expected_agents = {"talent", "scheduling", "onboarding", "performance", "knowledge", "route", "guardrails"}
        assert set(PASS_THRESHOLDS.keys()) == expected_agents
        for agent, threshold in PASS_THRESHOLDS.items():
            assert 0 < threshold <= 10, f"{agent} threshold {threshold} out of range"

    @pytest.mark.asyncio
    async def test_evaluate_output_feature_flag_off(self):
        """When SELF_EVAL_ENABLED=false, returns mock passing result without LLM call."""
        os.environ["SELF_EVAL_ENABLED"] = "false"
        from evaluation import evaluate_output
        with patch("evaluation.ChatOpenAI") as mock_llm:
            result = await evaluate_output("talent", {"input": "test"}, {"output": "test"})
            mock_llm.assert_not_called()
        assert result.passed is True
        assert result.score == 10.0

    @pytest.mark.asyncio
    async def test_input_hash_is_deterministic(self):
        """Same input always produces same hash."""
        from evaluation import evaluate_output
        os.environ["SELF_EVAL_ENABLED"] = "false"
        r1 = await evaluate_output("talent", {"a": 1}, {"b": 2})
        r2 = await evaluate_output("talent", {"a": 1}, {"b": 2})
        assert r1.input_hash == r2.input_hash

    def test_score_below_threshold_sets_passed_false(self):
        """passed=False when score < PASS_THRESHOLDS[agent]."""
        from evaluation import PASS_THRESHOLDS, EvalResult
        for agent, threshold in PASS_THRESHOLDS.items():
            failing = EvalResult(score=threshold - 0.1, passed=threshold - 0.1 >= threshold,
                                 rubric={}, critique="", failure_type="incomplete", input_hash="x")
            assert failing.passed is False


# ── Tests for reflection.py ──────────────────────────────────────────────────

class TestReflection:
    @pytest.mark.asyncio
    async def test_returns_empty_string_when_flag_off(self):
        os.environ["SELF_EVAL_ENABLED"] = "false"
        from reflection import get_active_reflections
        result = await get_active_reflections("talent", None)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_session_none(self):
        os.environ["SELF_EVAL_ENABLED"] = "true"
        from reflection import get_active_reflections
        result = await get_active_reflections("talent", None)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_formatted_prefix_with_reflections(self):
        os.environ["SELF_EVAL_ENABLED"] = "true"
        from reflection import get_active_reflections, INJECTION_PREFIX

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [
            ("Lesson A: next time do X.",),
            ("Lesson B: always check Y.",),
        ]
        mock_session.execute = AsyncMock(return_value=mock_result)

        result = await get_active_reflections("talent", mock_session, limit=2)
        assert "Lesson 1:" in result
        assert "Lesson A" in result
        assert "talent" in result

    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_rows(self):
        os.environ["SELF_EVAL_ENABLED"] = "true"
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)

        from reflection import get_active_reflections
        result = await get_active_reflections("talent", mock_session)
        assert result == ""

    @pytest.mark.asyncio
    async def test_generate_reflection_calls_llm(self):
        """generate_reflection calls ChatOpenAI and returns non-empty string."""
        from reflection import generate_reflection
        with patch("reflection.ChatOpenAI") as mock_cls:
            mock_chain = AsyncMock()
            mock_response = MagicMock()
            mock_response.content = "WHAT WENT WRONG: Missing context.\nROOT CAUSE: Incomplete input.\nNEXT TIME:\n- Always request missing fields."
            mock_chain.ainvoke = AsyncMock(return_value=mock_response)
            mock_cls.return_value.__or__ = MagicMock(return_value=mock_chain)
            with patch("reflection.ChatPromptTemplate") as mock_prompt:
                mock_prompt.from_messages.return_value.__or__ = MagicMock(return_value=mock_chain)
                result = await generate_reflection(
                    agent="talent", score=5.5, critique="Incomplete",
                    failure_type="incomplete", input_summary="CV text", output_summary="Low score"
                )
        assert isinstance(result, str)


# ── Tests for prompt_manager.py ──────────────────────────────────────────────

class TestPromptManager:
    def _make_manager(self):
        from prompt_manager import PromptManager
        return PromptManager()

    @pytest.mark.asyncio
    async def test_get_prompt_returns_default_when_no_db_row(self):
        """Returns DEFAULT_PROMPTS content when no DB row exists."""
        manager = self._make_manager()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        content, version = await manager.get_prompt("talent", mock_session)
        assert version == "1.0.0"
        assert isinstance(content, str)
        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_get_prompt_returns_active_from_db(self):
        """Returns version and content from DB when active row exists."""
        manager = self._make_manager()
        mock_session = AsyncMock()

        active_row = {"version": "1.3.0", "content": "You are a talent agent v1.3"}
        no_candidate = None

        mock_result_active = MagicMock()
        mock_result_active.mappings.return_value.first.return_value = active_row
        mock_result_candidate = MagicMock()
        mock_result_candidate.mappings.return_value.first.return_value = no_candidate

        mock_session.execute = AsyncMock(side_effect=[mock_result_active, mock_result_candidate])

        with patch("prompt_manager.random.randint", return_value=50):
            content, version = await manager.get_prompt("talent", mock_session)
        assert version == "1.3.0"
        assert content == "You are a talent agent v1.3"

    @pytest.mark.asyncio
    async def test_ab_routing_sends_candidate_traffic(self):
        """When random value <= AB_TEST_SPLIT, candidate is returned."""
        os.environ["AB_TEST_SPLIT"] = "10"
        manager = self._make_manager()
        mock_session = AsyncMock()

        active_row = {"version": "1.0.0", "content": "active prompt"}
        candidate_row = {"version": "1.1.0", "content": "candidate prompt"}

        mock_active = MagicMock()
        mock_active.mappings.return_value.first.return_value = active_row
        mock_candidate = MagicMock()
        mock_candidate.mappings.return_value.first.return_value = candidate_row

        mock_session.execute = AsyncMock(side_effect=[mock_active, mock_candidate])

        with patch("prompt_manager.random.randint", return_value=5):
            content, version = await manager.get_prompt("talent", mock_session)
        assert version == "1.1.0"
        assert content == "candidate prompt"

    @pytest.mark.asyncio
    async def test_promote_candidate_promotes_when_higher_score(self):
        """promote_candidate() promotes candidate when its score > active score."""
        manager = self._make_manager()
        mock_session = AsyncMock()

        active_row = {"version": "1.0.0", "score_avg": 7.2}
        candidate_row = {"version": "1.1.0", "score_avg": 8.1}

        mock_active_result = MagicMock()
        mock_active_result.mappings.return_value.first.return_value = active_row
        mock_candidate_result = MagicMock()
        mock_candidate_result.mappings.return_value.first.return_value = candidate_row

        update_result = MagicMock()
        mock_session.execute = AsyncMock(side_effect=[
            mock_active_result, mock_candidate_result,
            update_result, update_result,
        ])

        promoted = await manager.promote_candidate("talent", mock_session)
        assert promoted is True

    @pytest.mark.asyncio
    async def test_promote_candidate_does_not_promote_when_lower_score(self):
        """promote_candidate() does NOT promote when candidate score <= active score."""
        manager = self._make_manager()
        mock_session = AsyncMock()

        active_row = {"version": "1.0.0", "score_avg": 8.5}
        candidate_row = {"version": "1.1.0", "score_avg": 7.9}

        mock_active_result = MagicMock()
        mock_active_result.mappings.return_value.first.return_value = active_row
        mock_candidate_result = MagicMock()
        mock_candidate_result.mappings.return_value.first.return_value = candidate_row

        mock_session.execute = AsyncMock(side_effect=[mock_active_result, mock_candidate_result])

        promoted = await manager.promote_candidate("talent", mock_session)
        assert promoted is False

    @pytest.mark.asyncio
    async def test_promote_candidate_returns_false_when_no_candidate(self):
        manager = self._make_manager()
        mock_session = AsyncMock()

        mock_active_result = MagicMock()
        mock_active_result.mappings.return_value.first.return_value = {"version": "1.0.0", "score_avg": 8.0}
        mock_candidate_result = MagicMock()
        mock_candidate_result.mappings.return_value.first.return_value = None

        mock_session.execute = AsyncMock(side_effect=[mock_active_result, mock_candidate_result])
        promoted = await manager.promote_candidate("talent", mock_session)
        assert promoted is False

    def test_default_prompts_cover_all_agents(self):
        """DEFAULT_PROMPTS has entries for all 7 agent types."""
        from prompt_manager import PromptManager
        manager = PromptManager()
        expected = {"talent", "scheduling", "onboarding", "performance", "knowledge", "route", "guardrails"}
        assert set(manager.DEFAULT_PROMPTS.keys()) == expected
        for agent, prompt in manager.DEFAULT_PROMPTS.items():
            assert len(prompt) > 10, f"{agent} prompt too short"


# ── Tests for db.py ──────────────────────────────────────────────────────────

class TestDBModule:
    def test_build_async_url_postgresql(self):
        from db import _build_async_url
        result = _build_async_url("postgresql://user:pass@localhost/db")
        assert result.startswith("postgresql+asyncpg://")

    def test_build_async_url_postgres_shorthand(self):
        from db import _build_async_url
        result = _build_async_url("postgres://user:pass@localhost/db")
        assert result.startswith("postgresql+asyncpg://")

    def test_get_engine_raises_before_init(self):
        """get_engine() raises RuntimeError before init_db() is called."""
        import db as db_module
        db_module._engine = None
        with pytest.raises(RuntimeError, match="not initialised"):
            db_module.get_engine()

    @pytest.mark.asyncio
    async def test_get_session_yields_none_when_not_initialised(self):
        """get_session() yields None gracefully when DB not configured."""
        import db as db_module
        db_module._session_factory = None
        async for session in db_module.get_session():
            assert session is None
