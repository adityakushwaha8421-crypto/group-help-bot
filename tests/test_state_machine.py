import pytest

from app.cases.state_machine import InvalidTransition, transition
from app.db.models import Case, CaseStatus
from app.db.repository import status_history


async def test_transitions_logged(db):
    async with db.session_scope() as s:
        c = Case(case_id="CASE-20260910-000001", source_chat_id=1, source_user_id=1)
        s.add(c)
        await s.flush()
        assert await transition(s, c, CaseStatus.ANALYZING_EVIDENCE, reason="x")
        assert not await transition(s, c, CaseStatus.ANALYZING_EVIDENCE)  # no-op
        with pytest.raises(InvalidTransition):
            await transition(s, c, CaseStatus.VERIFIED)
        assert await transition(s, c, CaseStatus.VERIFIED, strict=False) is False
        hist = await status_history(s, c.case_id)
        assert [h.to_status for h in hist] == ["ANALYZING_EVIDENCE"]
        assert hist[0].from_status == "WAITING_FOR_INPUT"
