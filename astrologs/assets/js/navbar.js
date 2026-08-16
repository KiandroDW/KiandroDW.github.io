// Mobile menu toggle
(function () {
  var nav = document.getElementById('astrologsNav');
  var toggle = document.getElementById('navToggle');

  toggle.addEventListener('click', function () {
    var isOpen = nav.classList.toggle('is-open');
    toggle.setAttribute('aria-expanded', isOpen);
  });
})();

// Example: swap between logged-out and logged-in state.
// Replace this with your real auth check (e.g. a session cookie or API call).
function setAstrologsAuthState(isLoggedIn, initials) {
  var authBtn = document.getElementById('authBtn');
  var profileBtn = document.getElementById('profileBtn');

  if (isLoggedIn) {
    authBtn.style.display = 'none';
    profileBtn.style.display = 'inline-flex';
    if (initials) profileBtn.textContent = initials;
  } else {
    authBtn.style.display = 'inline-flex';
    profileBtn.style.display = 'none';
  }
}

// Demo: try setAstrologsAuthState(true, 'M') in the console to preview the logged-in state.
