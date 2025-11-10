import os
from django.shortcuts import render
from django.http import HttpResponse
import json
import geopandas as gpd
from shapely import ops, union_all,reverse
from shapely.ops import linemerge
from shapely.geometry import Point
from shapely.ops import snap
import geopandas as gpd
from shapely.geometry import Point, box
from shapely.ops import snap
import numpy as np
from scipy.spatial import cKDTree


import geopandas as gpd
import pandas as pd
import networkx as nx
import shapely
from shapely.ops import linemerge
from collections import defaultdict
import copy

def merge_simple_intersections(gdf):
    # Work only with LineStrings
    linegdf = gdf[gdf.geometry.type == "LineString"].copy().reset_index(drop=True)


    nodes = []
    for line in gdf.geometry:
        nodes.append(Point(line.coords[0]))
        nodes.append(Point(line.coords[-1]))

    nodes_gs = gpd.GeoSeries(nodes, crs=gdf.crs)
    unique_nodes = nodes_gs.drop_duplicates()


    streetsegments_geoseries = linegdf.geometry

    # Ensure both have same CRS
    if unique_nodes.crs != streetsegments_geoseries.crs:
        streetsegments_geoseries = streetsegments_geoseries.to_crs(unique_nodes.crs)

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
    pointsandlines =  gpd.GeoDataFrame(records, crs=unique_nodes.crs)


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

    # --- STEP 4: Combine back
    merged_gdf = gpd.GeoDataFrame(merged_records, crs=gdf.crs)
    cleaned_gdf = linegdf[~linegdf["id"].isin(merged_ids)].copy()
    cleaned_gdf = pd.concat([cleaned_gdf, merged_gdf], ignore_index=True)

    gj = json.loads(cleaned_gdf.to_json())

    # Remove feature-level "id"
    for feature in gj["features"]:
        feature.pop("id", None)  # remove the "id" key if it exists
    geojson_str = json.dumps(gj)



    return geojson_str, id_mapping




def snap_line_endpoints(gdf, bounds=[[0,0],[600,850]]):
    """
    Snaps endpoints of LineStrings in a GeoDataFrame to each other using tolerance
    derived from bounds extent.
    """
    import geopandas as gpd
    import numpy as np
    from shapely.geometry import Point, box
    from shapely.ops import unary_union, snap

    # Ensure we work with LineStrings
    lines_gdf = gdf[gdf.geometry.type=="LineString"].copy().reset_index(drop=True)

    # Extract endpoints
    endpoints = [Point(line.coords[0]) for line in lines_gdf.geometry] + \
                [Point(line.coords[-1]) for line in lines_gdf.geometry]
    endpoints_gs = gpd.GeoSeries(endpoints, crs=gdf.crs)

    bounds = [[0, 0], [600, 850]]  # xmin, ymin, xmax, ymax in coordinate units
    # Compute tolerance from bounds in same units as coordinates
    xmin, ymin = bounds[0]
    xmax, ymax = bounds[1]
    width = xmax - xmin
    height = ymax - ymin
    tolerance = 0.01 * min(width, height)  # 1% of smaller dimension

    print(f"Dynamic tolerance in coordinate units: {tolerance:.3f}")

    # Snap endpoints
    endpoints_union = unary_union(endpoints_gs)
    snapped_lines = lines_gdf.copy()
    snapped_lines["geometry"] = snapped_lines.geometry.apply(
        lambda g: snap(g, endpoints_union, tolerance)
    )

    return snapped_lines





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
    if not route_ids or len(route_ids) < 2:
        return line_gdf if inplace else line_gdf.copy()

    gdf = line_gdf if inplace else line_gdf.copy()

    # Build id -> row index
    id_to_idx = {val: idx for idx, val in gdf[id_col].items()}

    if len(route_ids) >= 2:
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
        print(curr_id,next_id,curr_idx,next_idx)

        if curr_idx is None or next_idx is None:
            continue  # skip missing ids

        curr_geom = gdf.at[curr_idx, "geometry"]
        next_geom = gdf.at[next_idx, "geometry"]

        curr_end = tuple(list(curr_geom.coords)[-1])
        next_start = tuple(list(next_geom.coords)[0])

        if next_start != curr_end:
            gdf.at[next_idx, "geometry"] = reverse(next_geom)

    return gdf





