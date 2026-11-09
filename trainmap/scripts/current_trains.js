// Resolve data files relative to the project root = parent folder of /scripts/.
// (document.currentScript is only available while the script is first loading, so capture it here.)
const ROOT_URL = new URL("../", document.currentScript.src);

// The static JSON files never change between refreshes, so load them only once.
let staticDataPromise = null;

function loadStaticData() {
	if (!staticDataPromise) {
		const readJson = async (path) => {
			const res = await fetch(new URL(path, ROOT_URL));
			if (!res.ok) throw new Error(`Could not load ${path}: HTTP ${res.status}`);
			return res.json();
		};
		staticDataPromise = Promise.all([
			readJson("data/routes.json"),
			readJson("data/stops.json"),
			readJson("data/timetable.json"),
		]).catch((err) => {
			staticDataPromise = null; // allow a retry on the next refresh
			throw err;
		});
	}
	return staticDataPromise;
}

async function getPositions() {
	const [routes, stops, timetable] = await loadStaticData();

	const TZ = "Europe/Brussels";

	// Offset (in seconds) of Brussels time relative to UTC at a given unix time
	function brusselsOffset(unixSec) {
		const parts = new Intl.DateTimeFormat("en-US", {
			timeZone: TZ,
			hourCycle: "h23",
			year: "numeric", month: "numeric", day: "numeric",
			hour: "numeric", minute: "numeric", second: "numeric",
		}).formatToParts(new Date(unixSec * 1000));
		const p = {};
		for (const { type, value } of parts) p[type] = Number(value);
		const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) / 1000;
		return asUtc - unixSec;
	}

	// Brussels wall-clock time -> unix seconds (DST safe)
	function brusselsToUnix(y, m, d, h, mi, s) {
		const guess = Date.UTC(y, m - 1, d, h, mi, s) / 1000;
		let result = guess - brusselsOffset(guess);
		result = guess - brusselsOffset(result);
		return result;
	}

	const nowUnix = Math.floor(Date.now() / 1000);

	const [Y, M, D] = new Intl.DateTimeFormat("en-CA", {
		timeZone: TZ,
		year: "numeric", month: "2-digit", day: "2-digit",
	}).format(new Date(nowUnix * 1000)).split("-").map(Number);

	// GTFS: times are measured from "noon minus 12h" on the service day.
	const serviceDayStart = brusselsToUnix(Y, M, D, 12, 0, 0) - 12 * 3600;

	// "HH:MM:SS" (Brussels, may exceed 24h) -> unix seconds
	function toUnix(t) {
		const [h, m, s] = t.split(":").map(Number);
		return serviceDayStart + h * 3600 + m * 60 + s;
	}

	const url = "https://api-management-discovery-production.azure-api.net/api/gtfs/feed/nmbssncb/rt/trip-update";

	const response = await fetch(url);
	if (!response.ok) {
		throw new Error(`HTTP ${response.status} ${response.statusText}`);
	}
	const today = new Date();
	const formattedDate = today.getFullYear().toString() + 
		String(today.getMonth() + 1).padStart(2, '0') +
		String(today.getDate()).padStart(2, '0');

	const data = await response.json();

	// tripId -> stopTimeUpdate[]
	const rtByTrip = new Map(
		data.entity.map((e) => [e.tripUpdate.trip.tripId, e.tripUpdate.stopTimeUpdate])
	);

	// Scheduled times of a stop shifted by its delay.
	// The delay is looked up in the stopTimeUpdate for this exact stop;
	// if the stop has no (usable) update, the previous known delay carries over.
	function actualTimes(stop, updates, carriedDelay) {
		let arrDelay = carriedDelay;
		let depDelay = carriedDelay;

		const upd = updates?.find((u) => u.stopId === stop.stop_id);
		if (upd && upd.scheduleRelationship !== 2) {
			// first stop has no arrival, last stop has no departure
			arrDelay = upd.arrival?.delay ?? upd.departure.delay;
			depDelay = upd.departure?.delay ?? upd.arrival.delay;
		}

		return {
			arr: toUnix(stop.arrival_time) + arrDelay,
			dep: toUnix(stop.departure_time) + depDelay,
			arrDelay,
			depDelay,
		};
	}

	const ret_obj = {};

	for (const [tripId, trip] of Object.entries(timetable)) {
		if (!trip.dates.includes(formattedDate)) {
			continue;
		}
		const route = routes[trip.route_id];
		const stopList = Object.values(trip.stops);
		const updates = rtByTrip.get(tripId); // undefined => no realtime data
		const hasRt = updates !== undefined;

		let delay = 0;
		let prev = null; // actual times of the previous stop

		for (let i = 0; i < stopList.length; i++) {
			const curr = actualTimes(stopList[i], updates, delay);
			delay = curr.depDelay;

			// Between previous and current stop (delay-corrected)
			if (prev && prev.dep < nowUnix && nowUnix < curr.arr) {
				if (/\([A-Z][A-Z]\)$/.test(stops[stopList[i-1].stop_id].stop_name)) {
					// Not currently in Belgium.
					break;
				}
				ret_obj[tripId] = {
					name: route.route_long_name,
					short_name: route.route_short_name,
					stopped: false,
					prev_station: stops[stopList[i - 1].stop_id].stop_name,
					next_station: stops[stopList[i].stop_id].stop_name,
					dep_time: prev.dep,
					arr_time: curr.arr,
					current_delay: hasRt ? prev.depDelay : -1,
				};
				break;
			}

			// Standing at the current stop (delay-corrected)
			if (curr.arr < nowUnix && nowUnix < curr.dep) {
				if (/\([A-Z][A-Z]\)$/.test(stops[stopList[i].stop_id].stop_name)) {
					// Not currently in Belgium.
					break;
				}
				ret_obj[tripId] = {
					name: route.route_long_name,
					short_name: route.route_short_name,
					stopped: true,
					curr_station: stops[stopList[i].stop_id].stop_name,
					next_station: stopList[i + 1] ? stops[stopList[i + 1].stop_id].stop_name : null,
					arr_time: curr.arr,
					dep_time: curr.dep,
					current_delay: hasRt ? curr.depDelay : -1,
				};
				break;
			}

			prev = curr;
		}
	}

	return ret_obj;
}
