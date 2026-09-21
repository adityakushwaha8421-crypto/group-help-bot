"""Whose statement is it? The payout's account number against the statement's header - in full, in groups,
masked - and never against the transaction rows, where OTHER people's accounts appear."""

from app.admin.payouts import parse_payout_page
from app.evidence.statement_account import MATCH, MISMATCH, UNKNOWN, compare_account, header_of, mask_account

ACCT = "50100123451231"


def test_the_full_number_in_any_grouping():
    assert compare_account(ACCT, "Account No : 50100123451231\nIFSC KKBK0001770").result == MATCH
    assert compare_account(ACCT, "A/c No 5010 0123 4512 31").result == MATCH
    assert compare_account(ACCT, "Account Number: 150100123451231").result == MISMATCH  # a longer number is another


def test_a_masked_number_matches_on_what_it_shows():
    assert compare_account(ACCT, "Account No: XXXXXXXXXX1231").how == "masked"
    assert compare_account(ACCT, "Savings A/c 5010****1231").result == MATCH
    assert compare_account(ACCT, "Account No: XXXXXXXXXX9999").result == MISMATCH
    assert compare_account(ACCT, "A/c 6020XXXX1231").result == MISMATCH  # same tail, other head


def test_another_account_is_a_mismatch_and_nothing_is_unknown():
    r = compare_account(ACCT, "Name: A B\nAccount Number: 99887766554433")
    assert r.result == MISMATCH and r.seen == "99887766554433"
    assert compare_account(ACCT, "Statement of account\nName: A B").result == UNKNOWN
    assert compare_account(ACCT, "").result == UNKNOWN


def test_only_the_header_decides():
    text = (
        "HDFC BANK\nName: Someone Else\nAccount No: 99887766554433\nBranch: Pune\n"
        "Date Narration Withdrawal Deposit Balance\n"
        "01/09 NEFT TO 50100123451231 RAHUL 500.00 1,000.00\n"
    )
    head = header_of(text)
    assert "50100123451231" not in head  # the row naming OUR account is someone else's transfer
    assert compare_account(ACCT, head).result == MISMATCH


def test_numbers_are_masked_for_display():
    assert mask_account(ACCT) == "•" * 10 + "1231"
    assert mask_account(None) == "-"


PAGE = """Payouts
WD-65913-68129
Payout WD-65913-68129
AMOUNT
₹926.25
STATUS
Success
UPDATED
20 Sep 2026 12:56
BENEFICIARY
Test User
BANK ACCOUNT
Kotak Mahindra Bank
PUNE BIBEWADI
ACCOUNT
50100123451231
IFSC
KKBK0001770
UTR
626312340212
"""


def test_the_payout_page_is_read_by_its_labels():
    p = parse_payout_page("WD-65913-68129", PAGE)
    assert (p.account, p.ifsc, p.bank, p.beneficiary) == (ACCT, "KKBK0001770", "Kotak Mahindra Bank", "Test User")
    assert p.amount == 926.25 and p.status == "Success" and p.utr == "626312340212"
    assert parse_payout_page("WD-11111-22222", PAGE) is None  # another withdrawal's page is never used


# ------------------------------------------------------------------ partly hidden account numbers
PAYOUT = "273501000021809"


def test_the_last_four_digits_are_enough():
    """The user's example: Illunise 273501000021809, statement XXXXXXXX1809."""
    r = compare_account(PAYOUT, "Name: A B\nAccount No: XXXXXXXX1809\nIFSC: FDRL0007778")
    assert r.result == MATCH and r.how == "masked" and r.seen == "XXXXXXXX1809"


def test_every_visible_digit_is_used():
    ok = [
        "A/c No: XXXXXXXXXXX1809",  # last 4
        "A/c No: XXXXXXXXX021809",  # last 6
        "Account Number 2735XXXXXXX1809",  # first digits + last digits
        "Account: 2735 XXXX XXXX 809",  # printed in groups
        "A/c 27350100002XXXX",  # only the first digits are visible
        "Account No. ********1809",
        "Savings A/c ending 1809",
        "Account ending with 021809",
        "A/c ....1809",
        "Account No: 00273501000021809",  # leading zeros are not another account
        "A/c XXXX-XXXX-XXX-1809",
    ]
    for text in ok:
        assert compare_account(PAYOUT, text).result == MATCH, text


