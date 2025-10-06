import re
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Tuple

import pandas as pd
import subprocess
import time
import pythoncom
import pywintypes

try:
    import win32com.client  # Outlook automation (Windows only)
except ImportError as e:
    raise ImportError(
        "pywin32 is required for Outlook automation. Install with: pip install pywin32"
    ) from e


# ---------------------------
# 1) Outlook initialization
# ---------------------------
# Outlook COM initialization and inbox access
def init_outlook(account_email: Optional[str] = None, ensure_running: bool = True):
    """
    Initialize Outlook COM and return (outlook_app, mapi_namespace, inbox_folder).
    Tries (in order):
      1) GetActiveObject to attach to an existing Outlook instance
      2) gencache.EnsureDispatch to create/attach via registry
      3) optionally start outlook.exe and retry
    Call CoInitialize on entry and CoUninitialize on unrecoverable failure.
    """
    pythoncom.CoInitialize()
    try:
        try:
            # Prefer attaching to an already-running instance
            outlook = win32com.client.GetActiveObject("Outlook.Application")
        except pywintypes.com_error:
            # Try to create/dispatch (gencache is more robust than Dispatch in many cases)
            outlook = win32com.client.gencache.EnsureDispatch("Outlook.Application")

        mapi = outlook.GetNamespace("MAPI")

        # Try to obtain the inbox for account_email if provided, otherwise default Inbox
        inbox = None
        if account_email:
            try:
                inbox = mapi.Folders(account_email).Folders("Inbox")
            except Exception:
                # fallback to default folder if account-specific folder not found
                inbox = None

        if inbox is None:
            try:
                inbox = mapi.GetDefaultFolder(6)  # 6 = olFolderInbox
            except Exception:
                inbox = None

        return outlook, mapi, inbox

    except pywintypes.com_error as exc:
        # Optionally try to start Outlook and retry once
        if ensure_running:
            try:
                # start Outlook via shell; allow time to initialize
                subprocess.Popen(["outlook.exe"], shell=False)
                time.sleep(5)
                # retry connect
                outlook = win32com.client.gencache.EnsureDispatch("Outlook.Application")
                mapi = outlook.GetNamespace("MAPI")
                inbox = mapi.GetDefaultFolder(6)
                return outlook, mapi, inbox
            except Exception as exc2:
                pythoncom.CoUninitialize()
                raise RuntimeError(f"Failed to start or connect to Outlook: {exc2}") from exc2

        pythoncom.CoUninitialize()
        raise RuntimeError(f"Failed to connect to Outlook via COM: {exc}") from exc


# -------------------------------------------
# 2) Subject keyword/phrase matching helpers
# -------------------------------------------
DEFAULT_SUBJECT_KEYWORDS = ["esker", "vendor", "update"]
DEFAULT_SUBJECT_PHRASES = ["esker vendor update", "esker vendor"]

def subject_matches(subject: str,
                    keywords: Iterable[str] = DEFAULT_SUBJECT_KEYWORDS,
                    phrases: Iterable[str] = DEFAULT_SUBJECT_PHRASES,
                    min_keyword_hits: int = 1) -> bool:
    """
    Return True if the subject contains:
      - any of the multi-word phrases, OR
      - at least `min_keyword_hits` of the single-word keywords (case-insensitive).
    Set min_keyword_hits=2 if you want a stricter "combination" match.
    """
    s = (subject or "").lower()

    # Phrase match: any whole phrase appears
    for ph in phrases:
        if ph.lower() in s:
            return True

    # Keyword combo match: count distinct keyword hits
    hits = sum(1 for kw in keywords if kw.lower() in s)
    return hits >= min_keyword_hits


# -----------------------------------
# 3) Body text extraction + parsing
# -----------------------------------
_TRIPLET_REGEX = re.compile(
    r"""
    (?P<company>[A-Z]{2}\d{2})      # Two letters + two digits, e.g., SG80
    \s+                             # whitespace
    (?P<vendor>[^\s]{3,40})         # vendor id (3-40 non-whitespace characters)
    \s+                             # whitespace
    (?P<name>[^\r\n]+?)             # the rest of the line (company name)
    (?=$|\r?\n)                     # end of line / string
    """,
    re.VERBOSE | re.MULTILINE
)

