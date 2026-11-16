# fcu_element_numbering.py  (v1.3)
# Dynamo for Revit - Python Script node, CPython3 engine
# Author: Aren  |  License: MIT (see LICENSE in the repo root)
# Numbers the duct-side elements of each FCU by their distance along the route
# from the return air terminal, so the LONGEST route reaches the biggest number
# (needed for pressure loss calculation). The FCU itself is not numbered.
#
#   return side element:  number = R - depth + 1   (R = longest return route)
#   supply side element:  number = R + depth
#   depth = distance from the FCU in COUNTED elements (the first one = 1)
#
# Rigid ducts at or below the minimum length are too short to cause a pressure
# drop. They are walked through (the network continues past them) but not
# counted, and they are written as 0 (= not numbered).
#
# Version history
#   v1.3  duct system type read as a number (works in CPython3); plenum-type
#         equipment is walked through and numbered; shared common block v2;
#         options string; Run toggle; report saved to a file
#   v1.2  short ducts not counted
#
# Inputs:
#   IN[0] filter text ("contains" match)               e.g. FCU-LI-L0-095
#   IN[1] parameter to write numbers into              default DLB_ELEMENT Number
#   IN[2] minimum duct length in mm (0 = rule off)     e.g. 100
#   IN[3] max hops (safety limit for the duct walk)    e.g. 100
#   IN[4] run (False = preview only, True = write)
#   IN[5] parameter to filter on                       default DLB-Designation
#   IN[6] options: "key=value; key=value"
#           branches=depth|zero        (default depth)
#             depth = branch elements get the distance-based number
#             zero  = branch elements get 0 (only the longest route is numbered)
#           through=word, word         (default fcu-pl, plenum)
#             Mechanical Equipment whose family name contains one of these
#             words is part of the route (for example a plenum) and is numbered
#           detail=yes|no              (default yes) list every numbered element
# Special filter parameter values: @type, @family, @familytype

import os
import traceback
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter,
    Element, FamilyInstance, MEPCurve, Domain, StorageType, LocationCurve
)
from System import Int32
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

doc = DocumentManager.Instance.CurrentDBDocument

VERSION = "v1.3"


# ---- COMMON BLOCK v2 (begin) -------------------------------------------
# Identical in scripts 01, 02, 04 and 05. Change it in common/network_walk.py
# first, then paste it into every script. It needs only the imports above
# (BuiltInCategory, BuiltInParameter, MEPCurve, FamilyInstance, Domain,
# LocationCurve) and no global variables.
# v2: equipment whose family name contains one of `through_names` (for example
# a plenum) is walked through as part of the network.

# Categories that count as part of an equipment's duct network
ALLOWED_CATS = {
    int(BuiltInCategory.OST_DuctCurves),
    int(BuiltInCategory.OST_FlexDuctCurves),
    int(BuiltInCategory.OST_DuctFitting),
    int(BuiltInCategory.OST_DuctAccessory),
    int(BuiltInCategory.OST_DuctTerminal),
}

DUCT_CAT = int(BuiltInCategory.OST_DuctCurves)
EQUIPMENT_CAT = int(BuiltInCategory.OST_MechanicalEquipment)


def eid_int(eid):
    """ElementId -> int. Works on old and new Revit versions."""
    try:
        return eid.Value            # Revit 2024+
    except AttributeError:
        return eid.IntegerValue     # older versions


def family_name_lower(el):
    """Lowercase family name of a placed family, or '' if unavailable."""
    try:
        return str(el.Symbol.FamilyName).lower()
    except Exception:
        return ""


def is_pass_through(el, through_names):
    """True for mechanical equipment whose family name contains a through word."""
    if not through_names:
        return False
    cat = el.Category
    if cat is None or eid_int(cat.Id) != EQUIPMENT_CAT:
        return False
    fam = family_name_lower(el)
    for word in through_names:
        if word in fam:
            return True
    return False


def is_allowed(el, through_names=()):
    cat = el.Category
    if cat is None:
        return False
    if eid_int(cat.Id) in ALLOWED_CATS:
        return True
    return is_pass_through(el, through_names)


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