def test_one_wrong_visible_digit_is_another_account():
    wrong = [
        "Account No: XXXXXXXX1808",  # last 4 differ
        "Account No: 2736XXXXXXX1809",  # same ending, another start
        "Account No: XXXXXXXXX031809",  # last 6 differ
        "Account No: 2735010XXXX1709",  # same length: a digit in the middle differs
    ]
    for text in wrong:
        r = compare_account(PAYOUT, text)
        assert r.result == MISMATCH and r.how == "masked", text


def test_too_few_visible_digits_decide_nothing():
    r = compare_account(PAYOUT, "Account No: XXXXXXXXXXXX809")  # 3 digits agree: neither match nor mismatch
    assert r.result == UNKNOWN and r.seen == "XXXXXXXXXXXX809"


def test_a_hidden_number_that_is_not_the_account_never_means_mismatch():
    """A masked mobile / card / customer id in the header is not the account: not unmatched because of it."""
    text = "Name: A B\nMobile: XXXXXX4321\nCard: XXXX XXXX XXXX 9911\nCustomer ID 55512345"
    assert compare_account(PAYOUT, text).result == UNKNOWN
    text += "\nAccount No: XXXXXXXX1809"
    assert compare_account(PAYOUT, text).result == MATCH  # ... and the real account line still decides


def test_the_full_number_of_another_account_still_wins_over_nothing():
    assert compare_account(PAYOUT, "Account Number: 99887766554433").result == MISMATCH


def test_a_number_of_zeros_matches_nothing():
    """Stripping leading zeros must never leave an empty pattern that 'matches' every statement."""
    assert compare_account("00000000000000", "Account No: 273501000021809").result == MISMATCH
    assert compare_account("00000000000000", "Name: A B").result == UNKNOWN


# ------------------------------------------------------------------ the account field is not always "at the top"
SBI_RECENT = "\n".join(
    ["Txn Date Value Date Description Ref No. Debit Credit Balance"]
    + [
        f"{d:02d} Sep 2026 {d:02d} Sep 2026 TO TRANSFER-UPI/DR/62631234{d:04d}/RAHUL 500.00 1,000.00"
        for d in range(1, 56)
    ]
    + ["Account Name : MR. SOURAV CHATTERJEE", "Account No : 00000031234567835 OTHER", "Branch : KOLKATA MAIN"]
)


def test_an_account_field_printed_after_the_rows_is_still_found():
    """Live 2026-09-21 (SBI, a/c ...7835): pypdf yields SBI's "Recent Transactions" header AFTER the rows, so the
    top of the text holds no account number at all -> "could not be verified" although the statement is right."""
    from app.evidence.statement_account import compare_statement_text

    assert compare_account("31234567835", header_of(SBI_RECENT)).result == UNKNOWN  # the old, header-only check
    r = compare_statement_text("31234567835", SBI_RECENT)
    assert r.result == MATCH and r.how == "full"  # 17 digits with leading zeros on the statement, 11 in the panel
    assert compare_statement_text("39999999999", SBI_RECENT).result == MISMATCH  # and another account is still seen


def test_the_value_on_the_next_line_and_a_masked_field_are_read():
    from app.evidence.statement_account import compare_statement_text

    assert compare_statement_text("31234567835", "rows...\n" * 50 + "Account Number :\n31234567835\n").result == MATCH
    assert compare_statement_text("31234567835", "rows...\n" * 50 + "A/c No. XXXXXXX7835\n").result == MATCH


def test_a_bare_number_in_the_rows_is_never_the_holders_account():
    """The customer's number inside someone ELSE's statement (a transfer to him) must not make it his statement."""
    from app.evidence.statement_account import compare_statement_text

    text = (
        "Account No : 99887766554\nDate Narration Withdrawal Deposit Balance\n"
        + "01/09 NEFT TO 31234567835 SOURAV 500.00\n" * 50
    )
    assert compare_statement_text("31234567835", text).result == MISMATCH


def test_unknown_says_why():
    from app.evidence.statement_account import compare_statement_text

    assert "no readable text" in compare_statement_text("31234567835", "").note
    assert "shows no account number" in compare_statement_text("31234567835", "PhonePe statement\nPaid to X 500").note
    assert "too few digits" in compare_statement_text("31234567835", "Account No: XXXXXXXX835").note


