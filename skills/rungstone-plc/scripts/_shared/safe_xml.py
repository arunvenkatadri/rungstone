"""
safe_xml.py - XML parsing for untrusted customer files.

Uses defusedxml when it is installed.  Otherwise falls back to the standard
library parser after refusing any document that declares a DTD or entities
(the vectors for entity-expansion and external-entity attacks).  PLC, HMI and
SCADA exports (L5X, SimaticML, PLCopen, FactoryTalk, WinCC, Ignition) never
need a DTD, so refusing them costs nothing.

parse_file() streams the document with iterparse and drops bulky subtrees
(tag data values) as soon as they close, so tens-of-MB exports do not have to
be held in memory with their data.
"""

import re
import xml.etree.ElementTree as _ET

try:  # pragma: no cover - depends on environment
    import defusedxml.ElementTree as _DET
    HAVE_DEFUSED = True
except Exception:  # noqa: BLE001
    _DET = None
    HAVE_DEFUSED = False

ParseError = _ET.ParseError
Element = _ET.Element

_DECL_RE = re.compile(br"<!\s*(DOCTYPE|ENTITY)", re.I)


class UnsafeXMLError(ValueError):
    """Raised when a document declares a DTD or entities."""


def _guard(head):
    m = _DECL_RE.search(head)
    if m:
        raise UnsafeXMLError("XML declares <!%s>; refused (DTDs/entities are not allowed in project files)"
                             % m.group(1).decode("ascii", "replace").upper())


def fromstring(data):
    """Parse bytes or str into an Element, safely."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    _guard(data[:200000])
    if HAVE_DEFUSED:
        return _DET.fromstring(data, forbid_dtd=True)
    return _ET.fromstring(data)


def _lname(tag):
    return tag.split("}", 1)[1] if isinstance(tag, str) and tag.startswith("{") else tag


def parse_file(path, drop=()):
    """Stream-parse a file; subtrees whose local name is in `drop` are emptied.

    Returns the root Element.  Dropped elements keep their tag and attributes
    (so structure stays inspectable) but lose children and text.
    """
    with open(path, "rb") as f:
        head = f.read(200000)
    _guard(head)
    drop = set(drop)
    it = (_DET.iterparse(path, events=("start", "end"), forbid_dtd=True) if HAVE_DEFUSED
          else _ET.iterparse(path, events=("start", "end")))
    root = None
    depth_in_drop = 0
    for ev, el in it:
        if ev == "start":
            if root is None:
                root = el
            if _lname(el.tag) in drop:
                depth_in_drop += 1
        else:
            if _lname(el.tag) in drop:
                depth_in_drop -= 1
                for c in list(el):
                    el.remove(c)
                el.text = None
    return root


def tostring(el, encoding="unicode"):
    return _ET.tostring(el, encoding=encoding)
