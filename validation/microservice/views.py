import os
from django.shortcuts import render
from django.http import HttpResponse
import json
from shapely import ops, union_all,reverse
from shapely.geometry import Point, box, LineString
from shapely.ops import snap, unary_union, linemerge
import numbers
import numpy as np
import geopandas as gpd
import pandas as pd
import networkx as nx
from collections import defaultdict
import copy

def merge_simple_intersections(linegdf):

    nodes = []
    for line in linegdf.geometry:
        nodes.append(Point(line.coords[0]))
        nodes.append(Point(line.coords[-1]))

    nodes_gs = gpd.GeoSeries(nodes)
    unique_nodes = nodes_gs.drop_duplicates()


    streetsegments_geoseries = linegdf.geometry


    # Build spatial index for lines
    sindex = streetsegments_geoseries.sindex

    records = []
    for i, n in enumerate(unique_nodes):
        # Candidate line indices that might intersect
        possible_idxs = list(sindex.intersection(n.bounds))
        if not possible_idxs:
            records.append({
                "geometry": n,
                "n_intersections": 0,
                "line_ids": []
            })
            continue

        # Filter actual intersections
        intersects_mask = streetsegments_geoseries.iloc[possible_idxs].intersects(n)
        intersecting_ids = list(linegdf.iloc[possible_idxs][intersects_mask]["id"])

        records.append({
            "geometry": n,
            "n_intersections": len(intersecting_ids),
            "line_ids": intersecting_ids
        })

    # Return GeoDataFrame
    pointsandlines =  gpd.GeoDataFrame(records)


    filteredsegments = pointsandlines[pointsandlines["n_intersections"] == 2]


    # --- STEP 2: Build connectivity graph
    G = nx.Graph()
    for line_ids in filteredsegments["line_ids"]:
        if len(line_ids) == 2:
            G.add_edge(line_ids[0], line_ids[1])

    # --- STEP 3: Find connected components (chains)
    merged_records = []
    merged_ids = set()
    id_mapping = {}
    merged_audit = []

    for component in nx.connected_components(G):
        component_ids = list(component)
        linestobemerged = linegdf[linegdf["id"].isin(component_ids)].copy()

        merged_line = ops.linemerge(list(linestobemerged.geometry))
        representative_row = linestobemerged.iloc[0].copy()
        new_id = representative_row["id"]

        representative_row["geometry"] = merged_line
        representative_row["merged_from"] = component_ids

        merged_records.append(representative_row)
        merged_ids.update(component_ids)

        # map old ids → new id
        for old_id in component_ids:
            id_mapping[old_id] = new_id

        merged_audit.append({
            "new_id": new_id,
            "merged_from": component_ids
        })

    # --- STEP 4: Combine back
    merged_gdf = gpd.GeoDataFrame(merged_records)
    cleaned_gdf = linegdf[~linegdf["id"].isin(merged_ids)].copy()
    cleaned_gdf = pd.concat([cleaned_gdf, merged_gdf], ignore_index=True)

    gj = json.loads(cleaned_gdf.to_json())

    # Remove feature-level "id"
    for feature in gj["features"]:
        feature.pop("id", None)  # remove the "id" key if it exists
    geojson_str = json.dumps(gj)



    return geojson_str, id_mapping, merged_audit




