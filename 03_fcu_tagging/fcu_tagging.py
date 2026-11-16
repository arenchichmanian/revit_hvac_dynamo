# fcu_tagging.py  (v1.2)
# Dynamo for Revit - Python Script node, CPython3 engine (Revit 2022+)
# Author: Aren  |  License: MIT (see LICENSE in the repo root)
# Places normal tags and Element Number tags on the elements of locked 3D views
# (the views created by fcu_isolated_3d_views.py). Run the numbering script
# first: an element with no number gets no Element Number tag.
# Rigid ducts at or below the minimum length get no tags at all.
#
# Version history
#   v1.2  complete tag table; plenum split from the FCU by family name;
#         leaders; replace option; alternative tag names; Run toggle;
#         options string; report saved to a file
#   v1.1  short ducts left untagged
#
# Inputs:
#   IN[0] view name filter ("contains" match)          e.g. 3D_FCU-L0-095
#   IN[1] number parameter                             default DLB_ELEMENT Number
#   IN[2] tag offset from the element, in paper mm     e.g. 8
#   IN[3] minimum duct length in mm (0 = rule off)     e.g. 100
#         use the SAME value as in the numbering script
#   IN[4] run (False = preview only, True = create tags)
#   IN[5] options: "key=value; key=value"
#           tags=both|normal|number     (default both)
#           leader=free|attached|none   (default free)
#           replace=no|yes              (default no)
#             yes = first delete the tags of the types below that are already
#                   in the view, then tag again
#           through=word, word          (default fcu-pl, plenum)
#             Mechanical Equipment whose family name contains one of these
#             words is a plenum (number tag only), the rest is an FCU
#             (normal tag only)

import os
import traceback
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, Element,
    FamilySymbol, IndependentTag, TagOrientation, Reference, View3D,
    LocationCurve, LocationPoint, StorageType, LeaderEndCondition, ElementId
)
from System.Collections.Generic import List
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

doc = DocumentManager.Instance.CurrentDBDocument

VERSION = "v1.2"

# ==================================================================
# EDIT HERE: tag rules
#   Each tag is written as "Family : Type", as in Revit (capital letters
#   do not matter). Several names = alternatives, the first one found in
#   the project is used. An empty list = this kind of element never gets
#   that kind of tag.
# ==================================================================
TAG_RULES = {
    "duct": {
        "label": "Ducts",
        "normal": ["BS_GA_Duct-ADDITIONAL FLOW_01 : L/L"],
        "number": ["BS_GA_Duct_Rotated : Element Number"],
    },
    "flex": {
        "label": "Flex ducts",
        "normal": ["BS_GA_flexDuct_01 : flex duvts",
                   "BS_GA_flexDuct : flex duvts"],
        "number": ["FLEX DUCT TAG : Element Number"],
    },
    "fitting": {
        "label": "Duct fittings",
        "normal": [],
        "number": ["DUCT FITTINGS TAG.0002 : Element Number"],
    },
    "accessory": {
        "label": "Duct accessories",
        "normal": ["DLB_GA_DuctAccessoriesTag_Designation : Designation"],
        "number": ["BS_GA_DuctAccessoriesTag_Designation : ELEMENT NUMBER"],
    },
    "terminal": {
        "label": "Air terminals",
        "normal": ["BS_GA_Flow_Size : Designation.Number"],
        "number": ["BS_GA_Flow_Size : ELEMENT NUMBER"],
    },
    "fcu": {
        "label": "FCU",
        "normal": ["DLB_GA_MechanicalEquipmentTags_MechanicalEquipment_Rotate"
                   " : Designation"],
        "number": [],
    },
    "plenum": {
        "label": "Plenum",
        "normal": [],
        "number": ["BS_GA_MechanicalEquipmentTags_MechanicalEquipment"
                   " : ELEMENT NUMBER"],
    },
}

# element category -> (rule key, tag category used to list alternatives)
CATEGORY_RULES = [
    (BuiltInCategory.OST_DuctCurves, "duct", BuiltInCategory.OST_DuctTags),
    (BuiltInCategory.OST_FlexDuctCurves, "flex", BuiltInCategory.OST_FlexDuctTags),
    (BuiltInCategory.OST_DuctFitting, "fitting",
     BuiltInCategory.OST_DuctFittingTags),
    (BuiltInCategory.OST_DuctAccessory, "accessory",
     BuiltInCategory.OST_DuctAccessoryTags),
    (BuiltInCategory.OST_DuctTerminal, "terminal",
     BuiltInCategory.OST_DuctTerminalTags),
    (BuiltInCategory.OST_MechanicalEquipment, "equipment",
     BuiltInCategory.OST_MechanicalEquipmentTags),
]