def is_small_duct(el, min_len_ft):
    """True for a rigid duct at or below the minimum length (no pressure drop)."""
    if min_len_ft <= 0:
        return False
    cat = el.Category
    if cat is None or eid_int(cat.Id) != DUCT_CAT:
        return False
    length = duct_length_ft(el)
    return length is not None and length <= min_len_ft + 1e-6


def get_connectors(el):
    """Return all connectors of a duct curve or a family instance."""
    try:
        if isinstance(el, MEPCurve):
            return list(el.ConnectorManager.Connectors)
        if isinstance(el, FamilyInstance):
            mep = el.MEPModel
            if mep is not None and mep.ConnectorManager is not None:
                return list(mep.ConnectorManager.Connectors)
    except Exception:
        pass
    return []


def neighbors_of_connector(c, self_id, through_names):
    """Allowed elements joined to connector c (never the owner itself)."""
    out = []
    try:
        if not c.IsConnected:
            return out
        for ref in c.AllRefs:
            o = ref.Owner
            if o is None:
                continue
            if eid_int(o.Id) == self_id:
                continue
            if not isinstance(o, (MEPCurve, FamilyInstance)):
                continue            # skips the logical system element
            if not is_allowed(o, through_names):
                continue            # skips other equipment
            out.append(o)
    except Exception:
        pass
    return out


def element_neighbors(el, through_names):
    """Allowed elements reachable from el through its duct (HVAC) connectors."""
    sid = eid_int(el.Id)
    out = []
    for c in get_connectors(el):
        try:
            if c.Domain != Domain.DomainHvac:
                continue
        except Exception:
            continue
        out.extend(neighbors_of_connector(c, sid, through_names))
    return out


def walk_side(roots, blocked, max_hops, min_len_ft, through_names):
    """
    Breadth-first walk outward from the elements touching an equipment connector.
    roots:         elements touching the connector(s) of the side being walked.
    blocked:       element ids that belong to another side (never entered).
    max_hops:      safety limit on how far the walk goes.
    min_len_ft:    short-duct threshold in feet (0 = rule off).
    through_names: lowercase words; equipment whose family name contains one
                   of them is walked through (for example a plenum).
    Returns (order, depth, parent, skipped, truncated, overlaps).
      order:   elements in discovery order (short ducts included)
      depth:   id -> distance in COUNTED elements; a short duct has the same
               depth as the element it was reached from
      parent:  id -> id of the element it was reached from (None for roots)
      skipped: ids of short ducts (walked through, never counted)
    """
    depth, parent, order = {}, {}, []
    skipped = set()
    overlaps = 0
    frontier = []
    for r in roots:
        rid = eid_int(r.Id)
        if rid in depth:
            continue
        if rid in blocked:
            overlaps += 1
            continue
        small = is_small_duct(r, min_len_ft)
        if small:
            skipped.add(rid)
        depth[rid] = 0 if small else 1
        parent[rid] = None
        order.append(r)
        frontier.append(r)

    hops = 1
    while frontier and hops < max_hops:
        nxt = []
        for el in frontier:
            eid = eid_int(el.Id)
            for o in element_neighbors(el, through_names):
                oid = eid_int(o.Id)
                if oid in depth:
                    continue
                if oid in blocked:
                    overlaps += 1
                    continue
                small = is_small_duct(o, min_len_ft)
                if small:
                    skipped.add(oid)
                depth[oid] = depth[eid] + (0 if small else 1)
                parent[oid] = eid
                order.append(o)
                nxt.append(o)
        frontier = nxt
        hops += 1
    return order, depth, parent, skipped, len(frontier) > 0, overlaps

# ---- COMMON BLOCK v2 (end) ---------------------------------------------


# ------------------------------------------------------------------
# Inputs
# ------------------------------------------------------------------
DEFAULT_FILTER_PARAM = "DLB-Designation"
DEFAULT_NUMBER_PARAM = "DLB_ELEMENT Number"

