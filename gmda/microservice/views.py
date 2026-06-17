import json 
import math
import numpy as np
from collections import defaultdict
from shapely.geometry import shape
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt



def _normalize_align_value(v):
    """
    This function normalizes the align values to a list of strings. Since SketchAlign values from 
    a feature can be a string, a list, or  nested. This will always return a clean flat list of sketch IDs.
    """
    if v is None: 
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out.extend(_normalize_align_value(x))
        return out
    s = str(v).strip()
    if s.lower().startswith('s') and s[1:].isdigit():
        s = s[1:]
    return [s] if s else []


def compute_mbr_points(geometry, r_buffer = 1):
    """
    This function will compute the 8 points from the Minimum Bounding 
    Rectangle drawn on a Polygon or MultiPolygon, 
    if it is a PointMarker, it will draw a buffer of 1 pixel radius
    and then extract the 8 points from the MBR.
    """ 
    if geometry.geom_type in ('Polygon', 'MultiPolygon'):
        x_min, y_min, x_max, y_max = map(float, geometry.bounds)
    elif geometry.geom_type =='Point':
        coords = list(geometry.coords)[0]
        x, y = float(coords[0]), float(coords[1])
        x_min, x_max = x - r_buffer, x + r_buffer
        y_min, y_max = y -r_buffer, y + r_buffer
    else:
        return None
    return [
        [x_min, y_max], [(x_min + x_max) / 2, y_max], 
        [x_max, y_max],
        [x_max, (y_min + y_max) / 2], [x_max, y_min],
        [(x_min + x_max) / 2, y_min],
        [x_min, y_min], 
        [x_min, (y_min + y_max) / 2] 
    ]


def landmark_pairs_generator(v_pairs, b_mbr, s_mbr):
    """
    This function will generate all possible point pairs between every pair of aligned
    landmarks by comparing each of the MBR points of one landmark against 
    each of the 8 MBR points of another landmakr.  
    """
    for i in range(len(v_pairs) - 1):
        b1, s1 = v_pairs[i]
        for j in range( i+1 ,len(v_pairs)):
            b2, s2 = v_pairs[j]
            if s1 == s2:
                continue
            b1_pts, s1_pts = b_mbr[b1], s_mbr[s1]
            b2_pts, s2_pts = b_mbr[b2], s_mbr[b2]
            for p1 in range(8):
                for p2 in range(8):
                    yield (
                        b1_pts[p1][0], b1_pts[p1][1],
                        b2_pts[p2][0], b2_pts[p2][1],
                        s1_pts[p1][0], s1_pts[p1][1],
                        s2_pts[p2][0], s2_pts[p2][1]
                    )

class UnionFind:
    """ 
    This is used to group lanmarks that are aligned together.
    For example, if B1 is aligned to S1 and S2 features, 
    UnionFind will group S1 and S2 together, 
    so that we can handle them correctly. 
    (excluding them from 1-to-1 pairs)
    """
    def __init__(self):
        self.parent = {}
    
    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x, y):
        px, py = self.find(x), self.find(y)
        if px != py:
            self.parent[px] = py

