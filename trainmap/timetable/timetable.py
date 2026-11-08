# Get the timetable data and put it into one large json
# Directory structure:
# timetable/
# ├── timetable.py
# └── timetable.json

# Data gathered from zip file:
# calendar_dates.txt -> on which dates every service runs
# stop_times.txt -> every stop on every trip
# trips.txt -> link every trip with a route and a service

# Only trips that run today are added

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
    os.remove("timetable/agency.txt")
    os.remove("timetable/calendar.txt")
    os.remove("timetable/calendar_dates.txt")
    os.remove("timetable/feed_info.txt")
    os.remove("timetable/routes.txt")
    os.remove("timetable/stop_times.txt")
    os.remove("timetable/stops.txt")
    os.remove("timetable/transfers.txt")
    os.remove("timetable/translations.txt")
    os.remove("timetable/trips.txt")
    print("Finished deleting")
    exit(0)


url = 'https://api-management-discovery-production.azure-api.net/api/gtfs/feed/nmbssncb/static'
print("Downloading started")
response = requests.get(url)
file_path = "timetable/nmbssncb.zip"

if response.status_code == 200:
    with ZipFile(BytesIO(response.content)) as zip_file:
        zip_file.extractall('timetable/')
        print("Files extracted succesfully")
else:
    print(f"Failed to download file: {response.status_code}")
    exit(1)


# The feed uses Belgian local time
today = datetime.now(ZoneInfo("Europe/Brussels"))
tomorow = today + timedelta(days=1)
date_td = today.strftime("%Y%m%d")
date_tm = tomorow.strftime("%Y%m%d")
print(f"Finding services for {date_td} and {date_tm}")

services = set()
print("Parsing calendar dates")
with open("timetable/calendar_dates.txt") as csvfile:
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
with open("timetable/trips.txt") as csvfile:
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
with open("timetable/stop_times.txt") as csvfile:
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
with open("timetable/timetable.json", "w") as f:
    f.write(json_stops)

print("Done, cleaning up")
terminate_connection()
