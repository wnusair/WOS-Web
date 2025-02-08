// scripts.js

// Confirmation for delete action
document.addEventListener('DOMContentLoaded', function () {
    const deleteForms = document.querySelectorAll('form[action*="/delete/"]');
    deleteForms.forEach(function (form) {
        form.addEventListener('submit', function (e) {
            if (!confirm('Are you sure you want to delete this item?')) {
                e.preventDefault();
            }
        });
    });
});