def snap_line_endpoints(lines_gdf, bounds=[[0,0],[600,850]]):

    # store before geometries as WKT (simple, fast)
    before_wkt = {row["id"]: row.geometry.wkt for _, row in lines_gdf.iterrows()}

    # Extract endpoints
    endpoints = [Point(line.coords[0]) for line in lines_gdf.geometry] + \
                [Point(line.coords[-1]) for line in lines_gdf.geometry]
    endpoints_gs = gpd.GeoSeries(endpoints)

    bounds = [[0, 0], [600, 850]]  # xmin, ymin, xmax, ymax in coordinate units
    # Compute tolerance from bounds in same units as coordinates
    xmin, ymin = bounds[0]
    xmax, ymax = bounds[1]
    width = xmax - xmin
    height = ymax - ymin
    tolerance = 0.01 * min(width, height)  # 1% of smaller dimension


    # Snap endpoints
    endpoints_union = unary_union(endpoints_gs)
    snapped_lines = lines_gdf.copy()
    snapped_lines["geometry"] = snapped_lines.geometry.apply(
        lambda g: snap(g, endpoints_union, tolerance)
    )

    # detect real changes
    changed_ids = []
    for _, row in snapped_lines.iterrows():
        _id = row["id"]
        if row.geometry.wkt != before_wkt[_id]:
            changed_ids.append(_id)


    return snapped_lines,changed_ids

def find_snapped_groups(original_lines_gdf, snapped_lines_gdf, id_col="id"):
    # Step 1: before intersections
    before_pairs = endpoint_intersection_pairs(original_lines_gdf, id_col=id_col)

    # Step 2: after intersections
    after_pairs = endpoint_intersection_pairs(snapped_lines_gdf, id_col=id_col)

    # Step 3: pairs that are new (were not intersecting before)
    new_pairs = after_pairs - before_pairs



    return new_pairs


# small helpers
def _to_native_id(x):
    # convert numpy/pandas ints to native int when possible, else str
    try:
        return int(x)
    except Exception:
        return str(x)

def _id_sort_key(x):
    # sort numeric ids numerically, strings lexicographically
    if isinstance(x, int):
        return (0, x)
    try:
        xi = int(x)
        return (0, xi)
    except Exception:
        return (1, str(x))


def endpoint_intersection_pairs(lines_gdf, id_col="id"):
    rows = []

    for _, row in lines_gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.geom_type != "LineString":
            continue

        sid = row[id_col]
        coords = list(geom.coords)
        for (x, y) in coords:
            rows.append({"id": sid, "x": x, "y": y})

    if not rows:
        return set()

    df_ep = pd.DataFrame(rows)

    # group by coordinate, collect ids that meet at that coordinate
    grouped = df_ep.groupby(["x", "y"])["id"].apply(list)

    pairs = set()
    for seg_ids in grouped:
        if len(seg_ids) < 2:
            continue
        seg_ids = [_to_native_id(s) for s in seg_ids]
        for i in range(len(seg_ids)):
            for j in range(i + 1, len(seg_ids)):
                a, b = seg_ids[i], seg_ids[j]
                if a == b:
                    continue
                pairs.add(frozenset((a, b)))

    return pairs

def combine_by_basealign(alignment):
    combined = defaultdict(list)

    for key, value in alignment.items():
        if key == 'checkAlignnum':  # skip metadata
            continue

        basealign_val = tuple(sorted(value['BaseAlign']['0']))  # make it hashable
        combined[basealign_val].append(copy.deepcopy(value))

    # Build merged output
    merged = {}
    for i, (basealign_val, items) in enumerate(combined.items(), start=1):
        merged_key = str(i)
        merged[merged_key] = {
            'BaseAlign': {'0': list(basealign_val)},
            'SketchAlign': {},
            'genType': items[0]['genType'],
            'degreeOfGeneralization': items[0]['degreeOfGeneralization']
        }

        # Combine SketchAlign entries
        all_sketches = []
        for item in items:
            for k, v in item['SketchAlign'].items():
                all_sketches.extend(v)
        merged[merged_key]['SketchAlign']['0'] = all_sketches

    merged['checkAlignnum'] = len(merged)
    return merged