# ------------------------------------------------------------------
# Inputs
# ------------------------------------------------------------------
DEFAULT_NUMBER_PARAM = "DLB_ELEMENT Number"

DEFAULT_OPTIONS = {
    "tags": "both",
    "leader": "free",
    "replace": "no",
    "through": "fcu-pl, plenum",
}


def inp(i, default):
    """Read IN[i] as trimmed text; use default if missing, None or empty."""
    try:
        v = IN[i]
    except Exception:
        return default
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def norm(text):
    """Lowercase and treat '.' like '-' so names compare regardless of separator."""
    return str(text).strip().lower().replace('.', '-')


def parse_options(text):
    """'a=1; b=2' -> ({'a': '1', 'b': '2'} merged over defaults, [unknown parts])."""
    opts = dict(DEFAULT_OPTIONS)
    unknown = []
    for part in text.replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            unknown.append(part)
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if k in opts:
            opts[k] = v
        else:
            unknown.append(part)
    return opts, unknown


filter_text = norm(inp(0, ""))
NUM_PARAM   = inp(1, DEFAULT_NUMBER_PARAM)
offset_mm   = float(inp(2, "8"))
min_len_mm  = float(inp(3, "100"))
min_len_ft  = min_len_mm / 304.8        # Revit works in feet
run         = bool(IN[4])

OPTS, UNKNOWN_OPTS = parse_options(inp(5, ""))
PASS          = OPTS["tags"].lower()
LEADER        = OPTS["leader"].lower()
REPLACE       = OPTS["replace"].lower()
THROUGH_NAMES = tuple(t.strip().lower() for t in OPTS["through"].split(",")
                      if t.strip())

DUCT_CAT = int(BuiltInCategory.OST_DuctCurves)


def option_errors():
    """Problems with the inputs. Empty list means all good."""
    errs = []
    for u in UNKNOWN_OPTS:
        errs.append("Unknown option '{}'. Allowed keys: tags, leader, replace, "
                    "through.".format(u))
    if PASS not in ("both", "normal", "number"):
        errs.append("tags must be both, normal or number (got '{}').".format(PASS))
    if LEADER not in ("free", "attached", "none"):
        errs.append("leader must be free, attached or none (got '{}')."
                    .format(LEADER))
    if REPLACE not in ("yes", "no"):
        errs.append("replace must be yes or no (got '{}').".format(REPLACE))
    if offset_mm < 0 or min_len_mm < 0:
        errs.append("offset and minimum duct length cannot be negative.")
    return errs


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def eid_int(eid):
    """ElementId -> int. Works on old and new Revit versions."""
    try:
        return eid.Value            # Revit 2024+
    except AttributeError:
        return eid.IntegerValue     # older versions


def get_name(el):
    """
    Read an element's name through the base class (CPython3 workaround for
    'TypeError: property cannot be read'), falling back to built-in parameters.
    """
    if el is None:
        return ""
    try:
        return str(Element.Name.__get__(el))
    except Exception:
        pass
    for bip in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.VIEW_NAME):
        try:
            p = el.get_Parameter(bip)
            if p is not None:
                s = p.AsString()
                if s:
                    return str(s)
        except Exception:
            pass
    return ""


def get_family_name(etype):
    """Family name of a family type (empty string if unavailable)."""
    if etype is None:
        return ""
    try:
        return str(etype.FamilyName)
    except Exception:
        pass
    try:
        p = etype.get_Parameter(BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM)
        if p is not None:
            s = p.AsString()
            if s:
                return str(s)
    except Exception:
        pass
    return ""


def family_name_lower(el):
    """Lowercase family name of a placed family, or '' if unavailable."""
    try:
        return str(el.Symbol.FamilyName).lower()
    except Exception:
        return ""


def is_plenum(el):
    """True for equipment whose family name contains a 'through' word."""
    fam = family_name_lower(el)
    for word in THROUGH_NAMES:
        if word in fam:
            return True
    return False


