"""Bank-statement PDF helpers: password detection and decryption (pypdf, no AI involved)."""

from __future__ import annotations

import re
from pathlib import Path


def pdf_view(path: str | Path | None) -> Path | None:
    """The statement as a file the AI can open as a PDF, whatever its extension.

    .pdf -> itself.  A PDF under another extension (MobiKwik exports "Txn Statement ....uu" that are plain PDFs)
    -> a ".pdf" copy next to it.  A real uuencoded file ("begin 644 name") holding a PDF -> the decoded ".pdf".
    Anything else -> None."""
    if not path or not Path(path).exists():
        return None
    src = Path(path)
    if src.suffix.lower() == ".pdf":
        return src
    out = src.with_name(src.name + ".pdf")
    if out.exists():
        return out
    try:
        data = src.read_bytes()
    except OSError:
        return None
    if data[:1024].lstrip().startswith(b"%PDF"):
        pdf = data
    elif data.lstrip().startswith(b"begin "):
        pdf = _uudecode(data)
        if not pdf or not pdf.lstrip().startswith(b"%PDF"):
            return None
    else:
        return None
    out.write_bytes(pdf)
    out.chmod(0o600)
    return out


def _uudecode(data: bytes) -> bytes | None:
    import binascii

    out = bytearray()
    started = False
    for line in data.splitlines():
        if not started:
            started = line.startswith(b"begin ")
            continue
        if line.strip() in (b"end", b"`", b""):
            if line.strip() == b"end":
                break
            continue
        try:
            out += binascii.a2b_uu(line)
        except binascii.Error:
            nbytes = (((line[0] - 32) & 63) * 4 + 5) // 3  # tolerate encoders that pad differently
            try:
                out += binascii.a2b_uu(line[:nbytes])
            except binascii.Error:
                return None
    return bytes(out) if started else None


def pdf_is_encrypted(path: str | Path | None) -> bool:
    """True when the PDF needs a password to open. Unreadable / missing files count as not encrypted."""
    if not path or not Path(path).exists():
        return False
    try:
        from pypdf import PdfReader

        return bool(PdfReader(str(path)).is_encrypted)
    except Exception:  # noqa: BLE001
        return False


WRONG_PASSWORD = "wrong password"
UNREADABLE = "unreadable"


def password_variants(password: str) -> list[str]:
    """The password as typed first, then the harmless slips: surrounding punctuation, inner spaces, letter case,
    and the digits alone (a date of birth typed as 06/10/1990 for a PDF locked with 06101990)."""
    seen: list[str] = []

    def add(v: str) -> None:
        if v and v not in seen:
            seen.append(v)

    add(password)
    bare = password.strip(" \t:=-–—>~.,;!?'\"()[]{}*`")
    add(bare)
    add(re.sub(r"\s+", "", bare))
    add(bare.upper())
    add(bare.lower())
    add(bare.capitalize())
    digits = re.sub(r"\D", "", bare)
    if len(digits) >= 4:
        add(digits)
    return seen


def _decrypt_pypdf(src: Path, out: Path, password: str) -> str:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(src))
    if reader.is_encrypted and not reader.decrypt(password):
        return WRONG_PASSWORD
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    with out.open("wb") as fh:
        writer.write(fh)
    return "ok"


def _decrypt_pikepdf(src: Path, out: Path, password: str) -> str:
    """A second opener (qpdf): it reads many protected PDFs pypdf cannot."""
    try:
        import pikepdf
    except ImportError:
        return UNREADABLE
    try:
        with pikepdf.open(str(src), password=password) as pdf:
            pdf.save(str(out))
        return "ok"
    except pikepdf.PasswordError:
        return WRONG_PASSWORD
    except Exception:  # noqa: BLE001
        return UNREADABLE


def open_protected_pdf(path: str | Path, password: str) -> tuple[Path | None, str]:
    """A decrypted copy next to the original (for analysis), with the reason when there is none:
    "wrong password" (every variant of it was rejected by both openers) or "unreadable" (the file itself)."""
    src = Path(path)
    out = src.with_name(src.stem + ".decrypted.pdf")
    if out.exists():
        return out, "ok"
    reasons: set[str] = set()
    for variant in password_variants(password):
        for opener in (_decrypt_pypdf, _decrypt_pikepdf):
            try:
                reason = opener(src, out, variant)
            except Exception:  # noqa: BLE001  a broken file, or a PDF feature the opener lacks
                reason = UNREADABLE
            if reason == "ok":
                try:
                    out.chmod(0o600)
                except OSError:
                    pass
                return out, "ok"
            reasons.add(reason)
    if out.exists():  # a half-written copy of a failed attempt
        out.unlink(missing_ok=True)
    return None, (WRONG_PASSWORD if WRONG_PASSWORD in reasons else UNREADABLE)


def decrypt_pdf(path: str | Path, password: str) -> Path | None:
    """Write a decrypted copy next to the original for analysis. None when the password is wrong."""
    return open_protected_pdf(path, password)[0]
