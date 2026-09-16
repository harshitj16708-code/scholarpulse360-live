// Instant theme apply before page render (prevents screen flicker)
(function () {
    const savedTheme = localStorage.getItem('theme') || 'dark';
    if (savedTheme === 'light') {
        document.documentElement.classList.add('light-theme');
    }
})();

document.addEventListener('DOMContentLoaded', () => {
    const themeToggleBtn = document.getElementById('theme-toggle');
    const themeIcon = document.getElementById('theme-icon');

    function updateThemeUI(isLight) {
        if (isLight) {
            document.body.classList.add('light-theme');
            document.documentElement.classList.add('light-theme');
            if (themeIcon) themeIcon.className = 'fa-solid fa-sun';
        } else {
            document.body.classList.remove('light-theme');
            document.documentElement.classList.remove('light-theme');
            if (themeIcon) themeIcon.className = 'fa-solid fa-moon';
        }
    }

    // Initial check on page load
    const currentTheme = localStorage.getItem('theme') || 'dark';
    updateThemeUI(currentTheme === 'light');

    // Button click handler
    if (themeToggleBtn) {
        themeToggleBtn.addEventListener('click', () => {
            const isCurrentlyLight = document.body.classList.contains('light-theme');
            const newTheme = isCurrentlyLight ? 'dark' : 'light';
            
            localStorage.setItem('theme', newTheme);
            updateThemeUI(newTheme === 'light');
        });
    }
});