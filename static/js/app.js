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

// Close help on Escape
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        var modal = document.getElementById('help-modal');
        if (modal && modal.style.display === 'flex') {
            toggleHelp();
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
