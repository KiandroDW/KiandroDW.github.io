const status = document.getElementById("status");
const authForms = document.getElementById("authForms");
const userSection = document.getElementById("userSection");
const userEmail = document.getElementById("userEmail");
const userId = document.getElementById("userId");

function showStatus(message, type = "") {
    status.textContent = message;
    status.className = type;
}

async function register() {
    const email = document.getElementById("registerEmail").value.trim();
    const password = document.getElementById("registerPassword").value;
    if (!email || !password) {
        showStatus("Please enter an email and password.", "error");
        return;
    }
    showStatus("Creating account...");
    const { data, error } = await supabaseClient.auth.signUp({
		email: email,
		password: password
	});
    if (error) {
        console.error(error);
        showStatus("Registration error: " + error.message, "error");
        return;
    }
    console.log("Registration result:", data);
    if (data.session) {
        showStatus("Account created and logged in.", "success");
    } else {
        showStatus(
			"Account created. Check your email to confirm your account.",
            "success"
        );
    }
}

async function login() {
    const email = document.getElementById("loginEmail").value.trim();
    const password = document.getElementById("loginPassword").value;
    if (!email || !password) {
        showStatus("Please enter an email and password.", "error");
        return;
    }
    showStatus("Logging in...");
    const { data, error } = await supabaseClient.auth.signInWithPassword({
		email: email,
		password: password
	});
    if (error) {
        console.error(error);
        showStatus("Login error: " + error.message, "error");
        return;
    }
    console.log("Login result:", data);
    showStatus("Successfully logged in.", "success");
    updateUserInterface(data.user);
}

async function logout() {
    const { error } = await supabaseClient.auth.signOut();
    if (error) {
        console.error(error);
        showStatus("Logout error: " + error.message, "error"
        );
        return;
    }
    showStatus("Logged out.", "success");
    updateUserInterface(null);
}

function updateUserInterface(user) {
    if (user) {
        authForms.classList.add("hidden");
        userSection.classList.remove("hidden");
        userEmail.textContent = user.email ?? "";
        userId.textContent = user.id;
    } else {
        authForms.classList.remove("hidden");
        userSection.classList.add("hidden");
        userEmail.textContent = "";
        userId.textContent = "";
    }
}

async function checkSession() {
    const {data, error} = await supabaseClient.auth.getSession();
    if (error) {
        console.error(error);
        showStatus("Session error: " + error.message, "error");
        return;
    }
    const session = data.session;
    if (session) {
        updateUserInterface(session.user);
        showStatus("Connected and logged in.", "success");
    } else {
        updateUserInterface(null);
        showStatus("Connected to Supabase. Not logged in.", "success");
    }
}

supabaseClient.auth.onAuthStateChange(
    (event, session) => {
        console.log("Auth event:", event);
        if (session) {
            updateUserInterface(session.user);
        } else {
            updateUserInterface(null);
        }
    }
);

// Event listeners
document.getElementById("registerButton").addEventListener("click", register);
document.getElementById("loginButton").addEventListener("click", login);
document.getElementById("logoutButton").addEventListener("click", logout);

// Start
checkSession();