def html_to_text(html: str) -> str:
    """
    Very light HTML → text transform (enough for this extraction).
    If you need more robustness, use 'beautifulsoup4'.
    """
    if not html:
        return ""
    # Replace <br> and <p> with newlines, strip other tags
    txt = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", html)
    txt = re.sub(r"(?i)</\s*p\s*>", "\n", txt)
    txt = re.sub(r"(?i)<\s*p[^>]*>", "", txt)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)  # drop other tags
    txt = re.sub(r"[ \t]+", " ", txt).strip()
    return txt

def extract_triplets_from_text(text: str) -> List[Tuple[str, str, str]]:
    """
    From a block of text, extract all occurrences like:
      SG77 1000338436 SPEEDYLINK LOGISTICS SDN BHD
    Returns list of (company_code, vendor_number, name).
    """
    results: List[Tuple[str, str, str]] = []
    for m in _TRIPLET_REGEX.finditer(text or ""):
        company = m.group("company").strip()
        vendor = m.group("vendor").strip()
        name = m.group("name").strip()
        # Guard against trailing artifacts (e.g., HTML entities that slipped through)
        name = re.sub(r"\s+", " ", name).strip(" -|")
        results.append((company, vendor, name))
    return results


# -----------------------------------------------------
# 4) Scan inbox & assemble DataFrame within time window
# -----------------------------------------------------
def find_matching_emails_inbox(inbox,
                               mapi,
                               minutes_back: int = 30,
                               min_keyword_hits: int = 1,
                               store_name: str = "Inbox",
                               debug: bool = True):
    """
    Iterate recent Inbox emails (last `minutes_back` minutes) that pass the subject filter.
    Yields Message objects. Uses GetFirst()/GetNext() to reliably enumerate Outlook collections
    (handles multiple messages with same subject).
    """
    try:
        if inbox is None:
            inbox = mapi.GetDefaultFolder(6)  # 6 = olFolderInbox
    except Exception as e:
        return
    items = inbox.Items
    # Attempt to sort newest first (ignore failures)
    try:
        items.Sort("[ReceivedTime]", True)  # newest first
    except Exception:
        pass  # ignore sort failure
    cutoff = datetime.now() - timedelta(minutes=minutes_back)
    cutoff_ts = cutoff.timestamp()

    # Restrict by ReceivedTime to limit scan size (Outlook expects US date format)
    # If locale issues occur, fallback to Python-side filtering.
    try:
        # Format: mm/dd/yyyy hh:mm AM/PM
        restriction = "[ReceivedTime] >= '{}'".format(cutoff.strftime("%m/%d/%Y %I:%M %p"))
        collection = items.Restrict(restriction)
    except Exception:
        collection = items  # fallback; will filter in Python

    # Robust enumerator for COM collections: prefer GetFirst()/GetNext(), 
    #ensures all items are processed including multiple messages with identical subjects.
    def _enumerate_com_collection(coll):
        try:
            itm = coll.GetFirst()
            while itm:
                yield itm
                itm = coll.GetNext()
        except Exception:
            # Fallback: attempt Python iteration (some collections support it)
            try:
                for itm in coll:
                    yield itm
            except Exception:
                return


    for msg in _enumerate_com_collection(collection):
        try:
            #received = msg.ReceivedTime  # COM datetime
            received = getattr(msg, "ReceivedTime", None)
        except Exception:
            continue

        # Convert ReceivedTime to Python datetime when possible    
        # Python-side cutoff guard (covers fallback & any Restrict quirks)
        received_py = None
        try:
            if isinstance(received, datetime):
                received_py = received
            elif isinstance(received, (float, int)):
                try:
                    received_py = datetime.fromtimestamp(float(received))
                except Exception:
                    # final attempt: rely on strptime of ISO-like string if provided
                    try:
                        received_py = datetime.strptime(str(received), "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        received_py = None
        except Exception:
            received_py = None
        
        # If we have a received datetime, compare via timestamp (handles tz differences)
        try:
            if received_py is not None:
                if received_py.timestamp() < cutoff_ts:
                # Convert to Python datetime
                # pywin32 returns a pytime; casting via datetime works implicitly when needed
                    continue
        except Exception:
            continue  # skip if any error occurs

        subject = getattr(msg, "Subject", "") or ""
        if subject_matches(subject, min_keyword_hits=min_keyword_hits):
            if debug:
                try:
                    received_str = received_py.isoformat() if received_py is not None else str(received)
                except Exception:
                    received_str = str(received)
                print(f"[DEBUG] Matching message found - Subject: '{subject}' Received: {received_str}")
            
            yield msg


def extract_rows_from_email(msg) -> List[Tuple[str, str, str]]:
    """
    Pull plain-text from the email and extract all triplets.
    Tries .Body first; if empty or too short, falls back to stripped .HTMLBody.
    """
    body_text = (getattr(msg, "Body", None) or "").strip()
    if not body_text or len(body_text) < 5:
        # Fallback: HTML → text
        html = getattr(msg, "HTMLBody", None) or ""
        body_text = html_to_text(html)

    return extract_triplets_from_text(body_text)


def build_dataframe(rows: List[Tuple[str, str, str]]) -> pd.DataFrame:
    """
    Build a DataFrame with required columns from extracted rows.
    """
    df = pd.DataFrame(rows, columns=["company_code", "vendor_number", "name"])
    # Optional: drop exact duplicates while preserving order
    if not df.empty:
        df = df.drop_duplicates().reset_index(drop=True)
    return df


def get_esker_vendor_updates_df(
    minutes_back: int = 30,
    min_subject_keyword_hits: int = 1,
    subject_keywords: Iterable[str] = DEFAULT_SUBJECT_KEYWORDS,
    subject_phrases: Iterable[str] = DEFAULT_SUBJECT_PHRASES,
    account_email: Optional[str] = None,
    debug: bool = True,
) -> pd.DataFrame:
    """
    High-level function:
      - initializes Outlook
      - scans Inbox for last `minutes_back` minutes
      - filters by subject (keywords/phrases)
      - extracts 'SG80 10002345678 KLO PTE LTD' triplets from bodies
      - returns DataFrame with columns: company_code, vendor_number, name
    Tweak `min_subject_keyword_hits` if you want stricter matching (e.g., 2).
    If debug=True, prints how many messages were found and how many unique rows returned.
    
    """
    # Wire custom matchers into subject_matches without changing global defaults
    global DEFAULT_SUBJECT_KEYWORDS, DEFAULT_SUBJECT_PHRASES
    DEFAULT_SUBJECT_KEYWORDS = list(subject_keywords)
    DEFAULT_SUBJECT_PHRASES = list(subject_phrases)

    #_, mapi = init_outlook()
    # Initialize Outlook (attach if running, start if allowed)
    account_email = 'john.tan@sh-cogent.com.sg'
    outlook, mapi, inbox = init_outlook(account_email=account_email, ensure_running=True)

    all_rows: List[Tuple[str, str, str]] = []
    msg_count = 0
    for msg in find_matching_emails_inbox(
        inbox,
        mapi=mapi,
        minutes_back=minutes_back,
        min_keyword_hits=min_subject_keyword_hits,
        debug=debug
    ):
        msg_count +=1
        rows = extract_rows_from_email(msg)
        if debug:
            print(f"[DEBUG] Extracted {len(rows)} rows from message #{msg_count}")
        
        all_rows.extend(rows)

    if debug:
        print(f"[DEBUG] Total messages processed: {msg_count}")
        print(f"[DEBUG] Total rows extracted: {len(all_rows)}")
    df = build_dataframe(all_rows)

    if debug:
        print(f"[DEBUG] Unique rows after dedup: {len(df)}")
        if not df.empty:
            print(df.to_string(index=False))

    return df






# ---------------------------
# Example standalone usage
# ---------------------------
if __name__ == "__main__":
    # By default, match if the subject contains ANY of the single keywords
    # (or any of the phrases). To require 2+ single keywords, set min_subject_keyword_hits=2.
    df = get_esker_vendor_updates_df(
        minutes_back=30,
        min_subject_keyword_hits=1,  # set to 2 for stricter "combination" matching
        subject_keywords=["esker", "vendor", "update"],
        subject_phrases=["esker vendor update", "esker vendor"],
    )
    print(df)
