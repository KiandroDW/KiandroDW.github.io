async function getMyObservedObjects() {
    const { data, error } = await supabaseClient
        .from("observations")
        .select(`
            id,
            observed_at,
            notes,
            objects (
                id,
                name,
                constellation,
                magnitude,
                object_types (
                    name
                )
            )
        `)
        .order("observed_at", {
            ascending: false
        });
    if (error) {
        console.error("My observations:", error);
        return [];
    }
    return data;
}

async function observeList() {
	const observations = await getMyObservedObjects();

	for (const observation of observations) {
		console.log(
			observation.objects.name,
			observation.observed_at,
			observation.notes
		);
	}
}

observeList();

async function getCatalogProgress(catalogName) {
    // Find the catalog
    const { data: catalog, error: catalogError } =
        await supabaseClient
            .from("catalogs")
            .select("id, name")
            .eq("name", catalogName)
            .single();
    if (catalogError) {
        console.error("Catalog:", catalogError);
        return null;
    }
    // Get all objects belonging to this catalog
    const { data: catalogObjects, error: objectsError } =
        await supabaseClient
            .from("object_catalogs")
            .select("object_id")
            .eq("catalog_id", catalog.id);
    if (objectsError) {
        console.error("Catalog objects:", objectsError);
        return null;
    }
    const objectIds = catalogObjects.map(
        row => row.object_id
    );
    // Nothing in catalog
    if (objectIds.length === 0) {
        return {
            catalog: catalog.name,
            total: 0,
            observed: 0,
            percentage: 0
        };
    }
    // Find this user's observations of those objects
    const { data: observations, error: observationError } =
        await supabaseClient
            .from("observations")
            .select("object_id")
            .in("object_id", objectIds);
    if (observationError) {
        console.error("Observations:", observationError);
        return null;
    }
    // Multiple observations of the same object only count once
    const observedObjectIds = new Set(
        observations.map(row => row.object_id)
    );
    const observed = observedObjectIds.size;
    const total = objectIds.length;
    const percentage =
        total === 0
            ? 0
            : Math.round((observed / total) * 100);
    return {
        catalog: catalog.name,
        total,
        observed,
        percentage
    };
}

async function getAllCatalogProgress() {
    const { data: catalogs, error } =
        await supabaseClient
            .from("catalogs")
            .select("id, name")
            .order("name");
    if (error) {
        console.error(error);
        return [];
    }
    const results = [];
    for (const catalog of catalogs) {
        const progress =
            await getCatalogProgress(catalog.name);
        if (progress) {
            results.push(progress);
        }
    }
	console.log(results);
	const progressBars = document.getElementById("progressBars");
	let html = ""

	for (const result of results) {
		html += `
			<p>${result.catalog}: ${result.observed}/${result.total}</p>
			<progress value="${result.observed}" max="${result.total}"></progress>
		`
	}
	progressBars.innerHTML = html;
    return results;
}

getAllCatalogProgress();
