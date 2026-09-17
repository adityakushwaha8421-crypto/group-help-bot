"""Bank-statement PDF helpers: password detection and decryption (pypdf, no AI involved)."""

from __future__ import annotations

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


def decrypt_pdf(path: str | Path, password: str) -> Path | None:
    """Write a decrypted copy next to the original for analysis. None when the password is wrong."""
    from pypdf import PdfReader, PdfWriter

    src = Path(path)
    out = src.with_name(src.stem + ".decrypted.pdf")
    if out.exists():
        return out
    try:
        reader = PdfReader(str(src))
        if reader.is_encrypted and not reader.decrypt(password):
            return None
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        with out.open("wb") as fh:
            writer.write(fh)
        out.chmod(0o600)
        return out
    except Exception:  # noqa: BLE001
        return None
