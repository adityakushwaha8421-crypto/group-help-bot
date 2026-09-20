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
