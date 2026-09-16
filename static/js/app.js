document.addEventListener("DOMContentLoaded", async () => {
    try {
        const res = await fetch('/api/opportunities');
        const result = await res.json();
        
        const countElement = document.getElementById('opportunity-count');
        if (countElement && result.data) {
            countElement.textContent = `${result.data.length} Available`;
        }
    } catch (err) {
        console.error("Error fetching opportunities:", err);
    }
});