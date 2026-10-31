# fcu_isolated_3d_views.py  (v3.4)
# Dynamo for Revit - Python Script node, CPython3 engine
# Author: Aren  |  License: MIT (see LICENSE in the repo root)
# Creates one isolated, section-boxed, locked 3D view per matched equipment.
#
# Version history
#   v3.4  pipe categories (incl. pipe insulation) turned off in the view;
#         leftovers still visible after isolating are hidden
#   v3.3  report is also saved to %USERPROFILE%\fcu_script01_report.txt
#   v3.2  walk can pass through named equipment (for example an FCU plenum)
#   v3.1  duct walk moved into the shared common block; connector notes
#   v3.0  options string: style, transparency, scale, direction, existing
#
# Inputs:
#   IN[0] filter text ("contains" match)              e.g. FCU-L0-016.1
#   IN[1] view name prefix (may be empty)             e.g. 3D
#   IN[2] section box margin in mm                    e.g. 300
#   IN[3] max hops (safety limit for the duct walk)   e.g. 100
#   IN[4] run (True/False)
#   IN[5] parameter to filter on  (optional)          default Mark
#   IN[6] parameter to name views (optional)          default Mark
#   IN[7] options (optional): "key=value; key=value"
#           style=edges|shaded|wireframe|hlr   (default edges)
#           transparency=0..90                 (default 0 = opaque)
#           scale=1 or more                    (default 50)
#           direction=se|sw|ne|nw              (default se)
#           existing=skip|replace              (default skip)
#           through=word, word                 (default fcu-pl, plenum)
#             Mechanical Equipment whose family name contains one of these
#             words is treated as part of the duct network (for example a plenum)
# Special parameter values: @type, @family, @familytype

import os
import traceback
import clr

clr.AddReference('RevitAPI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    FilteredElementCollector, BuiltInCategory, BuiltInParameter, ElementId,
    Element, FamilyInstance, MEPCurve, Domain, View3D, ViewFamilyType,
    ViewFamily, ViewDetailLevel, ViewOrientation3D, BoundingBoxXYZ, XYZ,
    DisplayStyle, OverrideGraphicSettings, TemporaryViewMode, LocationCurve
)
from System.Collections.Generic import List
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

doc = DocumentManager.Instance.CurrentDBDocument


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
DEFAULT_PARAM = "Mark"

DEFAULT_OPTIONS = {
    "style": "edges",
    "transparency": "0",
    "scale": "50",
    "direction": "se",
    "existing": "skip",
    "through": "fcu-pl, plenum",
}

# option value -> DisplayStyle member name
STYLE_NAMES = {
    "shaded": "Shading",
    "edges": "ShadingWithEdges",
    "wireframe": "Wireframe",
    "hlr": "HLR",
}

