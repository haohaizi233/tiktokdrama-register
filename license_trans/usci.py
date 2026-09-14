from __future__ import annotations

CHARSET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
CONFUSABLES = {
    "0": "OQD",
    "1": "I7T",
    "2": "Z",
    "5": "S",
    "6": "G",
    "8": "B",
    "B": "8R",
    "D": "0O",
    "G": "6C",
    "O": "0DQ",
    "Q": "0O",
    "S": "5",
    "Z": "2",
}


def _val(ch: str) -> int:
    return CHARSET.index(ch)


def checksum_ok(code: str) -> bool:
    code = (code or "").upper().replace(" ", "")
    if len(code) != 18:
        return False
    try:
        total = sum(_val(code[i]) * WEIGHTS[i] for i in range(17))
    except ValueError:
        return False
    check = 31 - total % 31
    if check == 31:
        check = 0
    return CHARSET[check] == code[17]


def is_company_usci(code: str) -> bool:
    code = (code or "").upper()
    return len(code) >= 2 and code[0] == "9" and code[1] == "1"


def repair_usci(code: str) -> str:
    """Fix a near-miss 18-char USCI by substituting one confused glyph."""
    code = (code or "").upper().replace(" ", "")
    if checksum_ok(code):
        return code
    if len(code) != 18:
        return code
    for i, ch in enumerate(code):
        for alt in CONFUSABLES.get(ch, ""):
            if alt not in CHARSET and i < 17:
                continue
            if i == 17 and alt not in CHARSET:
                continue
            trial = code[:i] + alt + code[i + 1 :]
            if checksum_ok(trial):
                return trial
        if ch not in CHARSET:
            for alt in CHARSET:
                trial = code[:i] + alt + code[i + 1 :]
                if checksum_ok(trial):
                    return trial
    return code