def compute_gmda(basemap_geojson, sketchmap_geojson):
    """
    
    """
    LANDMARK_TYPES = ('Polygon', 'MultiPolygon', 'CircleMarker', 'Landmark')

    # Loading Basemap Landmarks
    bsm_rows = []
    for feat in basemap_geojson.get('features', []):
        props = (feat.get('properties') or {}).copy()#
        try:
            geom = shape(feat['geometry'])
            props['geometry'] = geom
            otype = props.get('otype') or geom.geom_type
            if otype in LANDMARK_TYPES:
                if geom.geom_type in ('Polygon', 'MultiPolygon') and not geom.is_valid:
                    continue
                bsm_rows.append(props)
        except Exception as e:
                continue
    
    # Loading Sketchmap Landmarks
    skm_rows = []
    for feat in sketchmap_geojson.get('features', []):
        props = (feat.get('properties') or {}).copy()
        try:
            geom = shape(feat['geometry'])
            props['geometry'] = geom
            otype = props.get('otype') or geom.geom_type
            if otype in LANDMARK_TYPES:
                if geom.geom_type in ('Polygon', 'MultiPolygon') and not geom.is_valid:
                    continue
                skm_rows.append(props)
        except Exception as e:
            continue

    # Building MBR points for basemap and sketchmap landmarks
    #This builds two dictionaries, 
    # One for the basemap landmarks 
    # Second, for the sketchmap landmarks
    # mapping each feature ID to its 8 mbr points.
    bsm_dict_mbr = {}
    for row in bsm_rows:
        mbr = compute_mbr_points(row['geometry'])
        if mbr:
            base_id = str(row.get('id', '')).strip()
            if base_id:
                bsm_dict_mbr[base_id] = mbr 
    
    skm_dict_mbr = {}
    for row in skm_rows:
        mbr = compute_mbr_points(row['geometry'])
        sk_id_raw = row.get('sid') or row.get('id')
        if sk_id_raw is not None:
            sk_id = str(sk_id_raw).strip()
            if sk_id.lower().startswith('s') and sk_id[1:].isdigit():
                sk_id = sk_id[1:]
            if mbr:
                skm_dict_mbr[sk_id] = mbr
    
    # Building alignment map from basemap SketchAlign property.
    alignment_map = {}
    for b_row in bsm_rows:
        if b_row.get('missing') is True:
            continue
        otype = b_row.get('otype') or b_row['geometry'].geom_type
        if otype not in LANDMARK_TYPES:
            continue
        base_id = str(b_row.get('id', '')).strip()
        if not base_id:
            continue
        linked = _normalize_align_value(b_row.get('SketchAlign'))
        if linked:
            alignment_map[base_id] = linked

    # Union-Find to group sketchmap features that are aligned together
    #This will read the SketchAlign property from each basemap feature
    # to find which sketch features are aligned to it, 
    # and then it uses UnionFind to group them together.
    uf = UnionFind()
    for base_id, sketch_ids in alignment_map.items():
        for s_id in sketch_ids:
            uf.union(f"B_{base_id}", f"S_{s_id}")
    
    groups = defaultdict(lambda: {'base_ids':set(), 'sketch_ids':set()})
    for base_id, sketch_ids in alignment_map.items():
        root = uf.find(f"B_{base_id}")
        groups[root]['base_ids'].add(base_id)
        for s_id in sketch_ids:
            groups[root]['sketch_ids'].add(s_id)
        
    #Classifying pairs
    #This will separate the 1 to 1 aligned pairs from the 
    #many to one pairs , ntl is total number of landmarks, ndl is the number of drawn landmarks (1 to 1 pairs)

    verified_pairs = []
    excluded_base_ids = set()
    for group in groups.values():
        num_sketch = len(group['sketch_ids'])
        if num_sketch == 1:
            sketch_id = list(group['sketch_ids'])[0]
            for base_id in group['base_ids']:
                if base_id in bsm_dict_mbr and sketch_id in skm_dict_mbr:
                    verified_pairs.append((base_id, sketch_id))
        else:
            excluded_base_ids.update(group['base_ids'])

    nTL = len(bsm_dict_mbr) - len(excluded_base_ids)
    nDL = len(verified_pairs)

    if nDL < 2:
        return {
            'ERROR' : "Insufficient number of aligned Landmark Pairs (Need atleast 2)",
            'Number of Total Landmarks': nTL,
            'Number of Drawn Landmarks': nDL
        }

    # Computing GMDA Metrics

    pairs_gen = list(landmark_pairs_generator(verified_pairs, bsm_dict_mbr, skm_dict_mbr))
    def comb2(n):
        return n * (n - 1) // 2

    n_nTL = comb2(8 * nTL) - nTL * comb2(8) if nTL > 1 else 0
    n_nDL = len(pairs_gen) if pairs_gen else 1

    sum_can, sum_dist_abs, sum_sca_bias = 0, 0, 0
    sum_rot_sin, sum_rot_cos, sum_ang_abs = 0, 0, 0
    max_db, max_ds = 0.001, 0.001

    for b1x, b1y, b2x, b2y, s1x, s1y, s2x, s2y in pairs_gen:
        max_db = max(max_db, np.sqrt((b1x - b2x)**2 + (b1y - b2y)**2))
        max_ds = max(max_ds, np.sqrt((s1x - s2x)**2 + (s1y - s2y)**2))

    for b1x, b1y, b2x, b2y, s1x, s1y, s2x, s2y in pairs_gen:
        if (b1y < b2y and s1y < s2y) or (b1y > b2y and s1y > s2y):
            sum_can += 1
        if (b1x < b2x and s1x < s2x) or (b1x > b2x and s1x > s2x):
            sum_can += 1
        db = np.sqrt((b1x - b2x)**2 + (b1y - b2y)**2) / max_db
        ds = np.sqrt((s1x - s2x)**2 + (s1y - s2y)**2) / max_ds
        sum_sca_bias += (ds - db)
        sum_dist_abs += abs(ds - db)
        ang_b = np.arctan2(b2x - b1x, b2y - b1y)
        ang_s = np.arctan2(s2x - s1x, s2y - s1y)
        d = (ang_s - ang_b + np.pi) % (2 * np.pi) - np.pi
        sum_rot_sin += np.sin(d)
        sum_rot_cos += np.cos(d)
        sum_ang_abs += abs(np.degrees(d))

    return {
        'CanOrg':  round(float(sum_can / (2 * n_nTL)), 4) if n_nTL > 0 else 0,
        'CanAcc':  round(float(sum_can / (2 * n_nDL)), 4) if n_nDL > 0 else 0,
        'ScaBias': round(float(sum_sca_bias / n_nDL), 4) if n_nDL > 0 else 0,
        'DistAcc': round(float(1 - (sum_dist_abs / n_nDL)), 4) if n_nDL > 0 else 0,
        'RotBias': round(float(np.degrees(np.arctan2(sum_rot_sin, sum_rot_cos))), 4),
        'AngAcc':  round(float(1 - sum_ang_abs / (180 * n_nDL)), 4) if n_nDL > 0 else 0,
        'nTL': nTL,
        'nDL': nDL,
    }


@csrf_exempt
def calculateGMDA(request):
    """ 
    This is the function that Django will call when the frontend 
    sends a POST request to /gmda/calculateGMDA/ 
    It reads the two geoJSON payloads and then passes them to 
    compute_gmda, and then sends then back as JSON.

    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status = 405)
    try:
        basemap_geojson = json.loads(request.POST.get('basemapdata', '{}'))
        sketchmap_geojson = json.loads(request.POST.get('sketchmapdata', '{}'))
        result = compute_gmda(basemap_geojson, sketchmap_geojson)
        return JsonResponse(result)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status = 500)


    
