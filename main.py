import csv
import json
import math
import os.path
import pathlib
from datetime import datetime

import geopandas as gpd
import pandas as pd
import pyproj
import shapely
import simplekml
from shapely import Polygon
from shapely.geometry import Point, shape

# File to use for raw data caching. If it exists, data will be loaded from it instead of querying the internet. To
# force a redownload, just delete this file.
DATA_FILE = ".cache/data.json"
# File to use for geo data caching. If it exists, data will be loaded from it instead of running the processing. To
# force re-running the processing, just delete this file.
GEO_DATA_FILE = ".cache/geodata.json"
# Files to output results to
RESULT_FILE_JSON = "output.json"
RESULT_FILE_KML = "output.kml"
# To reduce output file size and processing time, ignore any regions that are overlaps, but of fewer than this number
# of entities.
MIN_OVERLAP_TO_REPORT = 4
# When converting a circle to a polygon, how many points should be used around the edge? Larger numbers mean longer
# processing times, but more accurate results.
CIRCLE_TO_POLY_POINTS = 128

# UKBOTA CSV data file
BUNKERS_FILE_PATH = "UKBOTA-8_01-FINALcsv.csv"
BUNKER_RADIUS_METRES = 1000

# Static storage
WGS84_TO_OS_GRID_TRANSFORMER = pyproj.Transformer.from_crs(4326, 27700)
OS_GRID_TO_WGS84_TRANSFORMER = pyproj.Transformer.from_crs(27700, 4326)

# Data storage
all_data = []
data_file = pathlib.Path(DATA_FILE)
geo_data_file = pathlib.Path(GEO_DATA_FILE)
result_file = pathlib.Path(RESULT_FILE_JSON)

# Main code starts here
# If we don't have an existing data file, start fetching data from the internet.
if not os.path.isfile(DATA_FILE):
    # Retrieve bunkers data
    print("Retrieving Bunker data...")
    with open(BUNKERS_FILE_PATH, newline='', encoding="utf-8-sig") as csvfile:
        reader = csv.DictReader(csvfile, dialect='excel')
        for entity in reader:
            all_data.append({"ref": entity["Description"],
                             "name": entity["Name"],
                             "type": "BUNKER",
                             "radiusMetres": BUNKER_RADIUS_METRES,
                             "lat": float(entity["Latitude"]),
                             "lon": float(entity["Longitude"])})

    # Write cache file
    print("Writing cache file...")
    data_file.parent.mkdir(exist_ok=True, parents=True)
    data_file.write_text(json.dumps(all_data, indent=2))

else:
    print("Reading cache file...")
    with open(data_file) as f:
        all_data = json.load(f)

print(str(len(all_data)) + " entities found.")

print("Converting data for GeoPandas...")
start = datetime.now()

# Convert all lat/lons to OS grid reference. This will break totally for data outside the UK, but it saves a lot of
# hassle in GeoPandas because everything can be in metres.
for entity in all_data:
    os_grid_ref = WGS84_TO_OS_GRID_TRANSFORMER.transform(entity["lat"], entity["lon"])
    entity["northing"] = os_grid_ref[0]
    entity["easting"] = os_grid_ref[1]

# Prepare a polygons for each entity in OS grid reference space. We will need this later whether or not we have a
# cached geo data file containing the overlap segments.
for entity in all_data:
    gs = gpd.GeoSeries(Point(entity["northing"], entity["easting"]), crs=27700)
    buffer_gs = gs.buffer(entity["radiusMetres"], CIRCLE_TO_POLY_POINTS)
    entity["polygon"] = buffer_gs[0]

all_buffers_geoseries = gpd.GeoSeries(list(map(lambda p: p["polygon"], all_data)))