def duct_length_ft(el):
    """Length of a duct in feet, or None if it cannot be read."""
    try:
        p = el.get_Parameter(BuiltInParameter.CURVE_ELEM_LENGTH)
        if p is not None and p.HasValue:
            return p.AsDouble()
    except Exception:
        pass
    try:
        loc = el.Location
        if isinstance(loc, LocationCurve):
            return loc.Curve.Length
    except Exception:
        pass
    return None


def is_small_duct(el):
    """True for a rigid duct at or below the minimum length (no pressure drop)."""
    if min_len_ft <= 0:
        return False
    cat = el.Category
    if cat is None or eid_int(cat.Id) != DUCT_CAT:
        return False
    length = duct_length_ft(el)
    return length is not None and length <= min_len_ft + 1e-6


def name_key(text):
    """'Family : Type' -> comparison key (lowercase, spaces tidied)."""
    parts = [" ".join(p.split()) for p in str(text).split(":")]
    return " : ".join(parts).lower()


SYMBOLS = {}            # name key -> FamilySymbol
TAGS_BY_CATEGORY = {}   # tag category id -> ["Family : Type", ...]


def build_symbol_map():
    """Index every family type by 'family : type', and tag types by category."""
    for s in FilteredElementCollector(doc).OfClass(FamilySymbol):
        full = get_family_name(s) + " : " + get_name(s)
        key = name_key(full)
        if key not in SYMBOLS:
            SYMBOLS[key] = s
        try:
            cid = eid_int(s.Category.Id)
        except Exception:
            continue
        TAGS_BY_CATEGORY.setdefault(cid, []).append(full)


def symbol_for(names):
    """First of the alternative names that exists in the project, or None."""
    for n in names:
        s = SYMBOLS.get(name_key(n))
        if s is not None:
            return s
    return None


def read_number(el):
    """The element's number as a positive int, or None if empty/0/missing."""
    try:
        p = el.LookupParameter(NUM_PARAM)
        if p is None:
            return None
        st = p.StorageType
        if st == StorageType.Integer:
            v = p.AsInteger()
        elif st == StorageType.String:
            s = p.AsString()
            v = int(s) if s and s.strip().isdigit() else 0
        else:
            return None
        return v if v > 0 else None
    except Exception:
        return None


def anchor_point(el):
    """Where the tag points: middle of a curve, else location point, else bbox centre."""
    try:
        loc = el.Location
        if isinstance(loc, LocationCurve):
            return loc.Curve.Evaluate(0.5, True)
        if isinstance(loc, LocationPoint):
            return loc.Point
    except Exception:
        pass
    try:
        bb = el.get_BoundingBox(None)
        if bb is not None:
            return bb.Min.Add(bb.Max).Multiply(0.5)
    except Exception:
        pass
    return None


def is_locked(v):
    try:
        return bool(v.IsLocked)
    except Exception:
        return True                 # unknown: try anyway, Revit will refuse if not


def find_views():
    out = []
    for v in FilteredElementCollector(doc).OfClass(View3D):
        if v.IsTemplate:
            continue
        nm = get_name(v)
        if filter_text in norm(nm):
            out.append((nm, v))
    out.sort(key=lambda t: t[0])
    return out


def tagged_ids(tag):
    """Ids of the elements a tag points at (new and old Revit API)."""
    try:
        return [eid_int(i) for i in tag.GetTaggedLocalElementIds()]
    except Exception:
        pass
    try:
        return [eid_int(tag.TaggedLocalElementId)]
    except Exception:
        return []


def existing_tags(view):
    """[(tag element, tag type id, [tagged element ids])] for tags in the view."""
    out = []
    for t in FilteredElementCollector(doc, view.Id).OfClass(IndependentTag):
        try:
            out.append((t, eid_int(t.GetTypeId()), tagged_ids(t)))
        except Exception:
            pass
    return out


def rule_key_for(el, base_key):
    if base_key != "equipment":
        return base_key
    return "plenum" if is_plenum(el) else "fcu"


