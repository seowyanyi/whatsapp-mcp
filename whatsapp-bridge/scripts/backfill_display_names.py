#!/usr/bin/env python3
"""Backfill messages.sender_display / content_display with resolved contact names.

The Go bridge resolves display names for *new* messages, but rows stored before
the fix (or synced from history) may still hold bare phone numbers in
`sender_display` and raw @<lid> tokens in `content_display`.

This script re-resolves those rows using the same logic as the bridge's
`bestContactName`:

  * A person has up to two JID forms - phone (@s.whatsapp.net) and LID (@lid),
    linked via whatsapp.db's whatsmeow_lid_map.
  * whatsmeow stores full_name (your address book) and push_name (their self-set
    name) on whichever row it learned about, often different rows.
  * For any number we therefore check both forms and prefer full_name, then
    push_name.

Usage:
    python3 backfill_display_names.py            # dry run (default): report only
    python3 backfill_display_names.py --apply    # write changes (makes a backup)

Paths default to ../store/{messages,whatsapp}.db relative to this script and can
be overridden with --messages-db / --whatsapp-db.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# A mention token in content_display, e.g. "@52871668191275". WhatsApp user-parts
# are digits only; we require >=5 to avoid touching "@4pm"-style text.
MENTION_RE = re.compile(r"@(\d{5,})")


def load_maps(wa: sqlite3.Connection):
    """Return (lid->pn, pn->lid) dicts of user-part strings."""
    lid_to_pn: dict[str, str] = {}
    pn_to_lid: dict[str, str] = {}
    for lid, pn in wa.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
        lid_to_pn[str(lid)] = str(pn)
        pn_to_lid[str(pn)] = str(lid)
    return lid_to_pn, pn_to_lid


def load_contacts(wa: sqlite3.Connection):
    """Return {jid_user: (full_name, push_name)} keyed by the JID user-part.

    Keys are bare user-parts (no @server). Both the phone row and the LID row of
    the same person are stored under their own user-part so the resolver can look
    up either form.
    """
    contacts: dict[str, tuple[str, str]] = {}
    rows = wa.execute(
        "SELECT their_jid, full_name, push_name FROM whatsmeow_contacts"
    )
    for their_jid, full_name, push_name in rows:
        user = str(their_jid).split("@", 1)[0]
        full_name = (full_name or "").strip()
        push_name = (push_name or "").strip()
        prev_full, prev_push = contacts.get(user, ("", ""))
        contacts[user] = (prev_full or full_name, prev_push or push_name)
    return contacts


def make_resolver(lid_to_pn, pn_to_lid, contacts):
    """Return name(number) -> str|None resolving across both JID forms."""

    def candidates(num: str):
        seen = [num]
        if num in lid_to_pn:
            seen.append(lid_to_pn[num])
        if num in pn_to_lid:
            seen.append(pn_to_lid[num])
        return seen

    def resolve(num: str):
        full = push = ""
        for cand in candidates(num):
            c_full, c_push = contacts.get(cand, ("", ""))
            full = full or c_full
            push = push or c_push
        return full or push or None

    return resolve


def backfill(messages_db: Path, whatsapp_db: Path, apply: bool) -> int:
    wa = sqlite3.connect(f"file:{whatsapp_db}?mode=ro", uri=True)
    lid_to_pn, pn_to_lid = load_maps(wa)
    resolve = make_resolver(lid_to_pn, pn_to_lid, load_contacts(wa))
    wa.close()

    if apply:
        backup = messages_db.with_suffix(f".db.bak-{int(time.time())}")
        shutil.copy2(messages_db, backup)
        print(f"Backup written: {backup}")

    db = sqlite3.connect(messages_db)
    rows = db.execute(
        "SELECT id, chat_jid, sender, sender_display, content_display FROM messages"
    ).fetchall()

    sender_fixed = content_fixed = 0
    updates: list[tuple[str | None, str | None, str, str]] = []
    samples: list[str] = []

    for msg_id, chat_jid, sender, sender_display, content_display in rows:
        new_sender = sender_display
        new_content = content_display

        # sender_display: fix only when it's a bare number we can name.
        if sender_display and sender_display.isdigit():
            name = resolve(str(sender)) if sender else None
            if name:
                new_sender = name

        # content_display: replace each resolvable @<number> token.
        if content_display and "@" in content_display:
            def repl(m: re.Match) -> str:
                name = resolve(m.group(1))
                return f"@{name}" if name else m.group(0)

            new_content = MENTION_RE.sub(repl, content_display)

        if new_sender != sender_display or new_content != content_display:
            if new_sender != sender_display:
                sender_fixed += 1
            if new_content != content_display:
                content_fixed += 1
                if len(samples) < 8:
                    samples.append(
                        f"  {content_display[:55]!r}\n   -> {new_content[:55]!r}"
                    )
            updates.append((new_sender, new_content, msg_id, chat_jid))

    print(f"Scanned {len(rows)} messages.")
    print(f"  sender_display rows resolvable:  {sender_fixed}")
    print(f"  content_display rows resolvable: {content_fixed}")
    if samples:
        print("\nSample content_display changes:")
        print("\n".join(samples))

    if not apply:
        print("\nDry run - no changes written. Re-run with --apply to commit.")
        db.close()
        return 0

    db.executemany(
        "UPDATE messages SET sender_display = ?, content_display = ? "
        "WHERE id = ? AND chat_jid = ?",
        updates,
    )
    db.commit()
    db.close()
    print(f"\nApplied {len(updates)} row updates.")
    return 0


def main() -> int:
    here = Path(__file__).resolve().parent
    store = here.parent / "store"
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--messages-db", type=Path, default=store / "messages.db")
    ap.add_argument("--whatsapp-db", type=Path, default=store / "whatsapp.db")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    for p in (args.messages_db, args.whatsapp_db):
        if not p.exists():
            print(f"error: database not found: {p}", file=sys.stderr)
            return 1

    return backfill(args.messages_db, args.whatsapp_db, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