# ------------------------------------------------------------------ only the last THREE digits are visible (SBI)
SBI_ACCT = "31234567835"


class _FakeReader:
    def __init__(self, account, name=None, ifsc=None, conf=0.9):
        self.payload = {
            "account_number": {"value": account, "confidence": conf, "evidence_text": None},
            "holder_name": {"value": name, "confidence": 0.9, "evidence_text": None},
            "ifsc": {"value": ifsc, "confidence": 0.9, "evidence_text": None},
        }

    async def read_statement_account(self, path):
        return self.payload


async def _check(monkeypatch, tmp_path, text, reader, **payout):
    from app.ai.analyzer import set_analyzer
    from app.evidence import statement_account as sa

    monkeypatch.setattr(sa, "pdf_text", lambda path, max_pages=1: text)
    set_analyzer(reader)
    try:
        return await sa.check_statement(tmp_path / "s.pdf", SBI_ACCT, **payout)
    finally:
        set_analyzer(None)


async def test_three_digits_plus_the_holders_name_is_a_match(monkeypatch, tmp_path):
    """Live 2026-09-21 (SBI a/c ...7835, SOURAV CHATTERJEE): "the AI read 835: too few digits to decide"."""
    r = await _check(
        monkeypatch, tmp_path, "", _FakeReader("XXXXXXXX835", name="Mr. SOURAV CHATTERJEE"),
        beneficiary="SOURAV CHATTERJEE", ifsc="SBIN0001234",
    )  # fmt: skip
    assert r.result == MATCH and "last 3 digits agree" in r.note and "holder's name" in r.note


async def test_three_digits_plus_the_ifsc_is_a_match(monkeypatch, tmp_path):
    text = "STATE BANK OF INDIA\nAccount No : XXXXXXXX835\nIFSC : SBIN0001234\nBranch : KOLKATA"
    r = await _check(
        monkeypatch, tmp_path, text, _FakeReader(None), beneficiary="SOURAV CHATTERJEE", ifsc="SBIN0001234"
    )
    assert r.result == MATCH and "IFSC SBIN0001234" in r.note


async def test_three_digits_alone_are_still_not_enough(monkeypatch, tmp_path):
    r = await _check(
        monkeypatch, tmp_path, "", _FakeReader("XXXXXXXX835", name="RAHUL SHARMA", ifsc="HDFC0000001"),
        beneficiary="SOURAV CHATTERJEE", ifsc="SBIN0001234",
    )  # fmt: skip
    assert r.result == UNKNOWN and "only the last 3 digits are visible" in r.note and "SOURAV CHATTERJEE" in r.note


async def test_three_wrong_digits_are_another_account_whatever_the_name(monkeypatch, tmp_path):
    r = await _check(
        monkeypatch, tmp_path, "", _FakeReader("XXXXXXXX999", name="SOURAV CHATTERJEE"),
        beneficiary="SOURAV CHATTERJEE", ifsc="SBIN0001234",
    )  # fmt: skip
    assert r.result == MISMATCH


def test_names_are_compared_part_by_part():
    from app.evidence.statement_account import same_person

    assert same_person("SOURAV CHATTERJEE", "Account Name : Mr. Chatterjee Sourav Kumar")
    assert not same_person("SOURAV CHATTERJEE", "Account Name : SOURAV DAS")
    assert not same_person("", "SOURAV") and not same_person(None, "x")


def test_spelling_variants_and_initials_are_the_same_person():
    """Live 2026-09-21 (Canara, RANGANATHA C): the statement spells it differently / prints the initial apart."""
    from app.evidence.statement_account import same_person

    for printed in ("RANGANATHA C", "RANGANATH C", "C RANGANATHA", "Mr. C. Ranganatha", "RANGA NATHA C", "RANGANATHAA"):
        assert same_person("RANGANATHA C", f"Customer Name : {printed}"), printed
    assert same_person("MOHAMMED IRFAN", "Name: MOHAMMAD IRFAN") and same_person(
        "SOURAV CHATTERJEE", "SOURAV CHATERJEE"
    )
    for other in ("RANGASWAMY C", "RAGHUNATH C", "RAMA C", "NATHA"):
        assert not same_person("RANGANATHA C", f"Customer Name : {other}"), other
    assert not same_person("SOURAV CHATTERJEE", "SOURAV DAS")