# Now run the processing, if we don't already have the result data
if not os.path.isfile(geo_data_file):
    # Assemble a GeoDataFrame containing all the entities.
    data = {'name': list(map(lambda p: p["ref"] + " " + p["name"], all_data)),
            'id': range(0, len(all_data)),
            'geom': all_buffers_geoseries}

    df = pd.DataFrame(data, columns=['name', 'id', 'geom'])
    gdf = gpd.GeoDataFrame(df, geometry='geom', crs=27700)

    print("Finding overlaps...")
    # Code from https://gis.stackexchange.com/questions/387773/count-overlapping-features-using-geopandas
    buffer_size = 0.1
    bounds = gdf.geometry.convex_hull.exterior.buffer(buffer_size).unary_union
    new_polys = list(shapely.ops.polygonize(bounds))
    # Removing the full merged polygons (first is always index 0,
    # subsequent will be the first of their own 'bunches' identified as disjoint from other 'bunches')
    bad_poly_idx = [0]
    while new_polys[max(bad_poly_idx)].disjoint(new_polys[-1]):
        for idx in range(max(bad_poly_idx), len(new_polys)):
            if new_polys[max(bad_poly_idx)].disjoint(new_polys[idx]):
                bad_poly_idx += [idx]
                break
    new_polys = [new_polys[i].buffer(-buffer_size) for i in range(len(new_polys)) if i not in bad_poly_idx]
    # count layers and track IDs of overlapping features
    gdf_with_overlap_polys = gpd.GeoDataFrame(geometry=new_polys)
    gdf_with_overlap_polys['layers'] = sum(
        [gdf_with_overlap_polys.geometry.intersects(poly) for poly in gdf.geometry.buffer(buffer_size).values])
    gdf_with_overlap_polys['piece'] = gdf_with_overlap_polys.index

    runtime = datetime.now() - start
    print("Generated " + str(len(gdf_with_overlap_polys.index)) + " overlap polys in " + str(
        runtime.total_seconds()) + " seconds.")

    print("Writing cache file...")
    geo_data_file.parent.mkdir(exist_ok=True, parents=True)
    geo_data_file.write_text(gdf_with_overlap_polys.to_json())
    print("Written " + str(int(os.path.getsize(GEO_DATA_FILE) / 1024 / 1024)) + " MB.")

else:
    print("Reading cache file...")
    with open(geo_data_file) as f:
        gdf_with_overlap_polys = gpd.read_file(f)

# Now iterate over the overlap poly features. Only care about ones with more than one "layer" (i.e. overlapping original
# entity). For each such overlap poly, create a test point inside it, and see which original entities it's in range of.
# Store that example point along with the list of entities in range.
print("Getting entity lists for overlap polygons...")
start = datetime.now()
overlap_data = []
for feature in gdf_with_overlap_polys.iterfeatures():
    if feature["properties"]["layers"] >= MIN_OVERLAP_TO_REPORT:
        test_point = shape(feature["geometry"]).representative_point()
        overlapping_entity_names = []
        for test_entity in all_data:
            north_dist = abs(test_point.x - test_entity["northing"])
            east_dist = abs(test_point.y - test_entity["easting"])
            dist = math.sqrt(east_dist * east_dist + north_dist * north_dist)
            if dist < test_entity["radiusMetres"]:
                overlapping_entity_names.append(test_entity["name"])
        poly_coords = feature["geometry"]["coordinates"][0]
        poly_coords_wgs84 = []
        for xy in poly_coords:
            poly_coords_wgs84.append(OS_GRID_TO_WGS84_TRANSFORMER.transform(xy[0], xy[1]))
        overlap_data.append({"polygon": poly_coords_wgs84,
                             "entities": overlapping_entity_names})

runtime = datetime.now() - start
print("Assessed " + str(len(gdf_with_overlap_polys.index)) + " overlap polys in " + str(
    runtime.total_seconds()) + " seconds.")

# Sort overlap data by number of entities
overlap_data_sorted = sorted(overlap_data, key=lambda p: len(p["entities"]), reverse=True)

print("Writing JSON results file...")
result_file.parent.mkdir(exist_ok=True, parents=True)
result_file.write_text(json.dumps(overlap_data_sorted, indent=2))

print("Writing KML results file...")
kml = simplekml.Kml()
for d in overlap_data_sorted:
    closed_poly = []
    for point in d["polygon"]:
        closed_poly.append([point[1], point[0]])
    closed_poly.append([d["polygon"][0][1], d["polygon"][0][0]])
    centroid_point = shapely.centroid(Polygon(closed_poly))
    centroid_lonlat = shapely.get_coordinates(centroid_point).tolist()[0]
    poly = kml.newpolygon(outerboundaryis=closed_poly)
    if len(d["entities"]) == 4:
        poly.style.polystyle.color = '99ff0000'
        poly.style.linestyle.color = 'ffff0000'
    elif len(d["entities"]) == 5:
        poly.style.polystyle.color = '9900ffff'
        poly.style.linestyle.color = 'ff00ffff'
    else:
        poly.style.polystyle.color = '990000ff'
        poly.style.linestyle.color = 'ff0000ff'
    name = str(len(d["entities"])) + "-fer"
    description = "<br/>".join(d["entities"])
    kml.newpoint(name=name, description=description, coords=[(centroid_lonlat[0], centroid_lonlat[1])])
kml.save(RESULT_FILE_KML)

print("Done.")
