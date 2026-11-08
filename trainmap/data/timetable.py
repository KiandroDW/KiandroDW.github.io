# Get the timetable data and put it into one large json
# Directory structure:
# data/
# ├── timetable.py
# ├── routes.json
# ├── stops.json
# └── timetable.json

# Data gathered from zip file:
# routes.txt -> to fill routes.json
# stops.txt -> to fill stops.json
# translations.txt -> to translate everything in the jsons
# calendar_dates.txt -> on which dates every service runs
# stop_times.txt -> every stop on every trip
# trips.txt -> link every trip with a route and a service

# routes.json:
# {route_id: {route_long_name(translated), route_short_name}}

# stops.json:
# {stop_id: {stop_name (translated), stop_lat, stop_lon}}

# timetable.json:
# {
#     trips.txt[trip_id]: {
#         route_id: trips.txt[route_id],
#         stops: {
#             stop_sequence: {
#                 stop_id: stop_times[stop_id],
#                 arrival_time: stop_times[arrival_time],
#                 departure_time: stop_times[departure_time]
#             }
#         }
#     }
# }


import csv
import json
import os
import requests
from datetime import datetime
from datetime import timedelta
from io import BytesIO
from zoneinfo import ZoneInfo
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
        if row["route_type"] == "2"
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

# The feed uses Belgian local time
today = datetime.now(ZoneInfo("Europe/Brussels"))
tomorow = today + timedelta(days=1)
date_td = today.strftime("%Y%m%d")
date_tm = tomorow.strftime("%Y%m%d")
print(f"Finding services for {date_td} and {date_tm}")

services = set()
print("Parsing calendar dates")
with open("data/calendar_dates.txt") as csvfile:
    reader = csv.DictReader(csvfile, delimiter=",")

    for row in reader:
        if row["date"] in (date_td, date_tm):
            services.add(row["service_id"])

if len(services) == 0:
    print("No services found")
    terminate_connection()

print("Checking routes")
valid_routes = {}
with open("data/routes.json", "r") as f:
    valid_routes = json.load(f)

trips = {}
print("Parsing trips")
with open("data/trips.txt") as csvfile:
    reader = csv.DictReader(csvfile, delimiter=",")

    trips = {
        row["trip_id"]: {
            "route_id": row["route_id"],
            "stops": {}
        }
        for row in reader
        if row["service_id"] in services and row["route_id"] in valid_routes
    }

if len(trips) == 0:
    print("Failed parsing trips")
    terminate_connection()

print("Parsing stop times")
with open("data/stop_times.txt") as csvfile:
    reader = csv.DictReader(csvfile, delimiter=",")

    for row in reader:
        trip_id = row["trip_id"]
        if trip_id in trips:
            trips[trip_id]["stops"].update({
                row["stop_sequence"]:
                {
                    "stop_id": row["stop_id"],
                    "arrival_time": row["arrival_time"],
                    "departure_time": row["departure_time"]
                }
            })

print("Writing json files")
json_stops = json.dumps(trips, indent=4)
with open("data/timetable.json", "w") as f:
    f.write(json_stops)

print("Done, cleaning up")
terminate_connection()
