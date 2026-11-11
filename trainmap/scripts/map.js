async function loadMap() {
	const map = L.map('map');

	L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
		maxZoom: 15,
		attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
	}).addTo(map);

	const [geojson, stops, blacklistText] = await Promise.all([
		fetch('data/simplified.geojson').then(r => r.json()),
		fetch('data/stops.json').then(r => r.json()),
		fetch('data/blacklist.txt').then(r => r.text())
	]);

	const blacklist = new Set(
		blacklistText
		.split(/\r?\n/)
		.map(line => line.trim())
		.filter(line => line.length > 0)
	);

	const geoLayer = L.geoJSON(geojson).addTo(map);

	const dots = L.featureGroup(
		Object.values(stops)
		.filter(p => !blacklist.has(p.stop_name.trim()))
		.filter(p => !/\([A-Z]{2}\)$/.test(p.stop_name))   // your existing rule, drop if not needed
		.map(p =>
			L.circleMarker([p.stop_lat, p.stop_lon], {
				radius: 7,
				color: '#fff',
				weight: 2,
				fillColor: '#d33',
				fillOpacity: 0.9
			}).bindTooltip(p.stop_name, { permanent: false, direction: 'right', offset: [8, 0] })
		)
	).addTo(map);

	map.fitBounds(geoLayer.getBounds());
}
