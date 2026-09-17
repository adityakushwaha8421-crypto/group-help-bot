"""The PDF password out of ANY natural wording (English / Hindi / Hinglish), never the word "password" itself,
and never asked for twice when the message already carried it. Rules first, the model only for what they miss."""

import pytest

from app.ai.extractor import extract_from_text, extract_mobile, read_password
from app.cases.correlation import ai_password, attach_message, missing_items, statement_needs_password
from app.db.repository import list_evidence
from tests.conftest import make_input
from tests.test_file_detection import make_pdf

MOB = "7733931348"
PW = "10753260494"


# ---------------------------------------------------------------- every wording the operator uses
@pytest.mark.parametrize(
    "text",
    [
        f"Password is {PW}",
        f"Password: {PW}",
        f"Password:- {PW}",
        f"Password - {PW}",
        f"Password = {PW}",
        f"PDF password is {PW}",
        f"PDF password: {PW}",
        f"PDF pass is {PW}",
        f"Pass: {PW}",
        f"Pass:- {PW}",
        f"Pass = {PW}",
        f"Pw: {PW}",
        f"PW:- {PW}",
        f"Pwd: {PW}",
        f"Pwd:- {PW}",
        f"The password is {PW}",
        f"Password for PDF is {PW}",
        f"PDF ka password {PW} hai",
        f"Password {PW} hai",
        # spacing, case and punctuation the operator actually types
        f"password    :-    {PW}",
        f"PASSWORD IS {PW}",
        f"Password:-{PW}",
        f"password={PW}",
        f"statement ka pw hai {PW}",
        f"the pdf password for the bank statement is {PW}",
        f"Pass word - {PW}",
        f"pin: {PW}",
    ],
)
def test_every_wording_gives_the_value(text):
    assert read_password(text) == PW
    assert extract_from_text(text).statement_password.value == PW


@pytest.mark.parametrize(
    "text",
    ["Please send the password", "password", "Password is", "what is the pdf password?", "ok", "thanks", PW],
)
def test_the_word_password_is_never_the_password(text):
    assert read_password(text) is None  # a bare token is handled by the awaiting-case rule, not by the keyword


def test_a_numeric_password_is_not_read_as_a_mobile_or_utr():
    assert extract_mobile("Password is 9175404354") is None
    assert extract_mobile("mobile 9175404354, password is 4321") == "9175404354"
    e = extract_from_text("PDF password 611532946151 hai")
    assert e.statement_password.value == "611532946151" and e.utr.value is None
    assert extract_from_text("UTR 611532946151").utr.value == "611532946151"


# ---------------------------------------------------------------- the case never asks twice
async def test_a_password_in_the_caption_is_never_asked_for(db, tmp_path):
    locked = make_pdf(tmp_path / "s.pdf", PW)
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        st = await attach_message(s, make_input(3, "document", f"PDF ka password {PW} hai", filename="s.pdf"))
        st.evidence.local_path = str(locked)
        ev = await list_evidence(s, r.case.case_id)
        assert r.case.statement_password == PW
        assert not statement_needs_password(r.case, ev) and "statement password" not in missing_items(r.case, ev)


async def test_a_sentence_password_reaches_the_waiting_case(db, tmp_path):
    locked = make_pdf(tmp_path / "s.pdf", "4321")
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        await attach_message(s, make_input(2, "text", MOB))
        st = await attach_message(s, make_input(3, "document", filename="s.pdf"))
        st.evidence.local_path, st.evidence.downloaded = str(locked), True
        assert "statement password" in missing_items(r.case, await list_evidence(s, r.case.case_id))
        await attach_message(s, make_input(5, "video"))
        await attach_message(s, make_input(4, "text", "PDF ka password 4321 hai bhai"))
        assert r.case.statement_password == "4321"
        assert missing_items(r.case, await list_evidence(s, r.case.case_id)) == []


# ---------------------------------------------------------------- the model reads what the rules cannot
async def test_the_model_is_asked_only_when_the_rules_find_nothing(db, tmp_path, fake_ai):
    locked = make_pdf(tmp_path / "s.pdf", "4321")
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        st = await attach_message(s, make_input(3, "document", filename="s.pdf"))
        st.evidence.local_path, st.evidence.downloaded = str(locked), True
        await attach_message(s, make_input(4, "text", f"password is {PW}"))  # the rules handle this one
        assert fake_ai.password_calls == 0 and r.case.statement_password == PW


async def test_the_model_reads_an_unusual_sentence(db, tmp_path, fake_ai):
    fake_ai.password = "4321"
    locked = make_pdf(tmp_path / "s.pdf", "4321")
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        st = await attach_message(s, make_input(3, "document", filename="s.pdf"))
        st.evidence.local_path, st.evidence.downloaded = str(locked), True
        await attach_message(s, make_input(4, "text", "wo jo pehle bheja tha na, 4321 wahi lagana file pe"))
        assert fake_ai.password_calls == 1 and r.case.statement_password == "4321"


async def test_the_model_may_say_there_is_none(fake_ai):
    fake_ai.password = None
    assert await ai_password("kya hua bhai, koi update?") is None


async def test_a_model_failure_is_not_an_error(fake_ai, monkeypatch):
    async def boom(text):
        raise RuntimeError("no api key")

    monkeypatch.setattr(fake_ai, "read_password", boom)
    assert await ai_password("some odd sentence with 4321 in it") is None