async def test_the_alert_says_what_the_statement_showed(monkeypatch, tmp_path):
    r = await _check(
        monkeypatch, tmp_path, "", _FakeReader("XXXXXXXX835", name="RAHUL SHARMA", ifsc="HDFC0000001"),
        beneficiary="SOURAV CHATTERJEE", ifsc="SBIN0001234",
    )  # fmt: skip
    assert "the statement shows: no bank name, name 'RAHUL SHARMA', IFSC HDFC0000001" in r.note
    assert "SBIN0001234" in r.note


# ------------------------------------------------------------------ last 2-3 digits + the SAME BANK (operator's rule)
class _BankReader(_FakeReader):
    def __init__(self, account, bank=None, **kw):
        super().__init__(account, **kw)
        self.payload["bank_name"] = {"value": bank, "confidence": 0.9, "evidence_text": None}


CANARA = dict(beneficiary="RANGANATHA C", ifsc="CNRB0000497", bank="Canara Bank")


async def _check_canara(monkeypatch, tmp_path, text, reader):
    from app.ai.analyzer import set_analyzer
    from app.evidence import statement_account as sa

    monkeypatch.setattr(sa, "pdf_text", lambda path, max_pages=1: text)
    set_analyzer(reader)
    try:
        return await sa.check_statement(tmp_path / "s.pdf", "04972010000136", **CANARA)
    finally:
        set_analyzer(None)


async def test_three_digits_and_the_bank_are_enough(monkeypatch, tmp_path):
    """Live 2026-09-21 (Canara a/c ...0136): a scanned statement - no name, no IFSC readable. The operator's rule:
    verify the last 2-3 digits of the account and the bank, nothing else."""
    r = await _check_canara(monkeypatch, tmp_path, "", _BankReader("XXXXXXXXXXX136", bank="Canara Bank"))
    assert r.result == MATCH and "last 3 digits agree" in r.note and "the bank (Canara Bank)" in r.note


async def test_two_digits_and_the_bank_are_enough_too(monkeypatch, tmp_path):
    r = await _check_canara(monkeypatch, tmp_path, "CANARA BANK\nA/c No : XXXXXXXXXXXX36\n", _BankReader(None))
    assert r.result == MATCH and "last 2 digits agree" in r.note


async def test_the_digits_without_the_bank_are_not_enough(monkeypatch, tmp_path):
    r = await _check_canara(monkeypatch, tmp_path, "", _BankReader("XXXXXXXXXXX136", bank="HDFC Bank"))
    assert r.result == UNKNOWN and "neither the bank (Canara Bank)" in r.note and "bank 'HDFC Bank'" in r.note


async def test_wrong_digits_are_another_account_even_at_the_same_bank(monkeypatch, tmp_path):
    r = await _check_canara(monkeypatch, tmp_path, "", _BankReader("XXXXXXXXXXX137", bank="Canara Bank"))
    assert r.result == MISMATCH


def test_the_bank_is_recognised_as_printed():
    from app.evidence.statement_account import same_bank

    assert same_bank("Canara Bank", None, "CANARA BANK  e-Passbook")
    assert same_bank("Canara Bank", "CNRB0000497", "Branch IFSC : CNRB0001234")  # any IFSC of that bank
    assert same_bank("State Bank of India", None, "SBI YONO - Account Statement")
    assert same_bank("Kotak Mahindra Bank", None, "kotak 811 statement")
    assert same_bank("State Bank of India", None, "STATE BANK OF INDIA")
    assert not same_bank("Canara Bank", "CNRB0000497", "HDFC BANK LTD  IFSC HDFC0000123")
    assert not same_bank("State Bank of India", "SBIN0001234", "CENTRAL BANK OF INDIA")
    assert not same_bank("Bank of India", None, "UNION BANK")
    assert not same_bank(None, None, "CANARA BANK")


def test_a_few_agreeing_digits_on_a_number_that_is_not_the_account_mean_nothing():
    r = compare_account("04972010000136", "Name: A B\nMobile: XXXXXXXX36")
    assert r.result == UNKNOWN and r.weak == 0
