# Get all the constant data one time and store them in json files
# Directory structure:
# data/
# ├── consts.py
# ├── routes.json
# └── stops.json

# Data gathered from zip file:
# routes.txt -> to fill routes.json
# stops.txt -> to fill stops.json
# translations.txt -> to translate everything in the jsons

# routes.json:
# {route_id: {route_long_name(translated), route_short_name}}

# stops.json:
# {stop_id: {stop_name (translated), stop_lat, stop_lon}}

import csv
import json
import os
import requests
from io import BytesIO
from zipfile import ZipFile


def terminate_connection():
    print("Deleting txt files")
    os.remove("data/agency.txt")
    os.remove("data/calendar.txt")
    os.remove("data/calendar_dates.txt")
    os.remove("data/feed_info.txt")
    os.remove("data/routes.txt")
    os.remove("data/stop_times.txt")
    os.remove("data/stops.txt")
    os.remove("data/transfers.txt")
    os.remove("data/translations.txt")
    os.remove("data/trips.txt")
    print("Finished deleting")
    exit(0)


url = 'https://api-management-discovery-production.azure-api.net/api/gtfs/feed/nmbssncb/static'
print("Downloading started")
response = requests.get(url)
file_path = "data/nmbssncb.zip"

if response.status_code == 200:
    with ZipFile(BytesIO(response.content)) as zip_file:
        zip_file.extractall('data/')
        print("Files extracted succesfully")
else:
    print(f"Failed to download file: {response.status_code}")
    exit(1)

translations = {}

print("Parsing translations")
with open("data/translations.txt") as csvfile:
    reader = csv.DictReader(csvfile, delimiter=",")

    translations = {
        row["field_value"]: row["translation"]
        for row in reader
        if row["table_name"] == "stops" and row["language"] == "nl"
    }

if len(translations) == 0:
    print("Failed parsing translations")
    terminate_connection()


stops = {}
print("Parsing stops")
with open("data/stops.txt") as csvfile:
    reader = csv.DictReader(csvfile, delimiter=",")

    stops = {
        row["stop_id"]: {
            "stop_name": translations[row["stop_name"]] if row["stop_name"] in translations else row["stop_name"],
            "stop_lat": row["stop_lat"],
            "stop_lon": row["stop_lon"]
        }
        for row in reader
    }

if len(stops) == 0:
    print("Failed parsing stops")
    terminate_connection()

routes = {}
print("Parsing routes")
with open("data/routes.txt") as csvfile:
    reader = csv.DictReader(csvfile, delimiter=",")

    routes = {
        row["route_id"]: {
            "route_long_name": " -- ".join([translations[e.strip()] if e.strip() in translations else e.strip() for e in row["route_long_name"].split("--")]),
            "route_short_name": row["route_short_name"]
        }
        for row in reader
    }

if len(routes) == 0:
    print("Failed parsing routes")
    terminate_connection()

print("Writing json files")
json_stops = json.dumps(stops, indent=4)
with open("data/stops.json", "w") as f:
    f.write(json_stops)

json_routes = json.dumps(routes, indent=4)
with open("data/routes.json", "w") as f:
    f.write(json_routes)

print("Done, cleaning up")
terminate_connection()
