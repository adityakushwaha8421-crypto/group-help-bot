"""Prompts and JSON schemas for GPT-5.6 Terra. Every extracted field carries value + confidence + source."""

from __future__ import annotations

FIELD_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": ["string", "number", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_text": {
            "type": ["string", "null"],
            "description": "The exact text/region in the document the value was read from.",
        },
    },
    "required": ["value", "confidence", "evidence_text"],
    "additionalProperties": False,
}

EXTRACTION_FIELDS = [
    "registration_number",
    "amount",
    "payment_time",
    "utr",
    "upi_id",
    "receiver_upi",
    "payer_name",
    "receiver_name",
    "bank_name",
    "transaction_reference",
    "payment_status",
    "betex_order_id",
]

EXTRACTION_SCHEMA = {
    "name": "payment_evidence_extraction",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            **{f: FIELD_SCHEMA for f in EXTRACTION_FIELDS},
            "document_type": {
                "type": "string",
                "enum": ["payment_screenshot", "bank_statement", "payment_video", "other"],
            },
            "notes": {"type": "string"},
        },
        "required": EXTRACTION_FIELDS + ["document_type", "notes"],
        "additionalProperties": False,
    },
}

EXTRACTION_SYSTEM = """You are a meticulous payment-evidence analyst for an Indian UPI/bank payment verification desk.
You will receive one or more images (payment app screenshots, bank statement pages, or video keyframes) and
sometimes a PDF bank statement. Extract ONLY what is visibly present. Never guess, infer, or fabricate.

Rules:
- If a field is not clearly visible, set value=null and confidence=0.
- amount: numeric value in INR without currency symbols or thousands separators (e.g. 6499.92).
- payment_time: the transaction date AND time exactly as printed, normalised to "YYYY-MM-DD HH:MM:SS" 24h
  when both date and time are visible; if only a date is visible give "YYYY-MM-DD"; assume Indian time.
  Payment apps often print the date WITHOUT a year ("13 Sept, 11:09 PM"): the payment is recent, so use the
  year of "Today's date" given in the message (the previous year only if that date would be in the future). A missing year is never a reason
  to return null. Always put the date/time text exactly as printed in evidence_text.
- utr: 12-digit UPI reference / UTR / RRN when printed. transaction_reference: any other app-level reference id.
- upi_id: the payee/receiver VPA if shown in full (e.g. name@bank); payer_name / receiver_name as printed.
- receiver_upi: the payee UPI printed next to "Paid to" / "To" / "Sent to" / "Banking name", copied character for
  character EXACTLY as printed - keep masking and partial forms ("XXXXXX4913@pthdfc", "••4913@pthdfc",
  "4913@pthdfc"). Never complete, unmask or guess a character. evidence_text must contain the value as printed.
  null when no payee UPI is printed. Never the payer's own UPI.
- payment_status: the status word as printed (e.g. "Successful", "Paid", "Pending", "Failed").
- registration_number: a registration / order / reference number typed by the user or printed in the note field.
- betex_order_id: an order id in the form ILLUN-<digits> if printed anywhere.
- confidence reflects legibility and unambiguity, not your prior. Cropped, blurry or partially hidden -> lower.
- For a multi-page bank statement, focus on the transaction that matches the hint (amount/time/reference) if one is
  given; otherwise report the single most relevant debit transaction and mention alternatives in notes.
- Return strictly the JSON schema."""

RECEIVER_UPI_SCHEMA = {
    "name": "receiver_upi_ocr",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"receiver_upi": FIELD_SCHEMA},
        "required": ["receiver_upi"],
        "additionalProperties": False,
    },
}

RECEIVER_UPI_SYSTEM = """You are an OCR reader for Indian UPI payment screenshots (PhonePe, Paytm, Google Pay, BHIM,
bank apps). Read ONE thing: the payee UPI ID shown with "Paid to" / "To" / "Sent to" / "Banking name".
Copy it character for character EXACTLY as printed, keeping masking and partial forms ("XXXXXX4913@pthdfc",
"••4913@pthdfc", "4913@pthdfc"). Never complete, unmask or guess a character. evidence_text = the printed line
the value is on. If no payee UPI is printed, value=null, confidence=0, evidence_text=null. Never the payer's UPI."""