# camera corner -> (forward x, forward y); forward z is always -1 (looking down)
DIRECTIONS = {
    "se": (-1, 1),
    "sw": (1, 1),
    "ne": (-1, -1),
    "nw": (1, -1),
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
    """Lowercase and treat '.' like '-' so FCU.L0.016 == FCU-L0-016."""
    return str(text).strip().lower().replace('.', '-')


def to_int(text):
    try:
        return int(str(text).strip())
    except Exception:
        return None


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
prefix       = inp(1, "")
offset_ft    = float(IN[2]) / 304.8      # mm -> feet (Revit works in feet)
max_hops     = int(IN[3])
run          = bool(IN[4])
FILTER_PARAM = inp(5, DEFAULT_PARAM)
NAME_PARAM   = inp(6, DEFAULT_PARAM)

OPTS, UNKNOWN_OPTS = parse_options(inp(7, ""))
STYLE         = OPTS["style"].lower()
DIRECTION     = OPTS["direction"].lower()
EXISTING_MODE = OPTS["existing"].lower()
TRANSPARENCY  = to_int(OPTS["transparency"])
SCALE         = to_int(OPTS["scale"])
THROUGH_NAMES = tuple(t.strip().lower() for t in OPTS["through"].split(",")
                      if t.strip())

# Categories made transparent when transparency > 0 (not the FCU itself)
TRANSPARENT_CATS = [
    BuiltInCategory.OST_DuctCurves,
    BuiltInCategory.OST_FlexDuctCurves,
    BuiltInCategory.OST_DuctFitting,
    BuiltInCategory.OST_DuctAccessory,
    BuiltInCategory.OST_DuctTerminal,
    BuiltInCategory.OST_DuctInsulations,
    BuiltInCategory.OST_DuctLinings,
]

BAD_NAME_CHARS = '{}[]|;<>?`~\\:'


def option_errors():
    """Problems with the options input. Empty list means all good."""
    errs = []
    for u in UNKNOWN_OPTS:
        errs.append("Unknown option '{}'. Allowed keys: style, transparency, "
                    "scale, direction, existing, through.".format(u))
    if STYLE not in STYLE_NAMES:
        errs.append("style must be one of: {} (got '{}')."
                    .format(", ".join(sorted(STYLE_NAMES)), STYLE))
    if DIRECTION not in DIRECTIONS:
        errs.append("direction must be one of: {} (got '{}')."
                    .format(", ".join(sorted(DIRECTIONS)), DIRECTION))
    if EXISTING_MODE not in ("skip", "replace"):
        errs.append("existing must be skip or replace (got '{}')."
                    .format(EXISTING_MODE))
    if TRANSPARENCY is None or not (0 <= TRANSPARENCY <= 90):
        errs.append("transparency must be a whole number from 0 to 90 (got '{}')."
                    .format(OPTS["transparency"]))
    if SCALE is None or SCALE < 1:
        errs.append("scale must be a whole number of 1 or more (got '{}')."
                    .format(OPTS["scale"]))
    return errs


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def get_name(el):
    """
    Read an element's name.
    Plain el.Name can raise 'TypeError: property cannot be read' in the
    CPython3 engine, so read it through the base class first, and fall back
    to the built-in name parameters.
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


def set_name(el, name):
    """Rename a view, with the same CPython3 workaround and a fallback."""
    try:
        Element.Name.__set__(el, name)
    except Exception as ex1:
        p = el.get_Parameter(BuiltInParameter.VIEW_NAME)
        if p is None or p.IsReadOnly:
            raise ex1
        if not p.Set(name):
            raise Exception("could not set view name '{}'".format(name))


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


def family_label(el):
    """Family name of a placed element, for report notes."""
    try:
        return str(el.Symbol.FamilyName)
    except Exception:
        return "?"


def clean_name(text):
    return "".join("-" if ch in BAD_NAME_CHARS else ch for ch in text)


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


def ids_text(ids):
    shown = ", ".join(str(i) for i in ids[:300])
    return shown + (" ..." if len(ids) > 300 else "")


def find_targets():
    """
    Returns (matched, undesignated, unnamed).
    matched:      list of (element, naming_value) whose filter value contains
                  the filter text.
    undesignated: ids of FCU-looking equipment with no filter value.
    unnamed:      ids of matches that have no naming value (cannot be named).
    """
    matched = []
    undesignated = []
    unnamed = []
    col = (FilteredElementCollector(doc)
           .OfCategory(BuiltInCategory.OST_MechanicalEquipment)
           .WhereElementIsNotElementType())
    for el in col:
        if not isinstance(el, FamilyInstance):
            continue
        fval = get_param_value(el, FILTER_PARAM)
        if fval:
            if filter_text in norm(fval):
                nval = get_param_value(el, NAME_PARAM)
                if nval:
                    matched.append((el, nval))
                else:
                    unnamed.append(eid_int(el.Id))
        else:
            etype = doc.GetElement(el.GetTypeId())
            if etype is None:
                continue
            names = (get_family_name(etype) + " " + get_name(etype)).lower()
            if "fcu" in names and not is_pass_through(el, THROUGH_NAMES):
                undesignated.append(eid_int(el.Id))
    matched.sort(key=lambda t: t[1])
    return matched, undesignated, unnamed


def duct_connector_counts(el):
    """(duct connectors, how many of them are not connected)."""
    total, open_count = 0, 0
    for c in get_connectors(el):
        try:
            if c.Domain != Domain.DomainHvac:
                continue
        except Exception:
            continue
        total += 1
        try:
            if not c.IsConnected:
                open_count += 1
        except Exception:
            pass
    return total, open_count


def build_insulation_map():
    """host element id -> list of insulation / lining element ids."""
    m = {}
    for bic in (BuiltInCategory.OST_DuctInsulations,
                BuiltInCategory.OST_DuctLinings):
        col = (FilteredElementCollector(doc)
               .OfCategory(bic).WhereElementIsNotElementType())
        for item in col:
            try:
                host = eid_int(item.HostElementId)
            except Exception:
                continue
            m.setdefault(host, []).append(item.Id)
    return m


def union_bbox(elements):
    mn = [1e30, 1e30, 1e30]
    mx = [-1e30, -1e30, -1e30]
    ok = False
    for e in elements:
        bb = e.get_BoundingBox(None)
        if bb is None:
            continue
        ok = True
        lo = (bb.Min.X, bb.Min.Y, bb.Min.Z)
        hi = (bb.Max.X, bb.Max.Y, bb.Max.Z)
        for i in range(3):
            mn[i] = min(mn[i], lo[i])
            mx[i] = max(mx[i], hi[i])
    return (mn, mx) if ok else None


def iso_orientation(center):
    """
    Isometric look direction for the chosen corner. The up vector (fx, fy, 2)
    is perpendicular to forward (fx, fy, -1) because fx*fx + fy*fy - 2 = 0.
    """
    fx, fy = DIRECTIONS[DIRECTION]
    fwd = XYZ(fx, fy, -1).Normalize()
    up = XYZ(fx, fy, 2).Normalize()
    eye = center.Subtract(fwd.Multiply(100.0))
    return ViewOrientation3D(eye, up, fwd)


def apply_style(view):
    """Set the display style. Returns False if this Revit lacks that style."""
    member = getattr(DisplayStyle, STYLE_NAMES[STYLE], None)
    if member is None:
        return False
    view.DisplayStyle = member
    return True


def apply_transparency(view, pct):
    """Make the duct-system categories see-through. Returns categories done."""
    if pct <= 0:
        return 0
    ogs = OverrideGraphicSettings()
    ogs.SetSurfaceTransparency(pct)
    done = 0
    for bic in TRANSPARENT_CATS:
        try:
            view.SetCategoryOverrides(ElementId(bic), ogs)
            done += 1
        except Exception:
            pass
    return done


def hide_all_except(view, keep_set):
    """Fallback: permanently hide every element in the view that is not kept."""
    to_hide = List[ElementId]()
    col = FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
    for e in col:
        if eid_int(e.Id) in keep_set:
            continue
        try:
            if e.CanBeHidden(view):
                to_hide.Add(e.Id)
        except Exception:
            pass
    if to_hide.Count > 0:
        view.HideElements(to_hide)


def isolate_permanently(view, keep_ids, keep_set):
    """
    Show only the kept elements, permanently.
    First choice: temporary isolate converted to permanent.
    Fallback: hide everything else one by one.
    Returns None on the first method, or a note if the fallback was used.
    """
    try:
        view.IsolateElementsTemporary(keep_ids)
        view.ConvertTemporaryHideIsolateToPermanent()
        return None
    except Exception as ex1:
        try:
            view.DisableTemporaryViewMode(TemporaryViewMode.TemporaryHideIsolate)
        except Exception:
            pass
        hide_all_except(view, keep_set)
        return "used fallback hiding because isolate failed: {}".format(ex1)


PIPE_CATS = [
    BuiltInCategory.OST_PipeInsulations,
    BuiltInCategory.OST_PipeCurves,
    BuiltInCategory.OST_FlexPipeCurves,
    BuiltInCategory.OST_PipeFitting,
    BuiltInCategory.OST_PipeAccessory,
]


def hide_pipe_categories(view):
    """Turn off pipe categories in the view (a duct system has no pipes)."""
    for bic in PIPE_CATS:
        try:
            cid = ElementId(bic)
            if view.CanCategoryBeHidden(cid):
                view.SetCategoryHidden(cid, True)
        except Exception:
            pass


def sweep_leftovers(view, keep_set):
    """
    After isolating, hide anything still visible that is not kept
    (e.g. pipe insulation that Revit keeps showing). Returns how many.
    """
    left = List[ElementId]()
    col = FilteredElementCollector(doc, view.Id).WhereElementIsNotElementType()
    for e in col:
        if eid_int(e.Id) in keep_set:
            continue
        try:
            if e.CanBeHidden(view):
                left.Add(e.Id)
        except Exception:
            pass
    if left.Count > 0:
        try:
            view.HideElements(left)
        except Exception:
            return -left.Count
    return left.Count


def verify_view(view, keep):
    """Short problem notes for kept elements that may not be visible."""
    problems = []
    for e in keep:
        eid = eid_int(e.Id)
        try:
            if e.IsHidden(view):
                problems.append("hidden:{}".format(eid))
                continue
            cat = e.Category
            if cat is not None and view.GetCategoryHidden(cat.Id):
                problems.append("category hidden:{}".format(eid))
                continue
        except Exception:
            pass
        try:
            if e.get_BoundingBox(None) is None:
                problems.append("no bounding box:{}".format(eid))
        except Exception:
            pass
    return problems


def process_target(start, name_value, vft_id, existing, created, ins_map):
    base = clean_name(name_value)
    name = "{}_{}".format(prefix, base) if prefix else base
    key = name.lower()

    if key in created:
        return "SKIP | duplicate value in this run, view already created | " + name

    if key in existing and EXISTING_MODE == "skip":
        return "SKIP | view exists (use existing=replace to rebuild it) | " + name

    # Work everything out first, so nothing is deleted unless a new view can be built
    roots = element_neighbors(start, THROUGH_NAMES)
    net, _depth, _parent, _skipped, truncated, _overlaps = walk_side(
        roots, set(), max_hops, 0.0, THROUGH_NAMES)
    keep = [start] + net

    notes = []
    dc, dopen = duct_connector_counts(start)
    if dc != 2:
        notes.append("{} duct connector(s) found, FCUs normally have 2".format(dc))
    if dopen:
        notes.append("{} duct connector(s) not connected".format(dopen))

    through_found = []
    for e in net:
        cat = e.Category
        if cat is not None and eid_int(cat.Id) == EQUIPMENT_CAT:
            through_found.append(family_label(e))
    if through_found:
        notes.append("passed through {} equipment piece(s): {}".format(
            len(through_found), ", ".join(through_found)))

    keep_ids = List[ElementId]()
    keep_set = set()
    for e in keep:
        for i in [e.Id] + ins_map.get(eid_int(e.Id), []):
            if eid_int(i) not in keep_set:
                keep_set.add(eid_int(i))
                keep_ids.Add(i)

    bounds = union_bbox(keep)
    if bounds is None:
        return "FAIL | no geometry found | " + name
    mn, mx = bounds

    replaced = False
    if key in existing:                         # existing=replace
        old_view = existing[key]
        try:
            doc.Delete(old_view.Id)
            del existing[key]
            replaced = True
        except Exception:
            # Revit refuses to delete this view (often because it is open).
            # Rename it out of the way so the new view can take the name.
            try:
                set_name(old_view, name + "_OLD")
                del existing[key]
                replaced = True
                notes.append("old view could not be deleted; renamed to '{}_OLD' "
                             "- delete it by hand".format(name))
            except Exception as ex:
                return ("SKIP | could not delete or rename the existing view "
                        "({}) | {}".format(ex, name))

    problems = []
    view = View3D.CreateIsometric(doc, vft_id)
    try:
        set_name(view, name)

        try:
            view.Scale = SCALE
        except Exception as ex:
            notes.append("scale not set: {}".format(ex))

        center = XYZ((mn[0] + mx[0]) / 2.0,
                     (mn[1] + mx[1]) / 2.0,
                     (mn[2] + mx[2]) / 2.0)
        view.SetOrientation(iso_orientation(center))

        box = BoundingBoxXYZ()
        box.Min = XYZ(mn[0] - offset_ft, mn[1] - offset_ft, mn[2] - offset_ft)
        box.Max = XYZ(mx[0] + offset_ft, mx[1] + offset_ft, mx[2] + offset_ft)
        view.SetSectionBox(box)
        view.IsSectionBoxActive = True

        view.DetailLevel = ViewDetailLevel.Fine
        if not apply_style(view):
            notes.append("display style '{}' not available".format(STYLE))

        fallback_note = isolate_permanently(view, keep_ids, keep_set)
        if fallback_note:
            notes.append(fallback_note)
        hide_pipe_categories(view)
        swept = sweep_leftovers(view, keep_set)
        if swept > 0:
            notes.append("hid {} leftover element(s) still visible after "
                         "isolating".format(swept))
        elif swept < 0:
            notes.append("CHECK: {} leftover element(s) could not be hidden"
                         .format(-swept))

        if TRANSPARENCY > 0:
            done = apply_transparency(view, TRANSPARENCY)
            if done == 0:
                notes.append("transparency could not be applied")

        problems = verify_view(view, keep)
        view.SaveOrientationAndLock()
    except Exception as ex:
        try:
            doc.Delete(view.Id)         # do not leave a half-built view behind
        except Exception:
            pass
        return "FAIL | {} | {}".format(name, ex)

    created.add(key)
    msg = "OK | {} | {} elements".format(name, keep_ids.Count)
    if replaced:
        msg += " | replaced the existing view (re-run the tagging script for it)"
    if truncated:
        msg += " | WARNING: hop limit reached, network may be incomplete"
    if problems:
        msg += " | CHECK: {} element(s) may not be visible ({})".format(
            len(problems), "; ".join(problems[:5]))
    for n in notes:
        msg += " | NOTE: " + n
    return msg


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    if not run:
        return ["Run toggle is False - nothing was changed."]

    errs = option_errors()
    if errs:
        return ["OPTIONS ERROR | " + e for e in errs]

    if not filter_text:
        return ["Filter text is empty - refusing to run on all equipment."]

    targets, undesignated, unnamed = find_targets()
    report = []
    if undesignated:
        report.append("WARNING: {} FCU-looking element(s) have no value in '{}' "
                      "(ids: {})".format(len(undesignated), FILTER_PARAM,
                                         ids_text(undesignated)))
    if unnamed:
        report.append("WARNING: {} match(es) have no value in '{}' so they cannot "
                      "be named and were skipped (ids: {})"
                      .format(len(unnamed), NAME_PARAM, ids_text(unnamed)))

    if not targets:
        report.append("No equipment with '{}' containing '{}'."
                      .format(FILTER_PARAM, filter_text))
        return report

    vfts = [v for v in FilteredElementCollector(doc).OfClass(ViewFamilyType)
            if v.ViewFamily == ViewFamily.ThreeDimensional]
    if not vfts:
        report.append("No 3D view family type found in the project.")
        return report
    vft_id = vfts[0].Id

    existing = {}
    for v in FilteredElementCollector(doc).OfClass(View3D):
        if v.IsTemplate:
            continue
        existing[get_name(v).lower()] = v
    created = set()

    ins_map = build_insulation_map()

    results = []
    TransactionManager.Instance.EnsureInTransaction(doc)
    try:
        for el, nval in targets:
            try:
                results.append(process_target(el, nval, vft_id, existing,
                                              created, ins_map))
            except Exception as ex:
                results.append("FAIL | {} | {}".format(nval, ex))
    finally:
        TransactionManager.Instance.TransactionTaskDone()

    ok = sum(1 for r in results if r.startswith("OK"))
    skipped = sum(1 for r in results if r.startswith("SKIP"))
    failed = sum(1 for r in results if r.startswith("FAIL"))
    header = ("Summary (v3.4c): matched {} | created {} | skipped {} | failed {} "
              "(filter '{}' contains '{}', named by '{}')"
              .format(len(targets), ok, skipped, failed,
                      FILTER_PARAM, filter_text, NAME_PARAM))
    settings = ("Settings: style={}, transparency={}, scale=1:{}, direction={}, "
                "existing={}, through={}".format(
                    STYLE, TRANSPARENCY, SCALE, DIRECTION.upper(), EXISTING_MODE,
                    ", ".join(THROUGH_NAMES) if THROUGH_NAMES else "(none)"))
    return [header, settings] + report + results


# ------------------------------------------------------------------
# Run, and also save the report to %USERPROFILE%\fcu_script01_report.txt
# ------------------------------------------------------------------
try:
    _result = main()
except Exception:
    _result = ["SCRIPT ERROR:", traceback.format_exc()]

try:
    _path = os.path.join(os.path.expanduser("~"), "fcu_script01_report.txt")
    with open(_path, "w", encoding="utf-8") as _f:
        _f.write("\n".join(str(x) for x in _result))
    _result.append("Report saved to: " + _path)
except Exception:
    pass

OUT = _result