def remap_alignment_ids(alignment, id_mapping):
    """
    Update alignment dictionary to reflect new IDs from merging.
    """
    new_alignment = {}
    for key, value in alignment.items():
        if key == "checkAlignnum":
            continue

        new_val = copy.deepcopy(value)
        new_basealign = {}

        for k, v in value["BaseAlign"].items():
            new_ids = []
            for old_id in v:
                new_ids.append(id_mapping.get(old_id, old_id))
            new_basealign[k] = list(set(new_ids))  # remove duplicates

        new_val["BaseAlign"] = new_basealign
        new_alignment[key] = new_val

    new_alignment["checkAlignnum"] = alignment.get("checkAlignnum", len(new_alignment))
    return new_alignment


def validateRoute(line_gdf, route_ids, id_col="id", inplace=False):
    print ("check if everything is fine in route",route_ids)
    if not route_ids or len(route_ids) < 2:
        return line_gdf if inplace else line_gdf.copy()

    gdf = line_gdf if inplace else line_gdf.copy()

    # Build id -> row index
    id_to_idx = {val: idx for idx, val in gdf[id_col].items()}

    if len(route_ids) >= 2:
        print ("check here route")
        f_id, s_id = route_ids[0], route_ids[1]
        f_idx = id_to_idx.get(f_id)
        s_idx = id_to_idx.get(s_id)
        if f_idx is not None and s_idx is not None:
            f_geom = gdf.at[f_idx, "geometry"]
            s_geom = gdf.at[s_idx, "geometry"]

            f_start = tuple(f_geom.coords[0]);
            f_end = tuple(f_geom.coords[-1])
            s_start = tuple(s_geom.coords[0]);
            s_end = tuple(s_geom.coords[-1])

            if (f_start == s_start) or (f_start == s_end):
                gdf.at[f_idx, "geometry"] = reverse(f_geom)

    for i in range(len(route_ids) - 1):
        curr_id = route_ids[i]
        next_id = route_ids[i + 1]
        curr_idx = id_to_idx.get(curr_id)
        next_idx = id_to_idx.get(next_id)

        if curr_idx is None or next_idx is None:
            continue  # skip missing ids

        curr_geom = gdf.at[curr_idx, "geometry"]
        next_geom = gdf.at[next_idx, "geometry"]

        curr_end = tuple(list(curr_geom.coords)[-1])
        next_start = tuple(list(next_geom.coords)[0])

        if next_start != curr_end:
            gdf.at[next_idx, "geometry"] = reverse(next_geom)

    return gdf



def to_builtin_types(obj):
    """
    Recursively convert numpy / pandas types to native Python types
    Useful just before json.dumps
    """
    # primitives
    if isinstance(obj, (str, bool, type(None))):
        return obj
    if isinstance(obj, numbers.Integral) and not isinstance(obj, bool):
        return int(obj)
    if isinstance(obj, numbers.Real):
        return float(obj)
    # numpy scalar types
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    # mapping
    if isinstance(obj, dict):
        return { to_builtin_types(k): to_builtin_types(v) for k, v in obj.items() }
    # list/tuple
    if isinstance(obj, (list, tuple, set)):
        return [to_builtin_types(i) for i in obj]
    # fallback to string
    return str(obj)



def apply_approved_snaps(line_gdf, snap_groups, id_col="id", inplace=False):


    gdf = line_gdf if inplace else line_gdf.copy()

    # id -> row index
    id_to_idx = {row_id: idx for idx, row_id in gdf[id_col].items()}
    snap_groups = snap_groups or []
    for group in snap_groups:
        if not group or len(group) < 2:
            continue

        # anchor is the first id in the group
        anchor_id = group[0]
        anchor_idx = id_to_idx.get(anchor_id)
        if anchor_idx is None:
            continue

        anchor_geom = gdf.at[anchor_idx, "geometry"]
        anchor_coords = list(anchor_geom.coords)
        anchor_endpoints = [anchor_coords[0], anchor_coords[-1]]  # (x,y) tuples

        # snap all other lines in the group to the anchor endpoints
        for sid in group[1:]:
            idx = id_to_idx.get(sid)
            if idx is None:
                continue

            geom = gdf.at[idx, "geometry"]
            coords = list(geom.coords)
            if len(coords) < 2:
                continue

            # endpoints of this line
            ep = [coords[0], coords[-1]]

            # find which endpoint (start or end) is closest to which anchor endpoint
            min_dist2 = float("inf")
            best_anchor = None
            best_ep_idx = None

            for a in anchor_endpoints:
                for j, e in enumerate(ep):
                    dx = a[0] - e[0]
                    dy = a[1] - e[1]
                    d2 = dx*dx + dy*dy  # squared distance
                    if d2 < min_dist2:
                        min_dist2 = d2
                        best_anchor = a
                        best_ep_idx = j

            # move that endpoint to the chosen anchor coord
            if best_ep_idx == 0:
                coords[0] = best_anchor
            else:
                coords[-1] = best_anchor

            gdf.at[idx, "geometry"] = LineString(coords)

    return gdf