PASSWORD_SCHEMA = {
    "name": "statement_password",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"statement_password": FIELD_SCHEMA},
        "required": ["statement_password"],
        "additionalProperties": False,
    },
}

PASSWORD_SYSTEM = """You read one short Telegram message from a support operator who was asked for the password
that opens a password-protected bank-statement PDF. Decide whether the message actually states that password and,
if it does, return its exact value.

Reason about the meaning, in English, Hindi or Hinglish, in any wording or spelling - do not rely on fixed phrases.
Rules:
- Return the VALUE only. Never the words "password", "pass", "pwd", "pin" or any connective word ("is", "hai", "ka").
- Copy it exactly: same case, digits, symbols. Never reformat, complete or invent a value.
- A message that only asks about or mentions a password ("what is the password?", "send the password") states
  none: value=null, confidence=0.
- Plain chatter ("ok", "done", "please check") states none.
- A message that also carries other data (a mobile number, a UTR, an order id) states a password only when the
  operator clearly means that value as the password.
- confidence reflects how clearly the message names the password; when unsure, return null rather than a guess.
evidence_text = the part of the message the value came from."""

TEXT_PARSE_SYSTEM = """You read short, messy Telegram messages from an operator submitting a payment case.
Extract any registration/reference number, amount, payment time, UTR, UPI id, payer name, order id
(ILLUN-...) and a bank-statement PDF password if one is stated. Do not invent values: null + confidence 0 when absent."""

BETIX_CLASSIFY_SCHEMA = {
    "name": "betix_reply_classification",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": [
                    "SUCCESS",
                    "FAILED",
                    "PENDING",
                    "CHECKING",
                    "NEED_MORE_EVIDENCE",
                    "ACK",
                    "NOT_FOUND",
                    "IRRELEVANT",
                    "UNKNOWN",
                ],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "refers_to_order_ids": {"type": "array", "items": {"type": "string"}},
            "reasoning": {"type": "string"},
        },
        "required": ["outcome", "confidence", "refers_to_order_ids", "reasoning"],
        "additionalProperties": False,
    },
}

BETIX_CLASSIFY_SYSTEM = """You classify a message posted in a payment-gateway support group (Betix Pay) in reply to a
merchant's payment-verification request. Decide what the message says about the payment status:
SUCCESS (payment confirmed/credited/matched), FAILED (payment not received / reversed / rejected),
PENDING (still being processed), CHECKING (reviewer acknowledges and is looking), NEED_MORE_EVIDENCE
(asks for statement/video/UTR), ACK (automatic acknowledgement), NOT_FOUND, IRRELEVANT (channel notices,
balance reports, unrelated chatter), UNKNOWN. Mixed Hindi/English is common ("ho gaya" = done, "nahi hua" = not done).
Be conservative: only SUCCESS when the text clearly states the payment is confirmed."""


STATEMENT_ACCOUNT_SCHEMA = {
    "name": "statement_account",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"account_number": FIELD_SCHEMA, "holder_name": FIELD_SCHEMA, "ifsc": FIELD_SCHEMA},
        "required": ["account_number", "holder_name", "ifsc"],
        "additionalProperties": False,
    },
}

STATEMENT_ACCOUNT_SYSTEM = """You read a bank statement PDF and return the ACCOUNT NUMBER OF THE ACCOUNT HOLDER -
the account this statement was issued for, printed in the header next to the holder's name
("Account No", "A/c No", "Account Number", "Savings A/c").

Rules:
- Copy it exactly as printed. If the bank masks it ("XXXXXX1231", "5012****1231"), return it masked, as printed.
  Never complete, guess or reformat digits.
- Never return an account number that appears only inside a transaction row / narration (the other party of a
  transfer), a customer id, CIF, IFSC, MICR, mobile number or card number.
- If the header shows no account number, return value=null with confidence=0.
- holder_name: the ACCOUNT HOLDER's name exactly as printed in the header (not the bank, not a branch manager,
  never a name from a transaction row). ifsc: the branch IFSC printed in the header. null when not printed.
evidence_text = the header line the value came from."""