def plan_view(view, missing):
    """
    Read-only: decide which tags to create in this view.
    Returns (jobs, stats, stale). jobs = [(label, kind, element, symbol, both)],
    stale = tags to delete first when replace=yes.
    missing collects (label, kind, names, tag category id) for tags not found.
    """
    tags_now = existing_tags(view)
    rule_type_ids = set()
    for rule in TAG_RULES.values():
        for kind in ("normal", "number"):
            s = symbol_for(rule[kind])
            if s is not None:
                rule_type_ids.add(eid_int(s.Id))

    stale = [t for t, tid, _ in tags_now if tid in rule_type_ids]
    existing = set()
    if REPLACE != "yes":
        for _, tid, ids in tags_now:
            for i in ids:
                existing.add((i, tid))

    jobs = []
    stats = {"normal": 0, "number": 0, "existing": 0, "unnumbered": 0, "short": 0}
    for bic, base_key, tag_bic in CATEGORY_RULES:
        elems = list(FilteredElementCollector(doc, view.Id)
                     .OfCategory(bic).WhereElementIsNotElementType())
        for el in elems:
            if is_small_duct(el):           # too short for a pressure drop: no tags
                stats["short"] += 1
                continue
            rule = TAG_RULES[rule_key_for(el, base_key)]
            eid = eid_int(el.Id)
            both = bool(rule["normal"]) and bool(rule["number"]) and PASS == "both"
            for kind in ("normal", "number"):
                if PASS != "both" and PASS != kind:
                    continue
                names = rule[kind]
                if not names:
                    continue
                sym = symbol_for(names)
                if sym is None:
                    missing[(rule["label"], kind)] = (names, int(tag_bic))
                    continue
                if kind == "number" and read_number(el) is None:
                    stats["unnumbered"] += 1
                    continue
                if (eid, eid_int(sym.Id)) in existing:
                    stats["existing"] += 1
                    continue
                jobs.append((rule["label"], kind, el, sym, both))
                stats[kind] += 1
    return jobs, stats, stale


def head_point(anchor, kind, both, up, right, d):
    """Where the tag text goes, relative to the point on the element."""
    if up is None or right is None or d <= 0:
        return anchor
    if LEADER == "none":
        if kind == "number" and both:
            return anchor.Add(up.Multiply(d))       # number above the normal tag
        return anchor
    side = right.Multiply(d)
    if kind == "number" and both:
        return anchor.Add(side).Subtract(up.Multiply(d))
    return anchor.Add(side).Add(up.Multiply(d))


def free_the_leader(tag, anchor):
    """Give the leader a free end that sits on the element. True if it worked."""
    try:
        tag.LeaderEndCondition = LeaderEndCondition.Free
    except Exception:
        return False
    try:                                            # Revit 2022 and newer
        refs = list(tag.GetTaggedReferences())
        if refs:
            tag.SetLeaderEnd(refs[0], anchor)
            return True
    except Exception:
        pass
    try:                                            # older Revit
        tag.LeaderEnd = anchor
        return True
    except Exception:
        return False