from shapely.ops import linemerge

def apply_approved_merges(line_gdf, merge_groups, id_col="id", inplace=False):

    gdf = line_gdf if inplace else line_gdf.copy()

    # id -> row index
    id_to_idx = {row_id: idx for idx, row_id in gdf[id_col].items()}

    rows_to_drop = []
    for group in merge_groups:
        if not group or len(group) < 2:
            continue

        # collect valid indices for this group
        idxs = []
        for sid in group:
            idx = id_to_idx.get(sid)
            if idx is not None:
                idxs.append(idx)

        if len(idxs) < 2:
            continue

        # geometries to merge
        geoms = list(gdf.loc[idxs, "geometry"])
        merged_geom = linemerge(geoms)

        # representative = first id in group
        rep_id = group[0]
        rep_idx = id_to_idx.get(rep_id)
        if rep_idx is None:
            # fallback: first index in idxs
            rep_idx = idxs[0]
            rep_id = gdf.at[rep_idx, id_col]

        # assign merged geometry to representative row
        gdf.at[rep_idx, "geometry"] = merged_geom

        # mark other rows for deletion
        for idx in idxs:
            if idx != rep_idx:
                rows_to_drop.append(idx)

    if rows_to_drop:
        gdf = gdf.drop(index=rows_to_drop).reset_index(drop=True)

    return gdf