DEFAULT_OPTIONS = {
    "branches": "depth",
    "through": "fcu-pl, plenum",
    "detail": "yes",
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
    """Lowercase and treat '.' like '-' so FCU.LI.L0.016 == FCU-LI-L0-016."""
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


filter_text  = norm(inp(0, ""))
NUM_PARAM    = inp(1, DEFAULT_NUMBER_PARAM)
min_len_mm   = float(inp(2, "100"))
min_len_ft   = min_len_mm / 304.8       # Revit works in feet
max_hops     = int(float(inp(3, "100")))
run          = bool(IN[4])
FILTER_PARAM = inp(5, DEFAULT_FILTER_PARAM)

OPTS, UNKNOWN_OPTS = parse_options(inp(6, ""))
BRANCH_MODE   = OPTS["branches"].lower()
DETAIL        = OPTS["detail"].lower()
THROUGH_NAMES = tuple(t.strip().lower() for t in OPTS["through"].split(",")
                      if t.strip())

# Short labels used in the report
CAT_ABBR = {
    int(BuiltInCategory.OST_DuctTerminal): "AT",
    int(BuiltInCategory.OST_DuctCurves): "D",
    int(BuiltInCategory.OST_FlexDuctCurves): "FX",
    int(BuiltInCategory.OST_DuctFitting): "F",
    int(BuiltInCategory.OST_DuctAccessory): "ACC",
    EQUIPMENT_CAT: "PL",
}

# DuctSystemType arrives as a plain number in the CPython3 engine
SYS_SUPPLY, SYS_RETURN, SYS_EXHAUST = 1, 2, 3
DIR_IN, DIR_OUT = 1, 2


def option_errors():
    """Problems with the inputs. Empty list means all good."""
    errs = []
    for u in UNKNOWN_OPTS:
        errs.append("Unknown option '{}'. Allowed keys: branches, through, "
                    "detail.".format(u))
    if BRANCH_MODE not in ("depth", "zero"):
        errs.append("branches must be depth or zero (got '{}').".format(BRANCH_MODE))
    if DETAIL not in ("yes", "no"):
        errs.append("detail must be yes or no (got '{}').".format(DETAIL))
    if min_len_mm < 0:
        errs.append("minimum duct length cannot be negative.")
    if max_hops < 1:
        errs.append("max hops must be 1 or more.")
    return errs


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
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


def type_label(el):
    """'family : type' of an element, for the report."""
    try:
        etype = doc.GetElement(el.GetTypeId())
    except Exception:
        etype = None
    if etype is None:
        return "?"
    fam = get_family_name(etype)
    typ = get_name(etype)
    return (fam + " : " + typ) if fam else (typ or "?")


def abbr(el):
    cat = el.Category
    if cat is None:
        return "?"
    return CAT_ABBR.get(eid_int(cat.Id), "?")


def lengths_text(elements):
    """Lengths in mm of up to 10 elements, for the report."""
    vals = []
    for e in elements[:10]:
        L = duct_length_ft(e)
        vals.append("{:.0f}".format(L * 304.8) if L is not None else "?")
    return ", ".join(vals) + (" ..." if len(elements) > 10 else "")


def get_param_value(el, spec):
    """
    Text value of the parameter called `spec`, or None if missing/empty.
    Looks at the placed element first, then at its type.
    Special specs: @type, @family, @familytype.
    """
    key = spec.strip().lower()
    etype = doc.GetElement(el.GetTypeId())

    if key in ("@type", "@family", "@familytype"):
        if etype is None:
            return None
        fam = get_family_name(etype)
        typ = get_name(etype)
        if key == "@type":
            val = typ
        elif key == "@family":
            val = fam
        else:
            val = fam + " - " + typ
        val = val.strip()
        return val if val else None

    p = el.LookupParameter(spec.strip())
    if p is None and etype is not None:
        p = etype.LookupParameter(spec.strip())
    if p is None:
        return None
    val = p.AsString()
    if not val:
        val = p.AsValueString()
    return val.strip() if val else None


def find_targets():
    """(element, filter value) for mechanical equipment matching the filter."""
    matched = []
    col = (FilteredElementCollector(doc)
           .OfCategory(BuiltInCategory.OST_MechanicalEquipment)
           .WhereElementIsNotElementType())
    for el in col:
        if not isinstance(el, FamilyInstance):
            continue
        if is_pass_through(el, THROUGH_NAMES):
            continue                # a plenum is part of a route, never a target
        val = get_param_value(el, FILTER_PARAM)
        if val and filter_text in norm(val):
            matched.append((el, val))
    matched.sort(key=lambda t: t[1])
    return matched


def as_number(value):
    """Enum or number -> int, or None. Enums are plain ints in CPython3."""
    try:
        return int(value)
    except Exception:
        pass
    text = str(value).lower()
    if "supply" in text:
        return SYS_SUPPLY
    if "return" in text:
        return SYS_RETURN
    if "exhaust" in text:
        return SYS_EXHAUST
    return None


def find_fcu_connectors(fcu):
    """
    Returns (return_connectors, supply_connectors, note).
    First choice: the duct system type of each duct connector
    (2 = Return Air, 1 = Supply Air). If that gives nothing, Exhaust Air
    counts as return; last resort is the flow direction (In = return side,
    Out = supply side).
    """
    hvac = []
    for c in get_connectors(fcu):
        try:
            if c.Domain != Domain.DomainHvac:
                continue
        except Exception:
            continue
        hvac.append(c)

    ret, sup, exh, seen = [], [], [], []
    for c in hvac:
        try:
            st = as_number(c.DuctSystemType)
        except Exception:
            st = None
        seen.append("?" if st is None else str(st))
        if st == SYS_RETURN:
            ret.append(c)
        elif st == SYS_SUPPLY:
            sup.append(c)
        elif st == SYS_EXHAUST:
            exh.append(c)

    note = ""
    if not ret and exh:
        ret = exh
        note = "no Return Air connector, used the Exhaust Air connector as return"

    if not ret or not sup:
        ret2, sup2 = [], []
        for c in hvac:
            try:
                d = as_number(c.Direction)
            except Exception:
                d = None
            if d == DIR_IN:
                ret2.append(c)
            elif d == DIR_OUT:
                sup2.append(c)
        if ret2 and sup2:
            ret, sup = ret2, sup2
            note = ("system types not usable ({}), sides taken from the flow "
                    "direction (In = return, Out = supply)".format(", ".join(seen)))
        else:
            note = "duct connectors found: {} (system types: {})".format(
                len(hvac), ", ".join(seen) or "none")
    return ret, sup, note


def longest_route(order, depth, parent, skipped):
    """Counted elements along the deepest path, listed from the FCU outward."""
    counted = [e for e in order if eid_int(e.Id) not in skipped]
    if not counted:
        return []
    by_id = {}
    for e in order:
        by_id[eid_int(e.Id)] = e
    leaf = max(counted, key=lambda e: (depth[eid_int(e.Id)], -eid_int(e.Id)))
    route = []
    cur = eid_int(leaf.Id)
    while cur is not None:
        if cur not in skipped:
            route.append(by_id[cur])
        cur = parent[cur]
    route.reverse()
    return route


def route_string(ret_numbered, sup_route):
    parts = [abbr(e) for e in ret_numbered] + ["[FCU]"] + [abbr(e) for e in sup_route]
    return ">".join(parts)


def plan_fcu(fcu, label, assigned):
    """
    Work out the numbering for one FCU (read-only).
    Returns (report_lines, items, detail_lines)
    where items = [(label, element, number), ...].
    """
    ret_c, sup_c, note = find_fcu_connectors(fcu)
    if not ret_c or not sup_c:
        return (["FAIL | {} | needs a Return Air and a Supply Air duct connector | {}"
                 .format(label, note)], [], [])

    fid = eid_int(fcu.Id)
    ret_roots = []
    for c in ret_c:
        ret_roots.extend(neighbors_of_connector(c, fid, THROUGH_NAMES))
    sup_roots = []
    for c in sup_c:
        sup_roots.extend(neighbors_of_connector(c, fid, THROUGH_NAMES))
    if not ret_roots or not sup_roots:
        side = "Return Air" if not ret_roots else "Supply Air"
        return (["FAIL | {} | the {} connector is not connected to any duct, fitting, "
                 "accessory, air terminal or plenum".format(label, side)], [], [])

    r_order, r_depth, r_parent, r_skip, r_trunc, _ = walk_side(
        ret_roots, set(), max_hops, min_len_ft, THROUGH_NAMES)
    s_order, s_depth, s_parent, s_skip, s_trunc, s_overlaps = walk_side(
        sup_roots, set(r_depth.keys()), max_hops, min_len_ft, THROUGH_NAMES)

    r_route = longest_route(r_order, r_depth, r_parent, r_skip)   # FCU -> far end
    s_route = longest_route(s_order, s_depth, s_parent, s_skip)   # FCU -> far end
    r_numbered = list(reversed(r_route))                          # far end -> FCU
    main = r_numbered + s_route
    main_ids = set(eid_int(e.Id) for e in main)
    R = len(r_route)
    S = len(s_route)

    # (element, number, where) triples. The longest route always gets
    # R - depth + 1 (return) and R + depth (supply), so its far supply end is
    # R + S, the highest number. Branch elements follow the same distance
    # formula ("depth" mode) or get 0 ("zero" mode); none can exceed R + S.
    # Short ducts are never counted: they always get 0.
    triples = []
    for e in r_numbered:
        triples.append((e, R - r_depth[eid_int(e.Id)] + 1, "return, main route"))
    for e in s_route:
        triples.append((e, R + s_depth[eid_int(e.Id)], "supply, main route"))
    off_count = 0
    skipped_els = []
    for side, order, depth, skip in (("return", r_order, r_depth, r_skip),
                                     ("supply", s_order, s_depth, s_skip)):
        for e in order:
            eid = eid_int(e.Id)
            if eid in main_ids:
                continue
            if eid in skip:
                skipped_els.append(e)
                triples.append((e, 0, side + ", short duct"))
                continue
            off_count += 1
            if BRANCH_MODE == "depth":
                n = (R - depth[eid] + 1) if side == "return" else (R + depth[eid])
            else:
                n = 0
            triples.append((e, n, side + ", branch"))

    items = []
    conflicts = {}
    kept = []
    for el, n, where in triples:
        eid = eid_int(el.Id)
        if eid in assigned:
            other = assigned[eid]
            conflicts[other] = conflicts.get(other, 0) + 1
            continue
        assigned[eid] = label
        items.append((label, el, n))
        kept.append((el, n, where))

    mode_txt = ("numbered by distance from the FCU" if BRANCH_MODE == "depth"
                else "set to 0")
    short_txt = ""
    if skipped_els:
        short_txt = ", short ducts not counted: {} [mm: {}]".format(
            len(skipped_els), lengths_text(skipped_els))
    lines = ["PLAN | {} | longest route {} elements (return {} + supply {}), "
             "highest number {}, {} branch element(s) {}{} | {}"
             .format(label, len(main), R, S, R + S, off_count, mode_txt, short_txt,
                     route_string(r_numbered, s_route))]
    if note:
        lines.append("NOTE | {} | {}".format(label, note))
    through_found = [type_label(e) for e, _, _ in kept if abbr(e) == "PL"]
    if through_found:
        lines.append("NOTE | {} | numbered {} equipment piece(s) as route "
                     "element(s): {}".format(label, len(through_found),
                                             ", ".join(through_found)))
    if r_trunc or s_trunc:
        lines.append("WARNING | {} | hop limit reached on the {} side, network may be "
                     "incomplete".format(label, "return" if r_trunc else "supply"))
    if s_overlaps:
        lines.append("WARNING | {} | the return and supply sides touch each other "
                     "({} connection(s)), check for a loop".format(label, s_overlaps))
    for other in sorted(conflicts):
        lines.append("WARNING | {} | {} element(s) were already numbered under {}, "
                     "kept those numbers".format(label, conflicts[other], other))
    if not r_route or not s_route:
        lines.append("WARNING | {} | the {} side has no countable element (only "
                     "short ducts?)".format(label, "return" if not r_route else "supply"))
    if r_route and abbr(r_route[-1]) != "AT":
        lines.append("WARNING | {} | return route starts at '{}', not an air terminal "
                     "(open end?)".format(label, abbr(r_route[-1])))
    if s_route and abbr(s_route[-1]) != "AT":
        lines.append("WARNING | {} | supply route ends at '{}', not an air terminal "
                     "(open end?)".format(label, abbr(s_route[-1])))

    detail = []
    if DETAIL == "yes":
        for el, n, where in sorted(kept, key=lambda t: (t[1] == 0, t[1],
                                                        eid_int(t[0].Id))):
            detail.append("  {} | {:>3} | {:<3} | id {} | {} | {}".format(
                label, n, abbr(el), eid_int(el.Id), where, type_label(el)))
    return lines, items, detail


def write_number(el, value):
    """Write one number. Returns a short status word."""
    try:
        p = el.LookupParameter(NUM_PARAM)
        if p is None:
            return "param-not-found"
        if p.IsReadOnly:
            return "read-only"
        st = p.StorageType
        if st == StorageType.Integer:
            ok = p.Set(Int32(value))
        elif st == StorageType.String:
            ok = p.Set(str(value))
        else:
            return "wrong-type"
        return "ok" if ok else "set-failed"
    except Exception as ex:
        return "error: {}".format(ex)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    errs = option_errors()
    if errs:
        return ["INPUT ERROR | " + e for e in errs]

    if not filter_text:
        return ["Filter text is empty - refusing to run on all equipment."]

    fcus = find_targets()
    if not fcus:
        return ["No equipment with '{}' containing '{}'."
                .format(FILTER_PARAM, filter_text)]

    report = ["Settings ({}): branches={}, ducts <= {:.0f} mm not counted, "
              "through={}, max hops {}".format(
                  VERSION, BRANCH_MODE, min_len_mm,
                  ", ".join(THROUGH_NAMES) if THROUGH_NAMES else "(none)", max_hops),
              "Legend: AT air terminal, D duct, FX flex duct, F fitting, "
              "ACC accessory, PL plenum, ? other"]
    assigned = {}
    to_write = []
    details = []
    for fcu, label in fcus:
        try:
            lines, items, detail = plan_fcu(fcu, label, assigned)
        except Exception as ex:
            lines, items, detail = ["FAIL | {} | {}".format(label, ex)], [], []
        report.extend(lines)
        to_write.extend(items)
        details.extend(detail)

    failed = sum(1 for r in report if r.startswith("FAIL"))
    if details:
        report.append("Elements (FCU | number | kind | id | where | family : type), "
                      "0 = not numbered:")
        report.extend(details)

    if not run:
        report.insert(0, "Summary ({}): PREVIEW ONLY - nothing was written. FCUs "
                         "matched: {}, failed: {}. Set Run to True to write '{}'."
                      .format(VERSION, len(fcus), failed, NUM_PARAM))
        return report

    tally = {}
    if to_write:
        TransactionManager.Instance.EnsureInTransaction(doc)
        try:
            for label, el, n in to_write:
                status = write_number(el, n)
                t = tally.setdefault(label, {})
                t[status] = t.get(status, 0) + 1
        finally:
            TransactionManager.Instance.TransactionTaskDone()

    for label in sorted(tally):
        report.append("WRITE | {} | {}".format(
            label, ", ".join("{}={}".format(k, v)
                             for k, v in sorted(tally[label].items()))))
    report.insert(0, "Summary ({}): WRITE MODE - FCUs matched: {}, failed: {}, "
                     "numbers sent to '{}': {}"
                  .format(VERSION, len(fcus), failed, NUM_PARAM, len(to_write)))
    return report


# ------------------------------------------------------------------
# Run, and also save the report to %USERPROFILE%\fcu_script02_report.txt
# ------------------------------------------------------------------
try:
    _result = main()
except Exception:
    _result = ["SCRIPT ERROR:", traceback.format_exc()]

try:
    _path = os.path.join(os.path.expanduser("~"), "fcu_script02_report.txt")
    with open(_path, "w", encoding="utf-8") as _f:
        _f.write("\n".join(str(x) for x in _result))
    _result.append("Report saved to: " + _path)
except Exception:
    pass

OUT = _result
