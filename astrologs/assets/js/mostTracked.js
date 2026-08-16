async function getMostTrackedObjects() {
    const { data, error } = await supabaseClient
        .rpc("get_most_tracked_objects", {
            limit_count: 10
        });
    if (error) {
        console.error("Most tracked objects:", error);
        return [];
    }
    return data;
}

async function displayMostTrackedObjects() {
    const objects = await getMostTrackedObjects();
    const results = document.getElementById("mostTracked");
    let html = `
        <table>
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Object Type</th>
                    <th>Count</th>
                </tr>
            </thead>
            <tbody>
    `;
    for (const object of objects) {
        html += `
            <tr>
                <td>${object.name}</td>
                <td>${object.object_type}</td>
                <td>${object.observation_count}</td>
            </tr>
        `;
    }
    html += `
            </tbody>
        </table>
    `;
    results.innerHTML = html;
}

displayMostTrackedObjects();