def validate(request):
    """
    Django view: receives metricMap JSON (likely GeoJSON),
    merges street segments that only touch at simple intersections,
    returns cleaned network as GeoJSON.
    """
    type= request.POST.get('type')

    if type == "metric":
        metricmapdata = request.POST.get('metricdata')
        routearray_raw = request.POST.get('route')  # string like "[1,2,3]"
        route_ids = json.loads(routearray_raw) if routearray_raw else []
        print("check check check", route_ids)

        metricMap = json.loads(metricmapdata)

        # assume metricMap is GeoJSON: {"type": "FeatureCollection", "features": [...]}
        features = metricMap.get("features", [])

        # Convert to GeoDataFrame
        gdf = gpd.GeoDataFrame.from_features(features)

        # Optional: ensure a CRS (e.g. EPSG:4326)
        if gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)

        # --- Separate Lines & Polygons ---
        line_gdf = gdf[gdf.geometry.type == "LineString"].copy()
        poly_gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()

        # --- Snap and merge Lines ---
        snapped_lines = snap_line_endpoints(line_gdf)
        merged_lines_json, id_mapping = merge_simple_intersections(snapped_lines)

        # --- Combine merged lines + untouched polygons ---
        merged_lines_json = json.loads(merged_lines_json)
        merged_lines_gdf = gpd.GeoDataFrame.from_features(merged_lines_json["features"])
        routecorrected = validateRoute(merged_lines_gdf, route_ids, id_col="id", inplace=False)

        combined_gdf = pd.concat([routecorrected, poly_gdf], ignore_index=True)
        combined_gdf = gpd.GeoDataFrame(combined_gdf, crs=gdf.crs)



        # Convert back to GeoJSON-like dict
        response_geojson = json.loads(combined_gdf.to_json())
        return HttpResponse(json.dumps(response_geojson), content_type="application/json")

    if type == "sketch":
        sketchmapdata = request.POST.get('sketchdata')
        alignmentdata = request.POST.get('alignment')
        routearray_raw = request.POST.get('route')  # string like "[1,2,3]"
        route_ids = json.loads(routearray_raw) if routearray_raw else []
        print("check check check sketch", route_ids)

        sketchMap = json.loads(sketchmapdata)
        alignment = json.loads(alignmentdata)

        # assume metricMap is GeoJSON: {"type": "FeatureCollection", "features": [...]}
        features = sketchMap.get("features", [])

        # Convert to GeoDataFrame
        gdf = gpd.GeoDataFrame.from_features(features)

        # Optional: ensure a CRS (e.g. EPSG:4326)
        if gdf.crs is None:
            gdf.set_crs(epsg=4326, inplace=True)



        # --- Separate Lines & Polygons ---
        line_gdf = gdf[gdf.geometry.type == "LineString"].copy()
        poly_gdf = gdf[gdf.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()

        # --- Snap and merge Lines ---
        snapped_lines = snap_line_endpoints(line_gdf)
        merged_lines_json, id_mapping = merge_simple_intersections(snapped_lines)

        # --- Combine merged lines + untouched polygons ---

        merged_lines_json = json.loads(merged_lines_json)
        merged_lines_gdf = gpd.GeoDataFrame.from_features(merged_lines_json["features"])
        routecorrected = validateRoute(merged_lines_gdf, route_ids, id_col="id", inplace=False)
        combined_gdf = pd.concat([routecorrected, poly_gdf], ignore_index=True)
        combined_gdf = gpd.GeoDataFrame(combined_gdf, crs=gdf.crs)
        # Convert back to GeoJSON-like dict
        response_geojson = json.loads(combined_gdf.to_json())

        corrected_alignment = combine_by_basealign(alignment)
        remapped_alignment = remap_alignment_ids(corrected_alignment, id_mapping)

        print("alignment (updated):", remapped_alignment)

        return HttpResponse(json.dumps({
            "updated_sketch": response_geojson,
            "updated_alignment": remapped_alignment
        }, default=lambda o: int(o) if hasattr(o, 'item') else str(o)), content_type="application/json")