def validate(request):
    type= request.POST.get('type')
    action = request.POST.get('action', 'preview')  # 'preview' or 'apply'

    if type == "metric":
        metricmapdata = request.POST.get('metricdata')
        routearray_raw = request.POST.get('route')  # string like "[1,2,3]"
        print("routearray_", routearray_raw)
        route_ids = json.loads(routearray_raw) if routearray_raw else []
        print(route_ids)

        metricMap = json.loads(metricmapdata)


        features = metricMap.get("features", [])

        line_features = []
        polygon_features = []

        for f in features:
            gtype = f["geometry"]["type"]
            if gtype == "LineString":
                line_features.append(f)
            elif gtype in ("Polygon", "MultiPolygon"):
                polygon_features.append(f)

        # Convert lines to GeoDataFrame
        line_gdf = gpd.GeoDataFrame.from_features(line_features)



        # --- Snap and merge Lines ---
        snapped_lines, snapped_ids = snap_line_endpoints(line_gdf)
        grouped_snaps = find_snapped_groups(line_gdf, snapped_lines)
        merged_lines_json, id_mapping,merged_audit = merge_simple_intersections(snapped_lines)



        snap_id_pairs = [sorted(list(g)) for g in grouped_snaps]

        audit = {
            "snap": snap_id_pairs,
            "merge": merged_audit
        }


        if action == 'preview':
            # Return proposed edits and audit to UI — do NOT commit changes
            return HttpResponse(json.dumps({
                "audit": to_builtin_types(audit)
            }), content_type="application/json")

        if action == 'apply':
            approved_snap_pairs = request.POST.get('snap')
            approved_merge_pairs = request.POST.get('merge')
            approved_snap_json = json.loads(approved_snap_pairs) if approved_snap_pairs else []
            approved_merge_json = json.loads(approved_merge_pairs) if approved_merge_pairs else []

            snapped_lines = apply_approved_snaps(line_gdf, approved_snap_json, id_col="id", inplace=False)

            approved_merge_json = approved_merge_json or []
            merge_groups = [m["merged_from"] for m in approved_merge_json]
            merged_lines = apply_approved_merges(snapped_lines, merge_groups, id_col="id", inplace=False)
            print ("inside apply", route_ids)
            routecorrected = validateRoute(merged_lines, route_ids, id_col="id", inplace=False)

            edited_lines_geojson = json.loads(routecorrected.to_json())
            edited_line_features = edited_lines_geojson["features"]


            # 3) combine back with polygons
            final_geojson = {
                "type": "FeatureCollection",
                "features": edited_line_features + polygon_features
            }




            return HttpResponse(json.dumps({
                "modifiedStreets": final_geojson
            }), content_type="application/json")


    if type == "sketch":
        sketchmapdata = request.POST.get('sketchdata')
        alignmentdata = request.POST.get('alignment')
        routearray_raw = request.POST.get('route')  # string like "[1,2,3]"
        route_ids = json.loads(routearray_raw) if routearray_raw else []


        sketchMap = json.loads(sketchmapdata)
        alignment = json.loads(alignmentdata)

        # assume metricMap is GeoJSON: {"type": "FeatureCollection", "features": [...]}
        features = sketchMap.get("features", [])

        line_features = []
        polygon_features = []

        for f in features:
            gtype = f["geometry"]["type"]
            if gtype == "LineString":
                line_features.append(f)
            elif gtype in ("Polygon", "MultiPolygon"):
                polygon_features.append(f)

        # Convert lines to GeoDataFrame
        line_gdf = gpd.GeoDataFrame.from_features(line_features)

        # --- Snap and merge Lines ---
        snapped_lines, snapped_ids = snap_line_endpoints(line_gdf)
        grouped_snaps = find_snapped_groups(line_gdf, snapped_lines)
        merged_lines_json, id_mapping, merged_audit = merge_simple_intersections(snapped_lines)
        snap_id_pairs = [sorted(list(g)) for g in grouped_snaps]


        audit = {
            "snap": snap_id_pairs,
            "merge": merged_audit
        }

        if action == 'preview':
            # Return proposed edits and audit to UI — do NOT commit changes
            return HttpResponse(json.dumps({
                "audit": to_builtin_types(audit)
            }), content_type="application/json")

        if action == 'apply':
            approved_snap_pairs = request.POST.get('snap')
            approved_merge_pairs = request.POST.get('merge')
            approved_snap_json = json.loads(approved_snap_pairs) if approved_snap_pairs else []
            approved_merge_json = json.loads(approved_merge_pairs) if approved_merge_pairs else []

            snapped_lines = apply_approved_snaps(line_gdf, approved_snap_json, id_col="id", inplace=False)

            approved_merge_json = approved_merge_json or []
            merge_groups = [m["merged_from"] for m in approved_merge_json]
            merged_lines = apply_approved_merges(snapped_lines, merge_groups, id_col="id", inplace=False)
            print ("inside apply", route_ids)
            routecorrected = validateRoute(merged_lines, route_ids, id_col="id", inplace=False)

            edited_lines_geojson = json.loads(routecorrected.to_json())
            edited_line_features = edited_lines_geojson["features"]

            # 3) combine back with polygons
            final_geojson = {
                "type": "FeatureCollection",
                "features": edited_line_features + polygon_features
            }



            corrected_alignment = combine_by_basealign(alignment)
            remapped_alignment = remap_alignment_ids(corrected_alignment, id_mapping)
            return HttpResponse(json.dumps({
                "modifiedStreets": final_geojson,
                "updated_alignment": remapped_alignment
            }), content_type="application/json")