def create_tags(view, jobs):
    """Create the planned tags. Returns (made, failed, first_error, not_freed)."""
    made, failed, first_error, not_freed = 0, 0, "", 0
    try:
        scale = int(view.Scale)
    except Exception:
        scale = 100
    d = offset_mm * scale / 304.8           # paper mm -> model feet
    try:
        up, right = view.UpDirection, view.RightDirection
    except Exception:
        up, right = None, None

    for label, kind, el, sym, both in jobs:
        eid = eid_int(el.Id)
        anchor = anchor_point(el)
        if anchor is None:
            failed += 1
            if not first_error:
                first_error = "{} {}: no location".format(label, eid)
            continue
        head = head_point(anchor, kind, both, up, right, d)
        try:
            tag = IndependentTag.Create(doc, sym.Id, view.Id, Reference(el),
                                        LEADER != "none",
                                        TagOrientation.Horizontal, head)
            made += 1
        except Exception as ex:
            failed += 1
            if not first_error:
                first_error = "{} {} ({}): {}".format(label, eid, kind, ex)
            continue
        if LEADER == "free":
            if not free_the_leader(tag, anchor):
                not_freed += 1
        if LEADER != "none":
            try:
                tag.TagHeadPosition = head
            except Exception:
                pass
    return made, failed, first_error, not_freed


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    errs = option_errors()
    if errs:
        return ["INPUT ERROR | " + e for e in errs]

    if not filter_text:
        return ["View filter is empty - refusing to run on all 3D views."]

    views = find_views()
    if not views:
        return ["No 3D view name contains '{}'.".format(filter_text)]

    build_symbol_map()

    report = ["Settings ({}): tags={}, leader={}, replace={}, offset {:.0f} mm, "
              "ducts <= {:.0f} mm untagged, through={}".format(
                  VERSION, PASS, LEADER, REPLACE, offset_mm, min_len_mm,
                  ", ".join(THROUGH_NAMES) if THROUGH_NAMES else "(none)")]
    plans = []
    missing = {}

    for name, v in views:
        if not is_locked(v):
            report.append("SKIP | {} | view is not locked (tags can only be placed "
                          "in locked 3D views)".format(name))
            continue
        jobs, stats, stale = plan_view(v, missing)
        if REPLACE != "yes":
            stale = []
        plans.append((name, v, jobs, stale))
        line = ("PLAN | {} | to create: {} normal, {} number | already tagged: {} "
                "| number tags skipped (element has no number): {} | short ducts "
                "left untagged: {}".format(
                    name, stats["normal"], stats["number"], stats["existing"],
                    stats["unnumbered"], stats["short"]))
        if stale:
            line += " | old tags to delete first: {}".format(len(stale))
        report.append(line)

    for (label, kind), (names, tag_cat) in sorted(missing.items()):
        loaded = sorted(set(TAGS_BY_CATEGORY.get(tag_cat, [])))
        shown = "; ".join(loaded[:15]) + (" ..." if len(loaded) > 15 else "")
        report.append("MISSING TAG | {} {} tag not found in the project: '{}' | "
                      "loaded tags of that category: {}".format(
                          label, kind, "' or '".join(names), shown or "(none)"))

    if not run:
        report.insert(0, "Summary ({}): PREVIEW ONLY - no tags were created. Views "
                         "matched: {}. Set Run to True to create the tags."
                      .format(VERSION, len(views)))
        return report

    total_jobs = sum(len(p[2]) for p in plans)
    total_stale = sum(len(p[3]) for p in plans)
    if total_jobs == 0 and total_stale == 0:
        report.insert(0, "Summary ({}): WRITE MODE - nothing to create. Views "
                         "matched: {}".format(VERSION, len(views)))
        return report

    used = {}
    for _, _, jobs, _ in plans:
        for job in jobs:
            used[eid_int(job[3].Id)] = job[3]

    done_lines = []
    total_made, total_failed = 0, 0
    TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        activated = False
        for sym in used.values():
            try:
                if not sym.IsActive:
                    sym.Activate()
                    activated = True
            except Exception:
                pass
        if activated:
            doc.Regenerate()

        for name, v, jobs, stale in plans:
            if not jobs and not stale:
                continue
            deleted = 0
            if stale:
                ids = List[ElementId]()
                for t in stale:
                    ids.Add(t.Id)
                try:
                    doc.Delete(ids)
                    deleted = len(stale)
                except Exception as ex:
                    done_lines.append("WARNING | {} | old tags could not be "
                                      "deleted: {}".format(name, ex))
            made, failed, first_error, not_freed = create_tags(v, jobs)
            total_made += made
            total_failed += failed
            line = "DONE | {} | created {} tag(s), failed {}".format(
                name, made, failed)
            if deleted:
                line += ", deleted {} old tag(s)".format(deleted)
            if not_freed:
                line += (" | NOTE: {} leader(s) stayed attached (free end not "
                         "available)".format(not_freed))
            if first_error:
                line += " | first error: " + first_error
            done_lines.append(line)
    finally:
        TransactionManager.Instance.TransactionTaskDone()

    report.insert(0, "Summary ({}): WRITE MODE - views matched: {}, tags created: "
                     "{}, failed: {}".format(VERSION, len(views), total_made,
                                             total_failed))
    return report + done_lines


# ------------------------------------------------------------------
# Run, and also save the report to %USERPROFILE%\fcu_script03_report.txt
# ------------------------------------------------------------------
try:
    _result = main()
except Exception:
    _result = ["SCRIPT ERROR:", traceback.format_exc()]

try:
    _path = os.path.join(os.path.expanduser("~"), "fcu_script03_report.txt")
    with open(_path, "w", encoding="utf-8") as _f:
        _f.write("\n".join(str(x) for x in _result))
    _result.append("Report saved to: " + _path)
except Exception:
    pass

OUT = _result
