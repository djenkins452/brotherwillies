// Help modal toggle
function toggleHelp() {
    var modal = document.getElementById('help-modal');
    if (modal.style.display === 'none' || modal.style.display === '') {
        modal.style.display = 'flex';
        document.body.style.overflow = 'hidden';
    } else {
        modal.style.display = 'none';
        document.body.style.overflow = '';
    }
}

function closeHelpOutside(event) {
    if (event.target.classList.contains('help-overlay')) {
        toggleHelp();
    }
}

// Profile dropdown toggle
function toggleProfileDropdown() {
    var dropdown = document.getElementById('profile-dropdown');
    if (dropdown) {
        dropdown.classList.toggle('open');
    }
}

// Close dropdowns on outside click
document.addEventListener('click', function(e) {
    var dropdown = document.getElementById('profile-dropdown');
    if (dropdown && dropdown.classList.contains('open')) {
        var wrap = dropdown.closest('.profile-dropdown-wrap');
        if (!wrap.contains(e.target)) {
            dropdown.classList.remove('open');
        }
    }
});

// Close help on Escape, also close profile dropdown
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        var modal = document.getElementById('help-modal');
        if (modal && modal.style.display === 'flex') {
            toggleHelp();
        }
        var dropdown = document.getElementById('profile-dropdown');
        if (dropdown) {
            dropdown.classList.remove('open');
        }
    }
});

// Slider value display
document.addEventListener('DOMContentLoaded', function() {
    document.querySelectorAll('input[type="range"]').forEach(function(slider) {
        var output = document.getElementById(slider.id + '-value');
        if (output) {
            output.textContent = parseFloat(slider.value).toFixed(2);
            slider.addEventListener('input', function() {
                output.textContent = parseFloat(this.value).toFixed(2);
            });
        }
    });
});
