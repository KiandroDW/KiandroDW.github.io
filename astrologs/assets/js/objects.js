// temp test database functions
const results = document.getElementById("results");
const loadButton = document.getElementById("loadObjects");

async function loadObjects() {
    showStatus("Loading astronomical objects...");
    results.innerHTML = "";
    const { data, error} = await supabaseClient.from("objects")
		.select(
			`id,
			name,
			constellation,
			magnitude,
			description,
			object_types (name)
			`)
		.order("name");
    if (error) {
        console.error(error);
        showStatus("Database error: " + error.message, "error");
        return;
    }

    showStatus(`Successfully loaded ${data.length} objects.`, "success");

    if (data.length === 0) {
        results.innerHTML = "<p>No astronomical objects have been added yet.</p>";
        return;
    }

    let html = `
        <table>
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Type</th>
                    <th>Constellation</th>
                    <th>Magnitude</th>
                    <th>Description</th>
                </tr>
            </thead>

            <tbody>
    `;

    for (const object of data) {
        const type = object.object_types?.name ?? "";
        html += `
            <tr>
                <td>${object.name}</td>
                <td>${type}</td>
                <td>${object.constellation ?? ""}</td>
                <td>${object.magnitude ?? ""}</td>
                <td>${object.description ?? ""}</td>
            </tr>
        `;
    }


    html += `
            </tbody>
        </table>
    `;

    results.innerHTML = html;
}


loadButton.addEventListener("click", loadObjects);
